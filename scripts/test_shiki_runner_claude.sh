#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-runner-claude-test-$$"
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
python3 scripts/shiki.py runner --help | grep "claude" >/dev/null

mkdir -p "$TARGET" "$FAKE_BIN"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-runner-claude-install.out

cd "$TARGET"
git init -b main >/tmp/shiki-runner-claude-git-init.out
git remote add origin https://github.com/example/shiki-runner-claude-test.git
git add .
git -c user.name="Shiki Test" -c user.email="shiki@example.test" commit -m "init" >/tmp/shiki-runner-claude-commit.out

cat >"$TMP_ROOT/plan.json" <<'JSON'
{
  "title": "Ship autonomous Claude Code dispatch",
  "outcome": "A ready task can be executed through Claude Code without asking the user to run a command",
  "completion_conditions": ["Claude runner evidence exists"],
  "non_goals": ["Do not use a real Claude Code session in this test"],
  "risk_level": "low",
  "required_skills": ["grill-with-docs", "tdd"],
  "grill_with_docs": {
    "status": "complete",
    "source": "CONTEXT.md",
    "decisions": ["Use Claude Code as the default implementation runtime (ADR 0008)"]
  },
  "spec_freeze": {
    "status": "frozen",
    "approved_by": "operator",
    "source": "test fixture"
  },
  "tasks": [
    {
      "title": "Write Claude marker",
      "scope": "Create the smallest Claude-visible implementation task",
      "acceptance_checks": ["Claude fake writes a marker in the materialized worktree"],
      "locks": ["path:claude-marker.txt"],
      "required_skills": ["tdd"]
    }
  ]
}
JSON

python3 "$ROOT/scripts/shiki.py" plan ingest --target "$TARGET" --plan-file "$TMP_ROOT/plan.json" >/tmp/shiki-runner-claude-plan.json
PLAN_ID="$(json_get /tmp/shiki-runner-claude-plan.json plan_id)"
python3 "$ROOT/scripts/shiki.py" run --target "$TARGET" --plan "$PLAN_ID" >/tmp/shiki-runner-claude-run.json
python3 "$ROOT/scripts/shiki.py" runner next --target "$TARGET" >/tmp/shiki-runner-claude-next.json
TASK_ID="$(json_get /tmp/shiki-runner-claude-next.json task_id)"

grep '"assigned_runtime": "claude-code"' "$TARGET/.shiki/tasks/$TASK_ID.json" >/dev/null

python3 "$ROOT/scripts/shiki.py" handoff task --target "$TARGET" "$TASK_ID" >/tmp/shiki-runner-claude-handoff.json
test -f "$TARGET/.shiki/handoffs/$TASK_ID-task.md"

cat >"$FAKE_BIN/claude" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  --version)
    echo "2.0.0 (Claude Code)"
    exit 0
    ;;
  auth)
    if [[ "${2:-}" == "status" ]]; then
      echo '{"loggedIn": true, "authMethod": "claude_subscription_oauth", "apiProvider": "anthropic"}'
      exit 0
    fi
    ;;
  -p)
    pwd > claude-marker.txt
    cat > claude-prompt.txt
    echo "claude fake executed"
    exit 0
    ;;
esac
echo "fake claude unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/claude"
export PATH="$FAKE_BIN:$PATH"

# Dry run shows the dispatch without executing it.
python3 "$ROOT/scripts/shiki.py" runner claude --target "$TARGET" --task-id "$TASK_ID" --dry-run >/tmp/shiki-runner-claude-dry.json
grep "claude -p" /tmp/shiki-runner-claude-dry.json >/dev/null
test ! -f "$TARGET/claude-marker.txt"

python3 "$ROOT/scripts/shiki.py" runner claude --target "$TARGET" --task-id "$TASK_ID" >/tmp/shiki-runner-claude-execute.json
WORKTREE="$(json_get /tmp/shiki-runner-claude-execute.json worktree)"
test -f "$WORKTREE/claude-marker.txt"
grep "$TASK_ID" "$WORKTREE/claude-prompt.txt" >/dev/null
grep '"status": "review"' "$TARGET/.shiki/tasks/$TASK_ID.json" >/dev/null
grep "claude -p" "$TARGET"/.shiki/runner/EXEC-*.json >/dev/null

# A claude-code task must not dispatch through the codex runner without --force.
if python3 "$ROOT/scripts/shiki.py" runner codex --target "$TARGET" --task-id "$TASK_ID" --dry-run >/tmp/shiki-runner-claude-wrong-runtime.out 2>&1; then
  echo "runner codex unexpectedly accepted a claude-code task" >&2
  exit 1
fi
grep "assigned to claude-code, not codex" /tmp/shiki-runner-claude-wrong-runtime.out >/dev/null

echo "shiki runner claude tests passed"
