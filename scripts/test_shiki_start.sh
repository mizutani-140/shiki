#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-start-test-$$"
TARGET="$TMP_ROOT/target"
FAKE_BIN="$TMP_ROOT/bin"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

json_get() {
  python3 -c 'import json,sys; text=open(sys.argv[1]).read(); start=text.rfind("\n{"); start = 0 if start == -1 else start + 1; print(json.loads(text[start:])[sys.argv[2]])' "$1" "$2"
}

cd "$ROOT"

python3 scripts/validate_shiki.py
python3 -m py_compile scripts/shiki.py scripts/validate_shiki.py
python3 scripts/shiki.py --help | grep "start" >/dev/null

mkdir -p "$TARGET" "$FAKE_BIN"

cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"${SHIKI_FAKE_GH_LOG}"
case "$1 $2" in
  "auth status")
    exit 0
    ;;
  "repo view")
    exit 1
    ;;
  "repo create")
    echo "https://github.com/example/shiki-start-test"
    exit 0
    ;;
  "issue create")
    echo "https://github.com/example/shiki-start-test/issues/101"
    exit 0
    ;;
  "secret set")
    cat >/dev/null
    exit 0
    ;;
  "secret list")
    echo "CLAUDE_CODE_OAUTH_TOKEN"
    exit 0
    ;;
  "api repos/"*"actions/permissions/workflow")
    cat >"${SHIKI_FAKE_GH_WORKFLOW_PAYLOAD:-/dev/null}"
    exit 0
    ;;
  "api repos/"*"/protection")
    cat >/dev/null
    exit 0
    ;;
  "api repos/"*)
    cat >/dev/null
    exit 0
    ;;
esac
echo "fake gh unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/gh"
export PATH="$FAKE_BIN:$PATH"
export SHIKI_FAKE_GH_LOG="$TMP_ROOT/gh.log"
export GIT_AUTHOR_NAME="Shiki Test"
export GIT_AUTHOR_EMAIL="shiki-test@example.local"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
export CLAUDE_CODE_OAUTH_TOKEN="fake-test-token"

cat >"$TMP_ROOT/answers.json" <<'JSON'
{
  "repo": "example/shiki-start-test",
  "project_name": "Shiki Start Test",
  "goal": "Ship a one command Shiki start flow",
  "outcome": "A user can run one command and receive a GitHub-first Shiki project with task evidence",
  "completion_conditions": [
    "The first generated task is dispatchable",
    "A GitHub issue exists for the first task"
  ],
  "non_goals": [
    "Do not require manual shiki init before start",
    "Do not bypass grill-with-docs"
  ],
  "risk_level": "medium",
  "required_skills": ["grill-with-docs", "to-prd", "to-issues", "tdd"],
  "approve_spec_freeze": true,
  "tasks": [
    {
      "title": "Create one command start path",
      "scope": "Initialize the repo, persist the grilled plan, run Shiki orchestration, and create the first GitHub issue",
      "acceptance_checks": ["One command creates Shiki run state and issue evidence"],
      "locks": ["path:src/start/*"],
      "required_skills": ["tdd"]
    }
  ]
}
JSON

# Start without explicit Spec Freeze approval must fail (ADR 0009).
python3 - "$TMP_ROOT/answers.json" "$TMP_ROOT/answers-unapproved.json" <<'PY'
import json
import sys

answers = json.load(open(sys.argv[1]))
answers.pop("approve_spec_freeze", None)
json.dump(answers, open(sys.argv[2], "w"), indent=2)
PY
if python3 scripts/shiki.py start "$TARGET" --answers-file "$TMP_ROOT/answers-unapproved.json" --execute --no-push --no-protect </dev/null 2>/tmp/shiki-start-unapproved.out; then
  echo "expected start without spec-freeze approval to fail" >&2
  exit 1
fi
grep "Spec Freeze was not approved" /tmp/shiki-start-unapproved.out >/dev/null

python3 scripts/shiki.py start \
  "$TARGET" \
  --answers-file "$TMP_ROOT/answers.json" \
  --execute \
  --no-push \
  --no-protect \
  >/tmp/shiki-start.json

test -f "$TARGET/.shiki/repo.json"
test -n "$(find "$TARGET/.shiki/plans" -type f -name 'P-*.json' -print -quit)"
test -n "$(find "$TARGET/.shiki/runs" -type f -name 'RUN-*.json' -print -quit)"
test -n "$(find "$TARGET/.shiki/starts" -type f -name 'START-*.json' -print -quit)"
test -n "$(find "$TARGET/.shiki/tasks" -type f -name 'T-*.json' -print -quit)"
grep "repo create" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep "issue create" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep "secret set CLAUDE_CODE_OAUTH_TOKEN --repo example/shiki-start-test" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep '"configured": true' /tmp/shiki-start.json >/dev/null

START_ID="$(json_get /tmp/shiki-start.json start_id)"
GOAL_ID="$(json_get /tmp/shiki-start.json goal_id)"
SKILLS_DIR="$(json_get /tmp/shiki-start.json skills_dir)"
case "$START_ID" in
  START-[0-9][0-9][0-9][0-9] | START-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *) echo "unexpected start id: $START_ID" >&2; exit 1 ;;
esac
test -f "$TARGET/.shiki/starts/$START_ID.json"
test -n "$SKILLS_DIR"
test -f "$TARGET/.shiki/goals/$GOAL_ID.json"

python3 "$TARGET/scripts/validate_shiki.py"

# Protect-enabled start must configure GitHub Actions workflow permissions for
# the CCA Review Bridge (default=read, can-approve=true) right after branch
# protection, through cmd_start -> cmd_init (ADR 0013).
PROTECT_TARGET="$TMP_ROOT/protect-target"
mkdir -p "$PROTECT_TARGET"
: >"$SHIKI_FAKE_GH_LOG"
export SHIKI_FAKE_GH_WORKFLOW_PAYLOAD="$TMP_ROOT/start-workflow-payload.json"
python3 scripts/shiki.py start \
  "$PROTECT_TARGET" \
  --answers-file "$TMP_ROOT/answers.json" \
  --execute \
  --no-push \
  >/tmp/shiki-start-protect.json
grep "api repos/example/shiki-start-test/actions/permissions/workflow -X PUT" "$SHIKI_FAKE_GH_LOG" >/dev/null
python3 - "$SHIKI_FAKE_GH_WORKFLOW_PAYLOAD" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload["default_workflow_permissions"] != "read":
    raise SystemExit(f"expected default_workflow_permissions read, got {payload['default_workflow_permissions']}")
if payload["can_approve_pull_request_reviews"] is not True:
    raise SystemExit("expected can_approve_pull_request_reviews to be true")
PY

echo "shiki start tests passed"
