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
# Isolate HOME so the default-branch resolver's config lookup
# (load_default_config reads ~/.shiki/config.json) cannot read the operator's
# real global config. Without this the resolver could pick up a non-`main`
# default_branch and abort the throwaway target's dispatch with an unrelated
# error, making this test depend on machine state. Empty HOME -> "main".
export HOME="$TMP_ROOT/home"
mkdir -p "$HOME"
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

# Regression (T-20260729T065622769632Z-9ee6b8e0): the coordinator moves onto a
# feature branch carrying a foreign goal's mirror state before dispatch. A task
# worktree must still be cut from the default branch (main), not from HEAD --
# otherwise the foreign file rides into the task PR's diff (the 56-blocking-
# reason failure). `shiki runner claude` alone must produce the correctly based
# worktree; no operator `git worktree add` is needed.
MAIN_TIP="$(git rev-parse main)"
git checkout -b coordinator-feature >/dev/null 2>&1
mkdir -p "$TARGET/.shiki/goals"
echo '{"id": "G-FOREIGN0000"}' >"$TARGET/.shiki/goals/G-FOREIGN0000.json"
git add -A
git -c user.name="Shiki Test" -c user.email="shiki@example.test" commit -m "foreign goal mirror state" >/dev/null

# Dry run shows the dispatch without executing it.
python3 "$ROOT/scripts/shiki.py" runner claude --target "$TARGET" --task-id "$TASK_ID" --dry-run >/tmp/shiki-runner-claude-dry.json
grep "claude -p" /tmp/shiki-runner-claude-dry.json >/dev/null
test ! -f "$TARGET/claude-marker.txt"

python3 "$ROOT/scripts/shiki.py" runner claude --target "$TARGET" --task-id "$TASK_ID" >/tmp/shiki-runner-claude-execute.json
WORKTREE="$(json_get /tmp/shiki-runner-claude-execute.json worktree)"
GOAL_ID="$(json_get /tmp/shiki-runner-claude-next.json goal_id)"
test -f "$WORKTREE/claude-marker.txt"
grep "$TASK_ID" "$WORKTREE/claude-prompt.txt" >/dev/null
grep '"status": "review"' "$TARGET/.shiki/tasks/$TASK_ID.json" >/dev/null
grep "claude -p" "$TARGET"/.shiki/runner/EXEC-*.json >/dev/null

# The dispatch carried THIS task's current contract into the worktree before the
# session (the amended-contract gap): the worktree's task file, goal file and
# lock record are synced from the coordinator, so a repair judges against current
# terms rather than the stale copy the branch was cut with. The worktree was cut
# from main, which lacks these locally-created mirror files; only the pre-session
# sync puts them there, so their presence proves the wiring.
test -f "$WORKTREE/.shiki/tasks/$TASK_ID.json"
test -f "$WORKTREE/.shiki/goals/$GOAL_ID.json"
test -f "$WORKTREE/.shiki/locks/$TASK_ID.json"
grep '"path:claude-marker.txt"' "$WORKTREE/.shiki/tasks/$TASK_ID.json" >/dev/null

# The worktree was cut from main even though the coordinator sat on
# coordinator-feature: its tip equals main's tip and the foreign goal file the
# feature branch carries is absent from the worktree — the contract sync carries
# only this task's own goal, never a foreign one.
test "$(git -C "$WORKTREE" rev-parse HEAD)" = "$MAIN_TIP"
test ! -f "$WORKTREE/.shiki/goals/G-FOREIGN0000.json"

# A claude-code task must not dispatch through the codex runner without --force.
if python3 "$ROOT/scripts/shiki.py" runner codex --target "$TARGET" --task-id "$TASK_ID" --dry-run >/tmp/shiki-runner-claude-wrong-runtime.out 2>&1; then
  echo "runner codex unexpectedly accepted a claude-code task" >&2
  exit 1
fi
grep "assigned to claude-code, not codex" /tmp/shiki-runner-claude-wrong-runtime.out >/dev/null

echo "shiki runner claude tests passed"
