#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-goal-loop-test-$$"
TARGET="$TMP_ROOT/target"
FAKE_BIN="$TMP_ROOT/bin"
export GH_STATE="$TMP_ROOT/gh-state"
export ORIGIN_GIT="$TMP_ROOT/origin.git"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

json_get() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

# Loop outputs stream one JSON document per executed control-plane command;
# the loop's own result is always the last document.
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

mkdir -p "$TARGET" "$FAKE_BIN" "$GH_STATE"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-goal-loop-install.out

cd "$TARGET"
git init -b main >/tmp/shiki-goal-loop-git-init.out
# Configure a repo user so the goal loop's create_pr commit+push step (which
# commits the implementer's worktree work) can commit, and a real bare origin so
# its `git push` succeeds (the loop now pushes the implementation to the branch).
git config user.name "Shiki Test"
git config user.email "shiki@example.test"
git init --bare "$TMP_ROOT/origin.git" >/dev/null
# origin keeps a GitHub fetch URL (Shiki requires a GitHub origin) but pushes go
# to a real local bare repo so the loop's create_pr commit+push step succeeds.
git remote add origin https://github.com/example/shiki-goal-loop-test.git
git remote set-url --push origin "$TMP_ROOT/origin.git"
git add .
git commit -m "init" >/tmp/shiki-goal-loop-commit.out
git push -u origin main >/dev/null 2>&1

cat >"$FAKE_BIN/claude" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  --version) echo "2.0.0 (Claude Code)"; exit 0 ;;
  auth)
    if [[ "${2:-}" == "status" ]]; then
      echo '{"loggedIn": true, "authMethod": "claude_subscription_oauth", "apiProvider": "anthropic"}'
      exit 0
    fi ;;
  -p)
    # The independent pre-PR reviewer (ADR 0011) carries --json-schema and
    # read-only --allowedTools; the implementer carries bypassPermissions.
    # Distinguish them and emit a clean structured verdict for the reviewer so
    # the loop's pre-PR code-review gate passes (a parse failure would fail
    # closed to stop_blocked).
    if printf '%s\n' "$@" | grep -q -- '--json-schema'; then
      cat >/dev/null
      echo '{"verdict":"clean","findings":[],"summary":"fake reviewer: no findings"}'
      exit 0
    fi
    cat > "claude-prompt-$$.txt"
    cp "claude-prompt-$$.txt" claude-last-prompt.txt
    echo "claude fake executed"
    exit 0 ;;
esac
echo "fake claude unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/claude"

cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
STATE="${GH_STATE:?}"
case "${1:-} ${2:-}" in
  "pr create")
    HEAD_BRANCH=""; prev=""
    for a in "$@"; do
      [[ "$prev" == "--head" ]] && HEAD_BRANCH="$a"
      prev="$a"
    done
    COUNTER_FILE="$STATE/pr-counter"
    NUMBER=$(( $(cat "$COUNTER_FILE" 2>/dev/null || echo 10) + 1 ))
    echo "$NUMBER" > "$COUNTER_FILE"
    echo "$HEAD_BRANCH" > "$STATE/branch-$NUMBER"
    echo "https://github.com/example/shiki-goal-loop-test/pull/$NUMBER"
    exit 0 ;;
  "pr list")
    # The closeout effector probes for an existing closeout PR by --head branch.
    echo '[]'
    exit 0 ;;
  "pr view")
    NUMBER="$3"
    # mergeStateStatus defaults to CLEAN; a fixture writes merge-state-<N> to drive
    # the BEHIND (base-moved) path the loop now syncs before merging.
    MSTATE="CLEAN"
    [[ -f "$STATE/merge-state-$NUMBER" ]] && MSTATE="$(cat "$STATE/merge-state-$NUMBER")"
    if [[ -f "$STATE/merged-$NUMBER" ]]; then
      echo "{\"state\":\"MERGED\",\"mergedAt\":\"2026-06-12T00:00:00Z\",\"mergeStateStatus\":\"$MSTATE\"}"
    else
      echo "{\"state\":\"OPEN\",\"mergedAt\":null,\"mergeStateStatus\":\"$MSTATE\"}"
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
    # Faithfully advance origin/main so the next closeout worktree (cut from
    # origin/main) sees the merged impl/closeout state (ADR 0012). The branch is a
    # fast-forward of main (built on it), so move the ref to the branch tip.
    BR="$(cat "$STATE/branch-$NUMBER" 2>/dev/null || echo "")"
    if [[ -n "$BR" ]]; then
      git -C "$ORIGIN_GIT" update-ref "refs/heads/main" "refs/heads/$BR"
      # Also advance the coordinator's remote-tracking ref so the closeout worktree
      # (added from origin/main) sees the merged state. The effector's real
      # `git fetch` reaches GitHub; here the fetch URL is a stub, so simulate it.
      git update-ref "refs/remotes/origin/main" "refs/remotes/origin/$BR" 2>/dev/null || true
    fi
    exit 0 ;;
  "run list")
    # The CCA-verdict resolver and rerun_cca both list this workflow. Serve a
    # canned failed run only when a fixture asks for it (DEFECT A); default empty.
    if [[ -f "$STATE/cca-run.json" ]]; then
      cat "$STATE/cca-run.json"
    else
      echo '[]'
    fi
    exit 0 ;;
  "run download")
    # Resolve the CCA verdict artifact read-only: copy the fixture verdict into
    # the resolver's --dir. Absent fixture -> a failed download (unresolvable).
    DIR=""; prev=""
    for a in "$@"; do
      [[ "$prev" == "--dir" ]] && DIR="$a"
      prev="$a"
    done
    if [[ -n "$DIR" && -f "$STATE/cca-verdict.json" ]]; then
      mkdir -p "$DIR"
      cp "$STATE/cca-verdict.json" "$DIR/cca-verdict.json"
      exit 0
    fi
    exit 1 ;;
  "run rerun")
    echo "rerun ${3:-}" >> "$STATE/gh-log"
    exit 0 ;;
esac
echo "fake gh unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/gh"
export PATH="$FAKE_BIN:$PATH"

cat >"$TMP_ROOT/plan.json" <<'JSON'
{
  "title": "Drive a frozen goal autonomously",
  "outcome": "A single task self-drives to auto-merge AND goal-complete on main",
  "completion_conditions": ["The task merges and the goal completes through the loop"],
  "non_goals": ["No real GitHub calls in this test"],
  "risk_level": "low",
  "required_skills": ["grill-with-docs", "tdd"],
  "grill_with_docs": {
    "status": "complete",
    "source": "CONTEXT.md",
    "decisions": ["Loop drives frozen goals (ADR 0008/0009); completion is pushed to main via a closeout PR (ADR 0012)"]
  },
  "spec_freeze": {
    "status": "frozen",
    "approved_by": "operator",
    "source": "test fixture"
  },
  "tasks": [
    {
      "title": "First slice",
      "scope": "Smallest end-to-end slice",
      "acceptance_checks": ["Slice one verified"],
      "locks": ["path:.shiki/**", "path:slice-one.txt"],
      "required_skills": ["tdd"],
      "test_command": "true"
    }
  ]
}
JSON

python3 "$ROOT/scripts/shiki.py" plan ingest --target "$TARGET" --plan-file "$TMP_ROOT/plan.json" >/tmp/shiki-goal-loop-plan.json
PLAN_ID="$(json_get /tmp/shiki-goal-loop-plan.json plan_id)"
python3 "$ROOT/scripts/shiki.py" run --target "$TARGET" --plan "$PLAN_ID" >/tmp/shiki-goal-loop-run.json
GOAL_ID="$(json_get /tmp/shiki-goal-loop-run.json goal_id)"

# The loop drives dispatch -> impl PR -> green checks -> auto-merge -> a closeout
# PR that pushes task=done + goal=complete to main (ADR 0012) -> auto-merge the
# closeout -> goal completion, with no operator input. Two merges: impl + closeout.
python3 "$ROOT/scripts/shiki.py" loop run --target "$TARGET" --goal-id "$GOAL_ID" --max-cycles 14 --interval 0 >/tmp/shiki-goal-loop-result.json
test "$(json_get_last /tmp/shiki-goal-loop-result.json outcome)" = "complete"
grep '"status": "complete"' "$TARGET/.shiki/goals/$GOAL_ID.json" >/dev/null
# The loop opened a closeout PR (ADR 0012): its history records create_closeout_pr.
grep '"action": "create_closeout_pr"' /tmp/shiki-goal-loop-result.json >/dev/null
# Impl PR + closeout PR both auto-merged.
test "$(grep -c merged "$GH_STATE/gh-log")" = "2"

# High/critical risk merges autonomously when all required checks are green:
# the "MergeGate policy check" required check IS the Guardian gate (it enforces
# guardian-policy.json — human approval OR external AI guardian review, ADR
# 0010), so green checks mean a recorded authority approved. The fake gh returns
# green checks, simulating that the Guardian gate passed.
python3 "$ROOT/scripts/shiki.py" goal create --target "$TARGET" --title "Guardian gate" --outcome "High risk merges via policy gate" >/tmp/shiki-goal-loop-guardian-goal.json
GGOAL="$(json_get /tmp/shiki-goal-loop-guardian-goal.json goal_id)"
python3 "$ROOT/scripts/shiki.py" issue plan --target "$TARGET" --goal-id "$GGOAL" \
  --title "High risk slice" --scope "Needs guardian" --risk-level high \
  --acceptance-check "Guardian approves" >/tmp/shiki-goal-loop-guardian-task.json
GTASK="$(json_get /tmp/shiki-goal-loop-guardian-task.json task_id)"
python3 - "$TARGET/.shiki/tasks/$GTASK.json" <<'PY'
import json
import sys

path = sys.argv[1]
task = json.load(open(path))
task["status"] = "review"
task["expected_pr"] = 99
json.dump(task, open(path, "w"), indent=2)
PY
python3 "$ROOT/scripts/shiki.py" loop step --target "$TARGET" --goal-id "$GGOAL" >/tmp/shiki-goal-loop-guardian.json
test "$(json_get_last /tmp/shiki-goal-loop-guardian.json action)" = "merge"

# A failed required check dispatches a bounded repair through the runner.
python3 "$ROOT/scripts/shiki.py" goal create --target "$TARGET" --title "Repair gate" --outcome "Failed check repairs" >/tmp/shiki-goal-loop-repair-goal.json
RGOAL="$(json_get /tmp/shiki-goal-loop-repair-goal.json goal_id)"
python3 "$ROOT/scripts/shiki.py" issue plan --target "$TARGET" --goal-id "$RGOAL" \
  --title "Repairable slice" --scope "Fails validate" \
  --acceptance-check "Validate passes" >/tmp/shiki-goal-loop-repair-task.json
RTASK="$(json_get /tmp/shiki-goal-loop-repair-task.json task_id)"
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
python3 "$ROOT/scripts/shiki.py" loop step --target "$TARGET" --goal-id "$RGOAL" >/tmp/shiki-goal-loop-repair.json
test "$(json_get_last /tmp/shiki-goal-loop-repair.json action)" = "dispatch_repair"
REPAIR_ID="$(json_get_last /tmp/shiki-goal-loop-repair.json repair_id)"
test -f "$TARGET/.shiki/repairs/$REPAIR_ID.json"
test -f "$TARGET/.shiki/handoffs/$REPAIR_ID-repair.md"
WORKTREE_DIR="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["path"])' "$TARGET/.shiki/worktrees/$RTASK.json")"
grep "$REPAIR_ID" "$WORKTREE_DIR/claude-last-prompt.txt" >/dev/null

# After the repair attempt limit, the loop stops for the Guardian.
python3 - "$TARGET" "$RTASK" "$RGOAL" <<'PY'
import json
import sys
from pathlib import Path

target, task_id, goal_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
for index in range(2, 4):
    payload = {
        "repair_id": f"RP-990{index}",
        "goal_id": goal_id,
        "task_id": task_id,
        "pr": 42,
        "attempt": index,
        "failing_checklist_items": ["fixture"],
        "failing_acceptance_criteria": [],
        "minimal_required_changes": ["fixture"],
        "prohibited_changes": [],
        "required_skill": "diagnose",
        "verification_commands": ["true"],
        "evidence_required": ["fixture"],
        "stop_condition": "fixture",
        "created_at": "2026-06-12T00:00:00+00:00",
    }
    (target / ".shiki" / "repairs" / f"RP-990{index}.json").write_text(json.dumps(payload, indent=2))
task_path = target / ".shiki" / "tasks" / f"{task_id}.json"
task = json.loads(task_path.read_text())
task["status"] = "repair-needed"
task_path.write_text(json.dumps(task, indent=2))
PY
if python3 "$ROOT/scripts/shiki.py" loop step --target "$TARGET" --goal-id "$RGOAL" >/tmp/shiki-goal-loop-limit.json; then
  echo "expected repair-limit stop to exit non-zero" >&2
  exit 1
fi
test "$(json_get_last /tmp/shiki-goal-loop-limit.json action)" = "stop_guardian"

# A dependency-complete task whose lock overlaps another task's ACTIVE lock must
# produce a NAMED stop_lock_blocked (owning task + overlapping lock) instead of
# silently skipping — a silent skip read as the PR #179 deadlock stall. The stop
# is a distinct action from the guardian/blocked-evidence stops above.
python3 "$ROOT/scripts/shiki.py" goal create --target "$TARGET" --title "Lock gate" --outcome "Serialized task names its blocker" >/tmp/shiki-goal-loop-lock-goal.json
LGOAL="$(json_get /tmp/shiki-goal-loop-lock-goal.json goal_id)"
python3 "$ROOT/scripts/shiki.py" issue plan --target "$TARGET" --goal-id "$LGOAL" \
  --title "Lock-blocked slice" --scope "Shares a lock with an active owner" \
  --acceptance-check "Runs once the owner releases" >/tmp/shiki-goal-loop-lock-task.json
LTASK="$(json_get /tmp/shiki-goal-loop-lock-task.json task_id)"
# Pin the blocked task's lock to a unique path and clear its dependencies so it is
# dependency-complete and ONLY its lock is contended.
python3 - "$TARGET/.shiki/tasks/$LTASK.json" <<'PY'
import json
import sys

path = sys.argv[1]
task = json.load(open(path))
task["status"] = "planned"
task["dependencies"] = []
task["locks"] = ["path:lock-conflict-slice.txt"]
json.dump(task, open(path, "w"), indent=2)
PY
# An external task holds the SAME path as an active lock (the contention owner).
LOCK_OWNER="T-20260729T000000000000Z-extowner"
python3 - "$TARGET/.shiki/locks/$LOCK_OWNER.json" "$LOCK_OWNER" <<'PY'
import json
import sys

path, owner = sys.argv[1], sys.argv[2]
record = {
    "task_id": owner,
    "goal_id": "G-external",
    "locks": ["path:lock-conflict-slice.txt"],
    "state": "active",
    "owner": "shiki-run",
    "created_at": "2026-07-29T00:00:00+00:00",
}
json.dump(record, open(path, "w"), indent=2)
PY
if python3 "$ROOT/scripts/shiki.py" loop step --target "$TARGET" --goal-id "$LGOAL" >/tmp/shiki-goal-loop-lock.json; then
  echo "expected lock-blocked stop to exit non-zero" >&2
  exit 1
fi
test "$(json_get_last /tmp/shiki-goal-loop-lock.json action)" = "stop_lock_blocked"
# The stop NAMES the owning task and the overlapping lock, so the goal reports the
# fact rather than appearing idle.
grep "$LOCK_OWNER" /tmp/shiki-goal-loop-lock.json >/dev/null
grep "lock-conflict-slice.txt" /tmp/shiki-goal-loop-lock.json >/dev/null

# DEFECT A — a needs_guardian CCA verdict produces a Guardian stop, NEVER a repair.
# The `CCA verdict` check is red and the rerun budget is spent (cca_rerun_count=2),
# so the loop resolves the verdict VALUE from the CCA evidence artifact and, seeing
# needs_guardian, stops for the Guardian without dispatching a repair or consuming a
# repair attempt (the PR #240 harm this task removes).
python3 "$ROOT/scripts/shiki.py" goal create --target "$TARGET" --title "Verdict gate" --outcome "needs_guardian verdict stops, never repairs" >/tmp/shiki-goal-loop-verdict-goal.json
VGOAL="$(json_get /tmp/shiki-goal-loop-verdict-goal.json goal_id)"
python3 "$ROOT/scripts/shiki.py" issue plan --target "$TARGET" --goal-id "$VGOAL" \
  --title "Guardian-verdict slice" --scope "CCA says a human must approve" --risk-level high \
  --acceptance-check "Guardian resolves the verdict" >/tmp/shiki-goal-loop-verdict-task.json
VTASK="$(json_get /tmp/shiki-goal-loop-verdict-task.json task_id)"
python3 - "$TARGET/.shiki/tasks/$VTASK.json" <<'PY'
import json
import sys

path = sys.argv[1]
task = json.load(open(path))
task["status"] = "review"
task["expected_pr"] = 88
# The same-head CCA race is already exhausted, so the decision reaches the
# verdict gate instead of another rerun.
task["cca_rerun_count"] = 2
json.dump(task, open(path, "w"), indent=2)
PY
# The CCA verdict check is red against green siblings...
cat >"$GH_STATE/checks-88.json" <<'JSON'
[{"name":"Validate Shiki mirror","bucket":"pass"},{"name":"CCA verdict","bucket":"fail"},{"name":"MergeGate metadata check","bucket":"pass"},{"name":"MergeGate policy check","bucket":"pass"}]
JSON
# ...and the durable CI evidence (the shiki-cca-evidence artifact) says needs_guardian.
cat >"$GH_STATE/cca-run.json" <<'JSON'
[{"databaseId":700,"conclusion":"failure","headSha":"","status":"completed"}]
JSON
cat >"$GH_STATE/cca-verdict.json" <<'JSON'
{"verdict":"needs_guardian","pr":88,"head_sha":"","task_id":"placeholder","goal_id":"placeholder","summary":"a human must approve"}
JSON
VERDICT_PACKETS_BEFORE="$(find "$TARGET/.shiki/repairs" -maxdepth 1 -name 'RP-*.json' -type f 2>/dev/null | wc -l | tr -d ' ')"
if python3 "$ROOT/scripts/shiki.py" loop step --target "$TARGET" --goal-id "$VGOAL" >/tmp/shiki-goal-loop-verdict.json; then
  echo "expected needs_guardian verdict stop to exit non-zero" >&2
  exit 1
fi
test "$(json_get_last /tmp/shiki-goal-loop-verdict.json action)" = "stop_guardian"
grep "needs_guardian" /tmp/shiki-goal-loop-verdict.json >/dev/null
# No repair packet was created — the Guardian gate never consumes a repair attempt.
VERDICT_PACKETS_AFTER="$(find "$TARGET/.shiki/repairs" -maxdepth 1 -name 'RP-*.json' -type f 2>/dev/null | wc -l | tr -d ' ')"
test "$VERDICT_PACKETS_BEFORE" = "$VERDICT_PACKETS_AFTER"
# Clean up the CCA fixtures so they cannot leak into any later case.
rm -f "$GH_STATE/cca-run.json" "$GH_STATE/cca-verdict.json"

# A PR that fell BEHIND its base is SYNCED, then merged — no operator action. Every
# required check is green and there is no real conflict; main simply moved while the
# PR was open. The loop merges the base into the branch (a plain merge + push, never
# a rebase/force-push) and re-verifies, then merges once GitHub reports CLEAN.
SYNC_BRANCH="shiki/behind-then-merge-slice"
SYNC_PR=77
# Build the task branch + an advanced base without moving the target's HEAD: use a
# scratch worktree that pushes the branch to the bare origin and stamps the shared
# origin/* tracking refs the sync effector reads (its `git fetch` hits the stub
# GitHub fetch URL and fails, exactly like the closeout effector's fetch above).
SYNC_SCRATCH="$TMP_ROOT/sync-scratch"
git worktree add -b "$SYNC_BRANCH" "$SYNC_SCRATCH" main >/dev/null 2>&1
git -C "$SYNC_SCRATCH" config user.name "Shiki Test"
git -C "$SYNC_SCRATCH" config user.email "shiki@example.test"
printf 'branch work\n' > "$SYNC_SCRATCH/sync-branch-file.txt"
git -C "$SYNC_SCRATCH" add sync-branch-file.txt
git -C "$SYNC_SCRATCH" commit -m "sync task branch work" >/dev/null
git -C "$SYNC_SCRATCH" push -u origin "$SYNC_BRANCH" >/dev/null 2>&1
# Advance the base on a non-conflicting file and record it as origin/main locally
# only (the branch is now BEHIND it); leave the bare origin's main untouched.
git -C "$SYNC_SCRATCH" checkout --detach main >/dev/null 2>&1
printf 'base advanced\n' > "$SYNC_SCRATCH/sync-base-advance.txt"
git -C "$SYNC_SCRATCH" add sync-base-advance.txt
git -C "$SYNC_SCRATCH" commit -m "base advances beyond the task branch" >/dev/null
git update-ref "refs/remotes/origin/main" "$(git -C "$SYNC_SCRATCH" rev-parse HEAD)"
git worktree remove --force "$SYNC_SCRATCH"
SYNC_TIP_BEFORE="$(git -C "$ORIGIN_GIT" rev-parse "$SYNC_BRANCH")"

python3 "$ROOT/scripts/shiki.py" goal create --target "$TARGET" --title "Behind gate" --outcome "A behind PR syncs then merges" >/tmp/shiki-goal-loop-behind-goal.json
BGOAL="$(json_get /tmp/shiki-goal-loop-behind-goal.json goal_id)"
python3 "$ROOT/scripts/shiki.py" issue plan --target "$TARGET" --goal-id "$BGOAL" \
  --title "Behind slice" --scope "Base moved while the PR was open" \
  --acceptance-check "Synced then merged" >/tmp/shiki-goal-loop-behind-task.json
BTASK="$(json_get /tmp/shiki-goal-loop-behind-task.json task_id)"
python3 - "$TARGET/.shiki/tasks/$BTASK.json" "$SYNC_BRANCH" "$SYNC_PR" <<'PY'
import json
import sys

path, branch, pr = sys.argv[1], sys.argv[2], int(sys.argv[3])
task = json.load(open(path))
task["status"] = "review"
task["expected_pr"] = pr
task["expected_branch"] = branch
json.dump(task, open(path, "w"), indent=2)
PY
cat >"$GH_STATE/checks-$SYNC_PR.json" <<'JSON'
[{"name":"Validate Shiki mirror","bucket":"pass"},{"name":"CCA verdict","bucket":"pass"},{"name":"MergeGate metadata check","bucket":"pass"},{"name":"MergeGate policy check","bucket":"pass"}]
JSON
# Step 1: every check green but the branch is BEHIND -> sync_branch, not merge.
echo "BEHIND" >"$GH_STATE/merge-state-$SYNC_PR"
python3 "$ROOT/scripts/shiki.py" loop step --target "$TARGET" --goal-id "$BGOAL" >/tmp/shiki-goal-loop-behind-sync.json
test "$(json_get_last /tmp/shiki-goal-loop-behind-sync.json action)" = "sync_branch"
# The sync pushed a real base merge: the branch head advanced and no merge happened.
SYNC_TIP_AFTER="$(git -C "$ORIGIN_GIT" rev-parse "$SYNC_BRANCH")"
test "$SYNC_TIP_AFTER" != "$SYNC_TIP_BEFORE"
test ! -f "$GH_STATE/merged-$SYNC_PR"
# Step 2: GitHub now reports the branch up to date -> the merge proceeds.
echo "CLEAN" >"$GH_STATE/merge-state-$SYNC_PR"
python3 "$ROOT/scripts/shiki.py" loop step --target "$TARGET" --goal-id "$BGOAL" >/tmp/shiki-goal-loop-behind-merge.json
test "$(json_get_last /tmp/shiki-goal-loop-behind-merge.json action)" = "merge"
test -f "$GH_STATE/merged-$SYNC_PR"

echo "shiki goal loop tests passed"
