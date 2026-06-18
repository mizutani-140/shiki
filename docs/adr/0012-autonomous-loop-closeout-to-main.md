# 0012 — Autonomous loop pushes goal completion to main via a closeout PR

- Status: accepted
- Date: 2026-06-18
- Deciders: operator (mizutani-140), Claude (planner/implementer)
- Related: ADR 0008 (default implementer + autonomous merge), ADR 0009 (spec freeze), ADR 0011 (autonomous quality-gate evidence), issue #140 (complete the autonomous post-freeze loop), the #140 T5 live self-drive.

## Context

The `shiki loop` self-drives a frozen Goal: dispatch → implement → pre-PR code review → loop-observed TDD → commit/push → open the task PR → CCA → auto-merge. A live #140 T5 run proved this works end to end: the loop merged a task PR (`#153`) with zero manual intervention.

But the loop's **completion** never reached main. After the `merge` action, the loop ran `_mark_done` (local `task.status=done`, local lock release) and then, when all tasks were `done`, `cmd_goal_complete` (local `goal.status=complete`, scorecard report). All of these write the **coordinator's local `.shiki/` mirror only** — never a branch, never main. After the auto-merge, GitHub (the operational source of truth) still showed `task=review`, `goal=planned`, lock `active`. The loop reported `outcome: complete` on the strength of local state alone.

`validate_shiki` couples task and goal completion: when a goal's DAG covers all tasks and every node is terminal (`done`), the goal MUST be `complete` on that HEAD checkout. `post_merge_reconcile` mode deliberately refuses terminal status (it only sets `review`) and so cannot push `goal=complete`. Pushing completion therefore requires a **normal-mode** PR carrying `task=done` + `goal=complete` + lock release together — exactly the manual closeout pattern used for PRs #144 / #149 / #152 / #154.

An 8-agent design+verification workflow examined two options:
- **B1**: fold completion into the impl PR (self-contained terminal PR).
- **B2**: after the impl PR merges, open and auto-merge a separate, deterministic closeout PR.

The verification found B1's "carry done+complete in the impl PR" conflicts with the loop's `review → CCA → merge → done` ordering (the decision engine keys on `task.status`; a local `done` short-circuits to a local `goal_complete` and exits before any merge). It also confirmed — empirically, via PR #149 — that a **pure-bookkeeping closeout PR with no implementation diff does obtain a `complete` CCA verdict**, so B2's separate closeout is viable. B2 matches the existing, proven manual closeout pattern and keeps the impl PR's CCA judging real implementation.

## Decision

The loop pushes completion to main by **automating the closeout PR** (B2). The transition `merge impl PR → local mark_done → local goal_complete` is replaced by `merge impl PR → open a normal-mode closeout PR → auto-merge it → only then report goal complete`.

Mechanics (single-task and last-task-of-a-goal; see Consequences for multi-task):

1. A new task field `closeout_pr` is the phase / re-entrancy anchor. The `merge` action no longer marks the task done; it only merges `expected_pr` and records evidence.
2. `decide_task_action`, for a `review` task whose `expected_pr` snapshot is **merged**:
   - if no `closeout_pr` yet → action `create_closeout_pr` (the impl PR just merged);
   - if `closeout_pr` is set → action `mark_done` (the closeout PR merged; completion is on main).
3. `create_closeout_pr` builds a normal-mode closeout PR in a **fresh worktree cut from `origin/main`** on the deterministic branch `shiki/<task>-closeout`: set `task.status=done`, lock `state=released`, and — only if this task makes every goal task `done` — run `cmd_goal_complete` (scorecard report + completion ledger + `goal.status=complete`) **in that worktree** so the coupling is satisfied on the HEAD. It then opens the PR, records the `/pull/N` self-reference ledger, and in the coordinator sets `task.closeout_pr` and repoints `task.expected_pr` (+ `expected_branch`) to the closeout PR so the existing snapshot/merge machinery drives it.
4. While `closeout_pr` is set and not merged, the loop gates on the closeout PR's required checks with **no repair path** (a bookkeeping PR has no implementation to repair): green → `merge`; a CCA same-head race → one `rerun_cca`; any genuine check failure → `stop_blocked` (recorded for diagnosis); else `wait_checks`.
5. Loop-executed tasks declare the `path:.shiki/**` lock, which covers every `.shiki` file the closeout stages (goal, dag, lock, report, ledgers), so `files_outside_locks` is satisfied without per-file lock bookkeeping.

Re-entrancy: `create_closeout_pr` is gated on `not task.closeout_pr`, so it is reached only when no closeout PR has been recorded. The closeout branch name is deterministic, so an existing PR for it means a PRIOR run was interrupted mid-effector (after `gh pr create` but before recording `closeout_pr`) and its HEAD may be incomplete — missing the repointed `expected_pr` or the `/pull` self-reference ledger that MergeGate requires. Silently adopting such a PR would block MergeGate forever with no repair path (the closeout phase has no auto-repair). The effector therefore **fails closed to `stop_blocked` for a recorded operator reconcile** (verify/repair the existing PR's HEAD and set `task.closeout_pr`, or close it and re-run) rather than adopting a possibly-broken PR. No force-push or branch deletion is performed (the constitution forbids them without Guardian authorization). Safe automatic adoption — verifying the existing PR's HEAD carries `expected_pr` + the `/pull` ledger before reusing it — is a deferred enhancement.

## Consequences

- The loop reaches **goal complete on main, durably, with zero manual intervention** — closing #140 / ADR 0011's "durable evidence, not local attestation" for the completion step itself.
- The autonomous path costs **two PRs per terminal task** (impl + closeout), both auto-merged at low/medium risk; high/critical still stops for Guardian via the unchanged policy gate.
- **Scope: single-task goals (and the last task of a goal).** A multi-task goal needs a per-task closeout (a goal-scoped PR cannot mutate sibling task files under the task-scope rule); the per-task `closeout_pr` field generalizes to this, but multi-task closeout sequencing is deferred and must be validated by its own live run.
- A closeout PR whose checks fail does **not** auto-repair; it stops the loop for a recorded authority, preventing an implementation-free PR from entering a futile repair cycle.
- The `merge` action no longer marks tasks done; done-marking moves to the post-closeout `mark_done` action. This is a deliberate inversion: the loop never records `done` locally until it is durable on main.
