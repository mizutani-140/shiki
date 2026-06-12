#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-runner-codex-test-$$"
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
python3 scripts/shiki.py runner --help | grep "codex" >/dev/null

mkdir -p "$TARGET" "$FAKE_BIN"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-runner-codex-install.out

cd "$TARGET"
git init -b main >/tmp/shiki-runner-codex-git-init.out
git remote add origin https://github.com/example/shiki-runner-codex-test.git
git add .
git -c user.name="Shiki Test" -c user.email="shiki@example.test" commit -m "init" >/tmp/shiki-runner-codex-commit.out

cat >"$TMP_ROOT/plan.json" <<'JSON'
{
  "title": "Ship autonomous Codex dispatch",
  "outcome": "A ready task can be executed through Codex without asking the user to run a command",
  "completion_conditions": ["Codex runner evidence exists"],
  "non_goals": ["Do not use a real Codex session in this test"],
  "risk_level": "low",
  "required_skills": ["grill-with-docs", "tdd"],
  "grill_with_docs": {
    "status": "complete",
    "source": "CONTEXT.md",
    "decisions": ["Use Codex as implementation runtime"]
  },
  "spec_freeze": {
    "status": "frozen",
    "approved_by": "operator",
    "source": "test fixture"
  },
  "tasks": [
    {
      "title": "Write Codex marker",
      "scope": "Create the smallest Codex-visible implementation task",
      "acceptance_checks": ["Codex fake writes a marker in the materialized worktree"],
      "locks": ["path:codex-marker.txt"],
      "runtime": "codex",
      "required_skills": ["tdd"]
    }
  ]
}
JSON

python3 "$ROOT/scripts/shiki.py" plan ingest --target "$TARGET" --plan-file "$TMP_ROOT/plan.json" >/tmp/shiki-runner-codex-plan.json
PLAN_ID="$(json_get /tmp/shiki-runner-codex-plan.json plan_id)"
python3 "$ROOT/scripts/shiki.py" run --target "$TARGET" --plan "$PLAN_ID" >/tmp/shiki-runner-codex-run.json
python3 "$ROOT/scripts/shiki.py" runner next --target "$TARGET" >/tmp/shiki-runner-codex-next.json
TASK_ID="$(json_get /tmp/shiki-runner-codex-next.json task_id)"
python3 "$ROOT/scripts/shiki.py" handoff task --target "$TARGET" "$TASK_ID" >/tmp/shiki-runner-codex-handoff.json
test -f "$TARGET/.shiki/handoffs/$TASK_ID-task.md"

cat >"$FAKE_BIN/codex" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  --version)
    echo "codex-cli 0.135.0"
    exit 0
    ;;
  login)
    if [[ "${2:-}" == "status" ]]; then
      echo "Logged in using ChatGPT"
      exit 0
    fi
    ;;
  exec)
    pwd > codex-marker.txt
    if [[ "${2:-}" == "-" ]]; then
      cat > codex-prompt.txt
    else
      printf "%s" "${2:-}" > codex-prompt.txt
    fi
    echo "codex fake executed"
    exit 0
    ;;
esac
echo "fake codex unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/codex"
export PATH="$FAKE_BIN:$PATH"

python3 "$ROOT/scripts/shiki.py" runner codex --target "$TARGET" --task-id "$TASK_ID" >/tmp/shiki-runner-codex-execute.json
WORKTREE="$(json_get /tmp/shiki-runner-codex-execute.json worktree)"
test -f "$WORKTREE/codex-marker.txt"
grep "$TASK_ID" "$WORKTREE/codex-prompt.txt" >/dev/null
grep '"status": "review"' "$TARGET/.shiki/tasks/$TASK_ID.json" >/dev/null
grep "codex exec" "$TARGET"/.shiki/runner/EXEC-*.json >/dev/null

echo "shiki runner codex tests passed"
