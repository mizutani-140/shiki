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

python3 scripts/shiki.py start \
  "$TARGET" \
  --answers-file "$TMP_ROOT/answers.json" \
  --no-push \
  --no-protect \
  --no-set-secret \
  >/tmp/shiki-start.json

test -f "$TARGET/.shiki/repo.json"
test -f "$TARGET/.shiki/plans/P-0001.json"
test -f "$TARGET/.shiki/runs/RUN-0001.json"
test -f "$TARGET/.shiki/starts/START-0001.json"
test -n "$(find "$TARGET/.shiki/tasks" -type f -name 'T-*.json' -print -quit)"
grep "repo create" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep "issue create" "$SHIKI_FAKE_GH_LOG" >/dev/null

START_ID="$(json_get /tmp/shiki-start.json start_id)"
GOAL_ID="$(json_get /tmp/shiki-start.json goal_id)"
SKILLS_DIR="$(json_get /tmp/shiki-start.json skills_dir)"
test "$START_ID" = "START-0001"
test -n "$SKILLS_DIR"
test -f "$TARGET/.shiki/goals/$GOAL_ID.json"

python3 "$TARGET/scripts/validate_shiki.py"

echo "shiki start tests passed"
