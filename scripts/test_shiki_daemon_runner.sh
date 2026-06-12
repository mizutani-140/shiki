#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-daemon-runner-test-$$"
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
python3 -m py_compile scripts/shiki.py scripts/validate_shiki.py
python3 scripts/shiki.py --help | grep -E "daemon|runner|smoke" >/dev/null

mkdir -p "$TARGET" "$FAKE_BIN"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-daemon-install.out

cd "$TARGET"
git init -b main >/tmp/shiki-daemon-git-init.out
# Hermetic git identity so `git commit` works in CI where no global git user is configured.
git config user.email "shiki-test@example.com"
git config user.name "Shiki Test"
git remote add origin https://github.com/example/shiki-daemon-test.git

cat >"$TMP_ROOT/plan.json" <<'JSON'
{
  "title": "Ship daemon smoke",
  "outcome": "A background loop can convert a grilled plan into Shiki run state",
  "completion_conditions": ["One task is dispatchable"],
  "non_goals": ["Do not create real GitHub objects in this test"],
  "risk_level": "low",
  "required_skills": ["grill-with-docs", "tdd"],
  "grill_with_docs": {
    "status": "complete",
    "source": "CONTEXT.md",
    "decisions": ["Use one dispatchable task"]
  },
  "spec_freeze": {
    "status": "frozen",
    "approved_by": "operator",
    "source": "test fixture"
  },
  "tasks": [
    {
      "title": "Write daemon marker",
      "scope": "Create the smallest runner-visible task",
      "acceptance_checks": ["Runner command records success"],
      "locks": ["path:daemon-marker.txt"],
      "required_skills": ["tdd"]
    }
  ]
}
JSON

python3 "$ROOT/scripts/shiki.py" daemon enqueue-plan --target "$TARGET" --plan-file "$TMP_ROOT/plan.json" >/tmp/shiki-enqueue.json
INBOX_FILE="$(json_get /tmp/shiki-enqueue.json inbox_file)"
test -f "$INBOX_FILE"

python3 "$ROOT/scripts/shiki.py" daemon run --target "$TARGET" --once >/tmp/shiki-daemon-run.json
RUN_ID="$(json_get /tmp/shiki-daemon-run.json run_id)"
test -f "$TARGET/.shiki/runs/$RUN_ID.json"
test -z "$(find "$TARGET/.shiki/inbox" -maxdepth 1 -type f -name '*.json' -print -quit)"

python3 "$ROOT/scripts/shiki.py" runner next --target "$TARGET" >/tmp/shiki-runner-next.json
TASK_ID="$(json_get /tmp/shiki-runner-next.json task_id)"

python3 "$ROOT/scripts/shiki.py" runner execute \
  --target "$TARGET" \
  --task-id "$TASK_ID" \
  --command "printf runner-ok > daemon-marker.txt" \
  >/tmp/shiki-runner-execute.json
grep "runner-ok" "$TARGET/daemon-marker.txt" >/dev/null

python3 "$ROOT/scripts/shiki.py" task status --target "$TARGET" "$TASK_ID" --status "done" >/tmp/shiki-daemon-task-done.json
python3 "$ROOT/scripts/shiki.py" goal complete --target "$TARGET" "$(json_get /tmp/shiki-daemon-run.json goal_id)" >/tmp/shiki-daemon-goal-complete.json

cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"${SHIKI_FAKE_GH_LOG}"
case "$1 $2" in
  "auth status")
    exit 0
    ;;
  "repo view")
    echo '{"name":"shiki-daemon-test"}'
    exit 0
    ;;
  "issue create")
    echo "https://github.com/example/shiki-daemon-test/issues/88"
    exit 0
    ;;
  "pr create")
    echo "https://github.com/example/shiki-daemon-test/pull/99"
    exit 0
    ;;
esac
echo "fake gh unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/gh"
export PATH="$FAKE_BIN:$PATH"
export SHIKI_FAKE_GH_LOG="$TMP_ROOT/gh.log"

cat >"$TMP_ROOT/smoke-plan.json" <<'JSON'
{
  "title": "Ship live smoke",
  "outcome": "A live smoke can create GitHub evidence for a fresh task",
  "completion_conditions": ["One smoke task is dispatchable"],
  "non_goals": ["Do not depend on previous daemon locks"],
  "risk_level": "low",
  "required_skills": ["grill-with-docs", "tdd"],
  "grill_with_docs": {
    "status": "complete",
    "source": "CONTEXT.md",
    "decisions": ["Use a separate smoke lock"]
  },
  "spec_freeze": {
    "status": "frozen",
    "approved_by": "operator",
    "source": "test fixture"
  },
  "tasks": [
    {
      "title": "Write smoke marker",
      "scope": "Create the smallest GitHub-visible smoke task",
      "acceptance_checks": ["GitHub issue and PR evidence is recorded"],
      "locks": ["path:smoke-marker.txt"],
      "required_skills": ["tdd"]
    }
  ]
}
JSON

python3 "$ROOT/scripts/shiki.py" smoke live \
  --target "$TARGET" \
  --plan-file "$TMP_ROOT/smoke-plan.json" \
  --dry-run \
  >/tmp/shiki-smoke-dry-run.json
grep "auth status" "$SHIKI_FAKE_GH_LOG" >/dev/null

python3 "$ROOT/scripts/shiki.py" smoke live \
  --target "$TARGET" \
  --plan-file "$TMP_ROOT/smoke-plan.json" \
  --execute-github \
  >/tmp/shiki-smoke-execute.json
grep "issue create" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep "pr create" "$SHIKI_FAKE_GH_LOG" >/dev/null

test -f "$ROOT/.github/workflows/shiki-orchestrator.yml"
grep "workflow_dispatch" "$ROOT/.github/workflows/shiki-orchestrator.yml" >/dev/null
grep "issue_comment" "$ROOT/.github/workflows/shiki-orchestrator.yml" >/dev/null

python3 "$TARGET/scripts/validate_shiki.py"

echo "shiki daemon runner tests passed"
