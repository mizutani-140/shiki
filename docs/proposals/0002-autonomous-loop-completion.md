# PRD 0002: Complete the Autonomous Post-Freeze Loop (finish #119 G-D)

Status: Requirements Definition complete; awaiting Spec Freeze.
Goal issue: #140. Parent PRD: #119 (Shiki 0.2.0, G-D). Decision: SADR-0011.

## Problem Statement

The Shiki 0.2.0 autonomous post-freeze loop (SADR-0008/SADR-0009, #119 G-D) is not
end-to-end functional. A live `shiki loop run` on 2026-06-17 proved the loop
self-implements (a headless `claude -p` runner wrote a correct, passing test
with no human action) and the Memory Loop auto-capture fires — but it **cannot
self-drive to auto-merge**. The connective tissue between "runner implemented"
and "MergeGate-green PR" is missing, so a task stalls in `review`/PR-open and
never reaches `done`. This is why #119 stays OPEN: G-D is genuinely incomplete.

Empirically verified gaps (file:line):
1. `create_pr` never commits/pushes the runner's work — `create_github_pr_for_task`
   (`scripts/shiki_github.py:293`) runs `gh pr create` with no commit/push; the
   runner adapter only pipes the handoff into `claude -p`. → *"No commits between
   main and <branch>"*.
2. `_sync_state_to_branch` (`scripts/shiki_loop.py`) syncs only a subset of
   ledgers to the branch, not the task's full `ledger_evidence` → MergeGate
   "ledger evidence missing" (append-only-evidence, fail-closed).
3. The autonomous path produces no skill-evidence ledgers → MergeGate "Required
   skill tdd has no matching ledger evidence"; CCA PR-12 (pre-PR code review)
   cannot be satisfied.
4. (Unreached) CCA + PR-12 for an autonomously-implemented task.
5. Loop tasks must lock `.shiki/**` (the loop syncs evidence to the branch); a
   task locked only to its code surface fails MergeGate "file outside locks".

## Solution

Make the loop produce the evidence MergeGate/CCA need — deterministically, and
via an independent verifier, never by the implementer self-attesting (SADR-0011).
After a successful dispatch and before opening the PR, the loop:

1. **Commits + pushes** the runner's implementation to the task branch.
2. **Records TDD evidence as an observable fact** — runs the task's tests in the
   worktree and records a `check` ledger of the command and green result
   (loop-observed green; red-first ordering is not gated — SADR-0011).
3. **Runs an independent code-review verifier** — a read-only `claude -p`
   dispatch (`--allowedTools` restricted to read tools, `--json-schema`
   structured verdict, separate context). The loop parses the verdict
   deterministically (parse/dispatch failure = fail-closed → block), records a
   `code-review` ledger, and writes the `## Pre-PR code review` PR-body section
   from it. Blocking findings feed the existing repair loop (verdict → repair
   packet → fix → re-review), bounded by the 3-attempt limit.
4. **Syncs the full evidence set** — copies every file referenced by
   `task.ledger_evidence` (plus the report/runner evidence) to the branch.
5. Then `create_pr`, and the autonomous merge proceeds for low/medium risk.

Loop-executed tasks auto-include `path:.shiki/**` in their locks (the loop
always syncs `.shiki` evidence to the branch), with a validator/doctor hint.

## User Stories

1. As an operator, after Spec Freeze I want a low/medium-risk task to go from
   `ready` to auto-merged `done` with **zero manual pre-clearing**, so a frozen
   Goal completes unattended.
2. As a Guardian, I want the loop's quality-gate evidence to be deterministic
   and independently verified (not self-graded by the implementer), so an
   unattended merge is trustworthy.
3. As a maintainer, I want a stubbed-runner contract test that proves the full
   dispatch → PR → merge → repair path, so the autonomous loop cannot silently
   regress.

## Implementation Decisions (settled in Requirements Definition)

- **Evidence ownership (SADR-0011, Q1):** the loop produces all quality-gate
  evidence; the implementer runtime never writes its own skill-evidence ledgers
  or PR-12 section.
- **Code-review verifier (Q2):** read-only `claude -p` (restricted `--allowedTools`,
  `--json-schema` verdict), same model / separate context (like CCA),
  fail-closed on parse/dispatch error, blocking → repair loop. Realized as a
  read-only **reviewer invocation** distinct from the bypass-permissions
  implementer adapter (`scripts/shiki_runtime_adapters.py`).
- **TDD evidence (Q3):** loop-observed green `check` ledger; red-first not gated.
- **Branch plumbing (gaps #1/#2, Q4):** add a commit+push-implementation step and
  a full `ledger_evidence` sync to `execute_action`'s `create_pr` branch /
  `_sync_state_to_branch` in `scripts/shiki_loop.py`, before
  `create_github_pr_for_task`. (Gap #1 prototype verified live to open the PR.)
- **Task-lock guard (gap #5, Q5):** loop-task registration auto-includes
  `path:.shiki/**`; `validate_shiki.py`/`shiki doctor` warn when a loop task
  lacks it.
- **Touched modules (high risk, architecture-gated):** `scripts/shiki_loop.py`,
  `scripts/shiki_github.py`, `scripts/shiki_runtime.py`,
  `scripts/shiki_runtime_adapters.py`, `scripts/shiki_tasks.py`,
  `scripts/validate_shiki.py`, `docs/agents/checklists.md`. Each PR Guardian-reviewed.

## Testing Decisions

- Unit: extend `tests/test_loop_engine.py` for the new create_pr-path effectors
  (commit+push, full evidence sync, tdd-evidence record, reviewer dispatch +
  fail-closed parse, blocking→repair).
- Contract: a stubbed-runner **and stubbed-reviewer** shell test (pattern of
  `scripts/test_shiki_runner_claude.sh` / `scripts/test_shiki_goal_loop.sh`) that
  drives dispatch → tdd-evidence → code-review → commit/push → create_pr →
  checks → merge → done, including the failure → repair branch.
- E2E acceptance: re-run `shiki loop run` on a low-risk claude-code task and
  confirm it self-drives to auto-merge + goal complete with no manual pre-clearing.
- The existing CI gate (full validator + unittest + contract shell tests) stays
  the verification surface; no new required checks.

## Out of Scope

- High/critical-risk auto-merge (remains Guardian-gated, SADR-0010).
- Changing CCA verdict semantics, the MergeGate required-check set,
  branch-protection contexts, or the Guardian policy.
- `bypassPermissions` bounding for the implementer (separate concern).
- Deterministic red→green verification (SADR-0011 future enhancement).
- Removing Codex support.

## Further Notes

- **Acceptance gate:** `shiki loop run` on a low-risk task completes
  implement → commit/push → tdd-evidence → code-review → PR → required checks +
  CCA pass → MergeGate green → auto-merge → task `done` → goal `complete`, with
  no manual pre-clearing; the stubbed contract test covers the full path incl.
  repair.
- **Execution:** #140 itself is executed manually/interactively under the guarded
  flow (CCA/MergeGate/Guardian, like the Memory Loop) until the acceptance gate
  proves the loop self-drives — a deliberate dogfood: the final task verifies the
  loop on a real task.
- **Risk:** high (core loop/runtime/github modules; architecture gate). Each PR
  Guardian-reviewed per SADR-0010.
- **CI-08 evidence:** Context & Impact produced from the live verification
  (gaps at file:line), SADR-0011, and a Workflow parallel exploration sweep
  (run 2026-06-17).
- **Decisions:** SADR-0011 (autonomous quality-gate evidence). Domain terms:
  CCA, MergeGate, Repair Loop, Agent Runtime (CONTEXT.md).
