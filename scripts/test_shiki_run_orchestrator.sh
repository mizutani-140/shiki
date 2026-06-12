#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-run-test-$$"
TARGET="$TMP_ROOT/target"
FAKE_BIN="$TMP_ROOT/bin"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

json_get() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

cd "$ROOT"

python3 scripts/validate_shiki.py
python3 -m py_compile scripts/shiki.py
python3 scripts/shiki.py --help | grep -E "plan|run|github|handoff" >/dev/null

mkdir -p "$TARGET" "$FAKE_BIN"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-run-install.out

cd "$TARGET"
git init -b main >/tmp/shiki-run-git-init.out
# Hermetic git identity so `git commit` works in CI where no global git user is configured.
git config user.email "shiki-test@example.com"
git config user.name "Shiki Test"
git remote add origin https://github.com/example/shiki-run-test.git

cat >"$TMP_ROOT/grilled-plan.json" <<'JSON'
{
  "title": "Ship guided onboarding",
  "outcome": "A user can finish onboarding through the smallest verified workflow",
  "completion_conditions": [
    "All generated tasks are done",
    "CCA and MergeGate evidence exists"
  ],
  "non_goals": [
    "Do not add billing",
    "Do not bypass GitHub checks"
  ],
  "risk_level": "medium",
  "required_skills": ["grill-with-docs", "to-prd", "to-issues", "tdd"],
  "grill_with_docs": {
    "status": "complete",
    "source": "CONTEXT.md",
    "decisions": ["Use one vertical slice first"]
  },
  "spec_freeze": {
    "status": "frozen",
    "approved_by": "operator",
    "source": "test fixture"
  },
  "tasks": [
    {
      "title": "Create onboarding checklist",
      "scope": "Add the smallest end-to-end checklist path",
      "acceptance_checks": ["User can see one onboarding checklist"],
      "locks": ["path:src/onboarding/*"],
      "required_skills": ["tdd"]
    },
    {
      "title": "Record onboarding completion",
      "scope": "Persist completion and expose it in the UI",
      "acceptance_checks": ["Completion survives reload"],
      "dependencies": ["Create onboarding checklist"],
      "locks": ["path:src/onboarding/*"],
      "required_skills": ["tdd"]
    }
  ]
}
JSON

if python3 "$ROOT/scripts/shiki.py" plan ingest --target "$TARGET" --plan-file "$TMP_ROOT/ungrilled.json" 2>/tmp/shiki-run-missing.out; then
  echo "expected missing plan file to fail" >&2
  exit 1
fi

# A grilled but unfrozen plan must be rejected (ADR 0009 Spec Freeze gate).
python3 - "$TMP_ROOT/grilled-plan.json" "$TMP_ROOT/unfrozen.json" <<'PY'
import json
import sys

plan = json.load(open(sys.argv[1]))
plan.pop("spec_freeze", None)
json.dump(plan, open(sys.argv[2], "w"), indent=2)
PY
if python3 "$ROOT/scripts/shiki.py" plan ingest --target "$TARGET" --plan-file "$TMP_ROOT/unfrozen.json" 2>/tmp/shiki-run-unfrozen.out; then
  echo "expected unfrozen plan to fail" >&2
  exit 1
fi
grep "spec_freeze.status=frozen" /tmp/shiki-run-unfrozen.out >/dev/null

python3 "$ROOT/scripts/shiki.py" plan ingest --target "$TARGET" --plan-file "$TMP_ROOT/grilled-plan.json" >/tmp/shiki-plan-ingest.json
PLAN_ID="$(json_get /tmp/shiki-plan-ingest.json plan_id)"
test -f "$TARGET/.shiki/plans/$PLAN_ID.json"

python3 "$ROOT/scripts/shiki.py" run --target "$TARGET" --plan "$PLAN_ID" >/tmp/shiki-run.json
GOAL_ID="$(json_get /tmp/shiki-run.json goal_id)"
test -f "$TARGET/.shiki/goals/$GOAL_ID.json"
test -f "$TARGET/.shiki/dag/$GOAL_ID.json"
test "$(find "$TARGET/.shiki/tasks" -type f -name 'T-*.json' | wc -l | tr -d ' ')" = "2"
test "$(find "$TARGET/.shiki/locks" -type f -name 'T-*.json' | wc -l | tr -d ' ')" = "1"
test "$(find "$TARGET/.shiki/worktrees" -type f -name 'T-*.json' | wc -l | tr -d ' ')" = "1"

cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"${SHIKI_FAKE_GH_LOG}"
if [[ "$1 $2" == "issue create" ]]; then
  echo "https://github.com/example/shiki-run-test/issues/42"
  exit 0
fi
if [[ "$1 $2" == "pr create" ]]; then
  echo "https://github.com/example/shiki-run-test/pull/77"
  exit 0
fi
echo "fake gh unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/gh"
export PATH="$FAKE_BIN:$PATH"
export SHIKI_FAKE_GH_LOG="$TMP_ROOT/gh.log"

FIRST_TASK="$(python3 - "$TARGET" <<'PY'
import json, pathlib, sys
tasks = sorted((pathlib.Path(sys.argv[1]) / ".shiki" / "tasks").glob("T-*.json"))
print(json.loads(tasks[0].read_text())["id"])
PY
)"

python3 "$ROOT/scripts/shiki.py" github issue --target "$TARGET" --task-id "$FIRST_TASK" >/tmp/shiki-github-issue.json
grep "issue create" "$SHIKI_FAKE_GH_LOG" >/dev/null

python3 "$ROOT/scripts/shiki.py" github pr --target "$TARGET" --task-id "$FIRST_TASK" --base main >/tmp/shiki-github-pr.json
grep "pr create" "$SHIKI_FAKE_GH_LOG" >/dev/null

python3 "$ROOT/scripts/shiki.py" repair packet \
  --target "$TARGET" \
  --task-id "$FIRST_TASK" \
  --pr 77 \
  --failing-item "CCA evidence missing" \
  --minimal-change "attach verification output only" \
  --verification-command "python3 scripts/validate_shiki.py" \
  >/tmp/shiki-run-repair.json
REPAIR_ID="$(json_get /tmp/shiki-run-repair.json repair_id)"

python3 "$ROOT/scripts/shiki.py" handoff repair --target "$TARGET" "$REPAIR_ID" >/tmp/shiki-repair-handoff.json
HANDOFF_FILE="$(json_get /tmp/shiki-repair-handoff.json handoff_file)"
test -f "$HANDOFF_FILE"
grep "$REPAIR_ID" "$HANDOFF_FILE" >/dev/null

python3 "$TARGET/scripts/validate_shiki.py"

echo "shiki run orchestrator tests passed"
