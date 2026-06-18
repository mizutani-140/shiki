#!/usr/bin/env bash
# =============================================================================
# Shiki end-to-end loop self-drive contract test (PRD 0002 T5)
# =============================================================================
#
# T1-T4 are MERGED. With a stubbed Claude runner, a stubbed read-only reviewer,
# and a stubbed gh, this drives the FULL integrated autonomous path with no
# operator input — and asserts the loop-owned evidence the integrated loop must
# produce, IN THE IMPLEMENTED ORDER.
#
# PATH 1 — self-drive to auto-merge (the create_pr branch of execute_action):
#     dispatch
#       -> (a) independent code-review gate (T3)   [stop_blocked if not clean]
#       -> (b) loop-observed TDD gate (T2)         [stop_blocked if RED]
#       -> (c) commit/push implementation (T1)
#       -> create_pr -> green checks -> auto-merge -> done -> goal complete
#   The code-review gate runs BEFORE the TDD gate, so a blocking review
#   short-circuits before any test run. A non-clean PRE-PR review (a blocking
#   verdict, or a parse/dispatch failure) FAILS CLOSED to stop_blocked: no PR
#   exists yet to anchor a repair packet, so the loop stops for diagnosis — it
#   does NOT dispatch a repair. This test asserts both loop-owned ledgers exist
#   AND that the code-review ledger precedes the TDD-gate ledger in the task's
#   ledger_evidence (so a future reorder breaks the test).
#
# PATH 2 — failure -> bounded repair (POST-PR required-check failure): a red
#   required check on a task that already has a PR dispatches a bounded repair
#   through the runner (repair packet + repair handoff). This is the post-PR
#   repair path; it is DISTINCT from the pre-PR review gate above, which
#   stop_blocks rather than repairs.
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
# The stub answers the two dispatch shapes the integrated loop uses:
#   * implementer: claude -p --permission-mode bypassPermissions   (writes work)
#   * reviewer (T3): claude -p --tools ... --disallowedTools ... --allowedTools
#     ... --json-schema ...  (read-only; emits a structured verdict). The loop
#     dispatches this before create_pr and parses the verdict; the stub emits a
#     clean verdict so the self-drive happy path stays green.
cat >"$FAKE_BIN/claude" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
# Reviewer invocation (T3): the loop dispatches a read-only reviewer with a
# restricted tool set and a --json-schema contract. Detect it by the read-only
# --allowedTools arm and emit a clean (zero-finding) verdict so the happy path
# stays green; the verdict schema is {"verdict":"clean"|"blocking", ...}.
for arg in "$@"; do
  if [[ "$arg" == "--allowedTools" ]]; then
    echo '{"verdict":"clean","findings":[]}'
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
      "test_command": "true",
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

# --- (T2) loop-observed TDD + (T3) independent code-review evidence ---------
# T2/T3/T4 are merged: the self-drive above ran BOTH loop-owned gates before
# opening the PR. The IMPLEMENTED create_pr ORDER is:
#     (a) independent code-review gate  (T3) -> stop_blocked if not clean
#     (b) loop-observed TDD gate        (T2) -> stop_blocked if RED
#     (c) commit/push                   (T1)
#     (d) create_pr
#     (e) sync_state
# i.e. CODE-REVIEW runs BEFORE the TDD gate (a blocking review short-circuits
# before any test run). Assert both loop-owned check ledgers exist AND that the
# code-review ledger was recorded BEFORE the tdd ledger, so a future reorder of
# execute_action's create_pr branch breaks this test.
FIRST_TASK="$(python3 -c 'import json;print(json.load(open("/tmp/shiki-loop-e2e-run.json"))["task_ids"][0])')"
python3 - "$TARGET" "$FIRST_TASK" <<'PY'
import json, os, sys
target, task_id = sys.argv[1], sys.argv[2]
task = json.load(open(os.path.join(target, ".shiki", "tasks", task_id + ".json")))
ledgers = []
for lid in task.get("ledger_evidence", []):
    p = os.path.join(target, ".shiki", "ledger", lid + ".json")
    if os.path.exists(p):
        ledgers.append(json.load(open(p)))


def has(pred):
    return any(pred(l) for l in ledgers)


# T2: the loop-observed TDD-GATE check ledger (distinct from the dispatch
# runner's EXEC ledger, which also carries EXEC evidence) — match its summary.
def is_tdd_gate(l):
    return (l.get("type") == "check"
            and "loop-observed tdd gate" in str(l.get("summary", "")).lower()
            and any(str(e).startswith(".shiki/runner/EXEC-") for e in (l.get("evidence") or [])))


assert has(is_tdd_gate), \
    "T2: no loop-observed TDD-gate check ledger with EXEC evidence on the merged task"
# T3: an independent pre-PR code-review check ledger.
assert has(lambda l: l.get("type") == "check"
           and "code-review" in json.dumps(l).lower()
           and "reviewer" in json.dumps(l).lower()), \
    "T3: no independent code-review check ledger on the merged task"


# ORDER: the loop appends each gate's ledger to ledger_evidence in creation
# order (no sort), so the code-review ledger's index must come BEFORE the tdd
# ledger's index — proving the implemented (a) review -> (b) TDD sequence, not
# just that both exist. Timestamps are second-granular and can tie, so index
# order (creation order) is the reliable signal.
evidence = task.get("ledger_evidence", [])
by_id = {l.get("id"): l for l in ledgers}


def first_index(pred):
    for i, lid in enumerate(evidence):
        l = by_id.get(lid)
        if l is not None and pred(l):
            return i
    return None


review_i = first_index(lambda l: l.get("type") == "check" and "reviewer" in json.dumps(l).lower())
tdd_i = first_index(is_tdd_gate)
assert review_i is not None and tdd_i is not None and review_i < tdd_i, \
    f"create_pr order: code-review (idx {review_i}) must precede the TDD gate (idx {tdd_i})"
print("e2e: loop-owned T3 code-review THEN T2 tdd-evidence ledgers, in the implemented order")
PY

# ===========================================================================
# PATH 2 (ASSERTS NOW): failure -> repair. A red required check on a task in
# review dispatches a bounded repair through the runner (dispatch_repair),
# writing a repair packet + repair handoff into the materialized worktree.
# This is the POST-PR repair branch; it is DISTINCT from the pre-PR review
# gate, which stop_blocks (no PR yet to anchor a repair) rather than repairs.
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

echo "shiki loop e2e: integrated self-drive (code-review -> TDD -> commit/push -> auto-merge) + post-PR repair verified"
