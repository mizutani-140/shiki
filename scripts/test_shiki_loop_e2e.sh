#!/usr/bin/env bash
# =============================================================================
# Shiki end-to-end loop self-drive contract test  --  SKELETON (PRD 0002 T5)
# =============================================================================
#
# Goal of the finished test: with a stubbed Claude runner AND a stubbed
# read-only reviewer, drive the FULL autonomous path with no operator input:
#
#     dispatch -> tdd-evidence(loop-observed) -> code-review(independent)
#       -> commit/push -> create_pr -> green checks -> auto-merge -> done
#       -> goal complete
#
# ...plus the failure -> repair branch (a red required check dispatches a
# bounded repair through the runner).
#
# HONEST STATUS  (round-1 finding: do not over-promise).
#
#   This is a SKELETON. The tdd-evidence step (T2) and the independent pre-PR
#   code-review step (T3) are NOT merged yet, so this file does NOT yet drive
#   the full T2/T3 path end-to-end. It is deliberately written to FAIL CLOSED
#   on the parts that exist and to mark the not-yet-wired seams with greppable
#   TODO(T2)/TODO(T3) markers so it can be completed by deleting TODO blocks,
#   not rewritten.
#
#   ASSERTS NOW (runs green today against main + this worktree):
#     * the loop self-drives a frozen low-risk goal to auto-merge + goal
#       complete with zero operator input (dispatch -> create_pr -> green
#       checks -> merge -> done -> goal complete), via `shiki loop run`;
#     * the failure -> repair branch fires: a red required check produces a
#       dispatch_repair action and a repair packet + repair handoff;
#     * the stub runner ("claude") and stub reviewer are wired and callable so
#       the T2/T3 seams have something to dispatch into once they land.
#
#   TODO (deferred until T2/T3 merge -- each marked inline below):
#     * TODO(T2): assert the loop ran the task's tests in the worktree and
#       recorded a type:check ledger naming skill `tdd` with an EXEC evidence
#       ref, and that a RED test run blocks (no PR, stop_blocked), and that
#       the per-task `test_command` field defaults to unittest-discover.
#       Depends on T2 inserting that step at the TOP of execute_action's
#       create_pr branch (before create_github_pr_for_task) and threading a
#       "## TDD evidence (loop-observed)" line into github_pr_body.
#     * TODO(T3): assert an independent read-only reviewer (claude -p with
#       --allowedTools restricted to read tools + --json-schema) is dispatched
#       BEFORE create_pr, that its verdict is parsed deterministically, that a
#       code-review ledger is recorded, that the "## Pre-PR code review"
#       PR-body section is written into github_pr_body, and that a blocking
#       verdict (or a parse/dispatch failure -- fail-closed) routes into
#       dispatch_repair rather than opening the PR.
#
# INTEGRATION CONTRACT (the exact sibling-owned surfaces this test depends on):
#     * scripts/shiki_loop.py :: execute_action   -- create_pr branch; T2 adds
#       the tdd-evidence step, T3 adds the reviewer step, both BEFORE the
#       existing _commit_and_push_implementation + create_github_pr_for_task.
#     * scripts/shiki_github.py :: github_pr_body  -- T2 adds the
#       "## TDD evidence (loop-observed)" section; T3 adds the
#       "## Pre-PR code review" section.
#     * scripts/shiki_tasks.py                     -- T2 adds the per-task
#       `test_command` field (default: python3 -m unittest discover -s tests).
#     * scripts/shiki_runtime_adapters.py          -- T3 adds the read-only
#       reviewer adapter distinct from the bypassPermissions implementer.
#
# Model: scripts/test_shiki_goal_loop.sh + scripts/test_shiki_runner_claude.sh.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-loop-e2e-test-$$"
TARGET="$TMP_ROOT/target"
FAKE_BIN="$TMP_ROOT/bin"
export GH_STATE="$TMP_ROOT/gh-state"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

json_get() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

# Loop outputs stream one JSON document per executed control-plane command; the
# loop's own result is always the last document.
json_get_last() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

decoder = json.JSONDecoder()
text = open(sys.argv[1]).read().strip()
objects = []
index = 0
while index < len(text):
    obj, end = decoder.raw_decode(text, index)
    objects.append(obj)
    while end < len(text) and text[end] in " \r\n\t":
        end += 1
    index = end
print(objects[-1][sys.argv[2]])
PY
}

cd "$ROOT"
python3 scripts/validate_shiki.py
python3 scripts/shiki.py loop --help | grep "step" >/dev/null

# --- Stage a real installed target with a pushable origin -------------------
mkdir -p "$TARGET" "$FAKE_BIN" "$GH_STATE"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-loop-e2e-install.out

cd "$TARGET"
git init -b main >/tmp/shiki-loop-e2e-git-init.out
git config user.name "Shiki Test"
git config user.email "shiki@example.test"
git init --bare "$TMP_ROOT/origin.git" >/dev/null
# origin keeps a GitHub fetch URL (Shiki requires a GitHub origin) but pushes go
# to a real local bare repo so the loop's create_pr commit+push step succeeds.
git remote add origin https://github.com/example/shiki-loop-e2e-test.git
git remote set-url --push origin "$TMP_ROOT/origin.git"
git add .
git commit -m "init" >/tmp/shiki-loop-e2e-commit.out
git push -u origin main >/dev/null 2>&1

# --- Stub implementer runtime ("claude") -----------------------------------
# The stub answers the two dispatch shapes the loop uses:
#   * implementer:  claude -p --permission-mode bypassPermissions   (writes work)
#   * TODO(T3) reviewer: claude -p --allowedTools <read-only> --json-schema ...
#     (read-only; emits a structured findings verdict). The reviewer branch is
#     stubbed here so the seam is wired; T3 will make the loop actually dispatch
#     it and parse the verdict. Until then this branch is unused by the loop.
cat >"$FAKE_BIN/claude" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
# TODO(T3): once T3 lands, the loop dispatches a read-only reviewer with
# `--allowedTools` restricted to read tools and a `--json-schema` contract.
# Detect that invocation and emit a clean (zero-blocking-findings) verdict so
# the happy path stays green; a separate fixture run will force a blocking
# verdict to exercise the verdict -> repair packet path.
for arg in "$@"; do
  if [[ "$arg" == "--allowedTools" ]]; then
    echo '{"verdict":"pass","blocking_findings":[],"findings":[]}'
    exit 0
  fi
done
case "${1:-}" in
  --version) echo "2.0.0 (Claude Code)"; exit 0 ;;
  auth)
    if [[ "${2:-}" == "status" ]]; then
      echo '{"loggedIn": true, "authMethod": "claude_subscription_oauth", "apiProvider": "anthropic"}'
      exit 0
    fi ;;
  -p)
    cat > "claude-prompt-$$.txt"
    cp "claude-prompt-$$.txt" claude-last-prompt.txt
    echo "claude fake executed"
    exit 0 ;;
esac
echo "fake claude unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/claude"

# --- Stub GitHub ------------------------------------------------------------
cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
STATE="${GH_STATE:?}"
case "${1:-} ${2:-}" in
  "pr create")
    COUNTER_FILE="$STATE/pr-counter"
    NUMBER=$(( $(cat "$COUNTER_FILE" 2>/dev/null || echo 10) + 1 ))
    echo "$NUMBER" > "$COUNTER_FILE"
    echo "https://github.com/example/shiki-loop-e2e-test/pull/$NUMBER"
    exit 0 ;;
  "pr view")
    NUMBER="$3"
    if [[ -f "$STATE/merged-$NUMBER" ]]; then
      echo '{"state":"MERGED","mergedAt":"2026-06-17T00:00:00Z"}'
    else
      echo '{"state":"OPEN","mergedAt":null}'
    fi
    exit 0 ;;
  "pr checks")
    NUMBER="$3"
    if [[ -f "$STATE/checks-$NUMBER.json" ]]; then
      cat "$STATE/checks-$NUMBER.json"
    else
      echo '[{"name":"Validate Shiki mirror","bucket":"pass"},{"name":"CCA verdict","bucket":"pass"},{"name":"MergeGate metadata check","bucket":"pass"},{"name":"MergeGate policy check","bucket":"pass"}]'
    fi
    exit 0 ;;
  "pr merge")
    NUMBER="$3"
    touch "$STATE/merged-$NUMBER"
    echo "merged $NUMBER" >> "$STATE/gh-log"
    exit 0 ;;
  "run list")
    echo '[]'
    exit 0 ;;
  "run rerun")
    echo "rerun ${3:-}" >> "$STATE/gh-log"
    exit 0 ;;
esac
echo "fake gh unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/gh"
export PATH="$FAKE_BIN:$PATH"

# ===========================================================================
# PATH 1 (ASSERTS NOW): self-drive a frozen low-risk goal to auto-merge.
# dispatch -> create_pr (commit/push + PR) -> green checks -> merge -> done
# -> goal complete, with zero operator input.
# ===========================================================================
cat >"$TMP_ROOT/plan.json" <<'JSON'
{
  "title": "End-to-end self-drive of a frozen low-risk goal",
  "outcome": "A single low-risk slice merges through the loop with no operator input",
  "completion_conditions": ["The slice merges through the loop and the goal completes"],
  "non_goals": ["No real GitHub calls in this test"],
  "risk_level": "low",
  "required_skills": ["grill-with-docs", "tdd"],
  "grill_with_docs": {
    "status": "complete",
    "source": "CONTEXT.md",
    "decisions": ["The loop drives frozen goals (ADR 0008/0009/0011)"]
  },
  "spec_freeze": {
    "status": "frozen",
    "approved_by": "operator",
    "source": "test fixture"
  },
  "tasks": [
    {
      "title": "Only slice",
      "scope": "Smallest end-to-end slice",
      "acceptance_checks": ["Slice verified"],
      "locks": ["path:slice-one.txt"],
      "required_skills": ["tdd"]
    }
  ]
}
JSON

python3 "$ROOT/scripts/shiki.py" plan ingest --target "$TARGET" --plan-file "$TMP_ROOT/plan.json" >/tmp/shiki-loop-e2e-plan.json
PLAN_ID="$(json_get /tmp/shiki-loop-e2e-plan.json plan_id)"
python3 "$ROOT/scripts/shiki.py" run --target "$TARGET" --plan "$PLAN_ID" >/tmp/shiki-loop-e2e-run.json
GOAL_ID="$(json_get /tmp/shiki-loop-e2e-run.json goal_id)"

python3 "$ROOT/scripts/shiki.py" loop run --target "$TARGET" --goal-id "$GOAL_ID" --max-cycles 10 --interval 0 >/tmp/shiki-loop-e2e-result.json
test "$(json_get_last /tmp/shiki-loop-e2e-result.json outcome)" = "complete"
grep '"status": "complete"' "$TARGET/.shiki/goals/$GOAL_ID.json" >/dev/null
test "$(grep -c merged "$GH_STATE/gh-log")" = "1"

# ---------------------------------------------------------------------------
# TODO(T2): once scripts/shiki_loop.py execute_action's create_pr branch runs
# the task's tests in the worktree (before create_github_pr_for_task) and
# records a type:check ledger naming skill `tdd` with an EXEC evidence ref,
# assert that ledger exists for the merged task and that github_pr_body carried
# the "## TDD evidence (loop-observed)" section. Also add a sibling fixture
# where the worktree test_command exits non-zero and assert the loop returns
# action=stop_blocked WITHOUT opening a PR (fail-closed; dispatch_repair is
# PR-gated). The per-task `test_command` field (scripts/shiki_tasks.py) must
# default to: python3 -m unittest discover -s tests
# ---------------------------------------------------------------------------
#
# TODO(T3): once scripts/shiki_loop.py dispatches the independent read-only
# reviewer (claude -p --allowedTools <read tools> --json-schema ...) before
# create_pr, assert: (a) a code-review ledger is recorded for the merged task;
# (b) github_pr_body carried the "## Pre-PR code review" section; (c) a fixture
# that forces a blocking reviewer verdict routes into dispatch_repair instead of
# create_pr; (d) a fixture where the reviewer dispatch/parse FAILS is
# fail-closed (blocks, never silently passes). The stub reviewer branch in the
# fake `claude` above (the `--allowedTools` arm) is the seam to drive.
# ---------------------------------------------------------------------------

# ===========================================================================
# PATH 2 (ASSERTS NOW): failure -> repair. A red required check on a task in
# review dispatches a bounded repair through the runner (dispatch_repair),
# writing a repair packet + repair handoff into the materialized worktree.
# This is the same repair branch a T3 blocking review verdict will feed into.
# ===========================================================================
python3 "$ROOT/scripts/shiki.py" goal create --target "$TARGET" --title "Repair gate" --outcome "Failed check repairs" >/tmp/shiki-loop-e2e-repair-goal.json
RGOAL="$(json_get /tmp/shiki-loop-e2e-repair-goal.json goal_id)"
python3 "$ROOT/scripts/shiki.py" issue plan --target "$TARGET" --goal-id "$RGOAL" \
  --title "Repairable slice" --scope "Fails validate" \
  --acceptance-check "Validate passes" >/tmp/shiki-loop-e2e-repair-task.json
RTASK="$(json_get /tmp/shiki-loop-e2e-repair-task.json task_id)"
python3 "$ROOT/scripts/shiki.py" lock acquire --target "$TARGET" "$RTASK" >/dev/null
python3 "$ROOT/scripts/shiki.py" worktree allocate --target "$TARGET" "$RTASK" >/dev/null
python3 "$ROOT/scripts/shiki.py" handoff task --target "$TARGET" "$RTASK" >/dev/null
python3 - "$TARGET/.shiki/tasks/$RTASK.json" <<'PY'
import json
import sys

path = sys.argv[1]
task = json.load(open(path))
task["status"] = "review"
task["expected_pr"] = 42
json.dump(task, open(path, "w"), indent=2)
PY
cat >"$GH_STATE/checks-42.json" <<'JSON'
[{"name":"Validate Shiki mirror","bucket":"fail"},{"name":"CCA verdict","bucket":"pass"},{"name":"MergeGate metadata check","bucket":"pass"},{"name":"MergeGate policy check","bucket":"pass"}]
JSON
python3 "$ROOT/scripts/shiki.py" loop step --target "$TARGET" --goal-id "$RGOAL" >/tmp/shiki-loop-e2e-repair.json
test "$(json_get_last /tmp/shiki-loop-e2e-repair.json action)" = "dispatch_repair"
REPAIR_ID="$(json_get_last /tmp/shiki-loop-e2e-repair.json repair_id)"
test -f "$TARGET/.shiki/repairs/$REPAIR_ID.json"
test -f "$TARGET/.shiki/handoffs/$REPAIR_ID-repair.md"
WORKTREE_DIR="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["path"])' "$TARGET/.shiki/worktrees/$RTASK.json")"
grep "$REPAIR_ID" "$WORKTREE_DIR/claude-last-prompt.txt" >/dev/null

echo "shiki loop e2e skeleton tests passed (T2/T3 seams TODO; see header)"
