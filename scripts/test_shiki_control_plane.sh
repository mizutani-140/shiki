#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-control-plane-test-$$"
TARGET="$TMP_ROOT/target"

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
python3 scripts/shiki.py --help | grep -E "goal|issue|dispatch|repair" >/dev/null
python3 scripts/shiki.py runner --help | grep "codex" >/dev/null
python3 scripts/shiki.py goal --help | grep "complete" >/dev/null
python3 scripts/shiki.py issue --help | grep "plan" >/dev/null
grep "goal create" .codex/skills/shiki/SKILL.md >/dev/null
grep "Register durable state" .claude/commands/shiki.md >/dev/null
grep "shiki runner codex --target TARGET --task-id T-XXXX" .claude/commands/shiki.md >/dev/null
grep "manual \`codex\` command" .codex/skills/shiki/SKILL.md >/dev/null
grep "operator's requested Target Repository" .claude/commands/shiki.md >/dev/null
grep "not automatically the requested Target" .codex/skills/shiki/SKILL.md >/dev/null
grep -- "--max-turns 60" .github/workflows/shiki-claude-review.yml >/dev/null

mkdir -p "$TARGET"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-control-install.out

cd "$TARGET"
git init -b main >/tmp/shiki-control-git-init.out
# Hermetic git identity so `git commit` works in CI where no global git user is configured.
git config user.email "shiki-test@example.com"
git config user.name "Shiki Test"
git remote add origin https://github.com/example/shiki-control-plane-test.git

python3 "$ROOT/scripts/shiki.py" goal create \
  --target "$TARGET" \
  --title "Ship searchable audit trail" \
  --outcome "Operators can search task evidence from GitHub PR records" \
  --completion-condition "All task slices have done status" \
  --completion-condition "CCA and MergeGate evidence exists" \
  --required-skill grill-with-docs \
  --required-skill tdd \
  >/tmp/shiki-goal-create.json

GOAL_ID="$(json_get /tmp/shiki-goal-create.json goal_id)"
test -f "$TARGET/.shiki/goals/$GOAL_ID.json"

python3 "$ROOT/scripts/shiki.py" issue plan \
  --target "$TARGET" \
  --goal-id "$GOAL_ID" \
  --title "Search audit evidence by task" \
  --scope "Add the smallest vertical slice for searching task evidence" \
  --acceptance-check "A user can query task evidence by task id" \
  --acceptance-check "Verification command records evidence" \
  --lock "path:src/audit/*" \
  --required-skill tdd \
  >/tmp/shiki-issue-plan.json

TASK_ID="$(json_get /tmp/shiki-issue-plan.json task_id)"
test -f "$TARGET/.shiki/tasks/$TASK_ID.json"
test -f "$TARGET/.shiki/dag/$GOAL_ID.json"

python3 "$ROOT/scripts/shiki.py" lock acquire --target "$TARGET" "$TASK_ID" >/tmp/shiki-lock.json
python3 "$ROOT/scripts/shiki.py" dispatch check --target "$TARGET" "$TASK_ID" >/tmp/shiki-dispatch.json
python3 "$ROOT/scripts/shiki.py" worktree allocate --target "$TARGET" "$TASK_ID" >/tmp/shiki-worktree.json
test -f "$TARGET/.shiki/worktrees/$TASK_ID.json"

python3 "$ROOT/scripts/shiki.py" repair packet \
  --target "$TARGET" \
  --task-id "$TASK_ID" \
  --pr 123 \
  --failing-item "missing verification evidence" \
  --minimal-change "add the requested verification evidence only" \
  --required-skill evidence-only \
  --verification-command "python3 scripts/validate_shiki.py" \
  >/tmp/shiki-repair.json
python3 -c 'import json; out=json.load(open("/tmp/shiki-repair.json")); packet=json.load(open(out["repair_file"])); assert packet["required_skill"] == "evidence-only"'

python3 "$ROOT/scripts/shiki.py" task status --target "$TARGET" "$TASK_ID" --status done >/tmp/shiki-task-status.json
python3 "$ROOT/scripts/shiki.py" goal complete --target "$TARGET" "$GOAL_ID" >/tmp/shiki-goal-complete.json

python3 "$TARGET/scripts/validate_shiki.py"

echo "shiki control-plane tests passed"
