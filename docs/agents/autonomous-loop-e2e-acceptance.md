# Autonomous Loop End-to-End Acceptance Gate

This is the live acceptance-gate plan for PRD 0002 / Goal G-...1de3b322 (the
ADR 0011 autonomous quality-gate work). It proves the second, non-simulated half
of T5: a real `shiki loop run` on a low-risk task self-drives to **auto-merge +
goal complete with zero manual pre-clearing**, and the run is captured as durable
evidence.

The stubbed contract test (`scripts/test_shiki_loop_e2e.sh`) proves the wiring
deterministically in CI. This gate proves the same path against the real Claude
runner, the real read-only reviewer, real GitHub Actions checks, real CCA, and
real MergeGate — the things stubs cannot vouch for.

## Why a separate live gate

ADR 0011's whole point is that autonomous evidence must be **deterministic
observable facts + an independent verifier, never the implementer
self-attesting**. A green stubbed test shows the state machine is shaped right;
it cannot show that:

- a real headless `claude -p` implementer produces a worktree the loop can
  commit/push;
- the loop-observed TDD test run (T2) actually executes and goes green on real
  task code;
- the independent read-only reviewer (T3) actually dispatches, returns a
  parseable structured verdict, and its fail-closed path holds;
- real GitHub required checks (Validate Shiki mirror, CCA verdict, MergeGate
  metadata check, MergeGate policy check) all go green on the autonomous PR;
- MergeGate auto-merges a low-risk PR with **no human touching the PR** — no
  manual label, no manual re-run, no manual evidence backfill, no manual merge.

## Preconditions (verify before starting — do NOT pre-clear the task)

1. T1, T2, T3, T4 are merged to `main` (branch plumbing, loop-observed TDD
   evidence, independent code-review verifier, lock guard). Without T2/T3 the
   PR cannot satisfy MergeGate `required_skill=tdd`/`code-review` or CCA PR-12,
   and the gate will (correctly) fail to auto-merge.
2. `claude --version` and `claude auth status` report a logged-in subscription
   session (the runner and the reviewer both dispatch through it).
3. `gh auth status` is logged in with merge permission on the repo.
4. Branch protection + required checks are configured exactly as the goal-loop
   required-check set expects (see `docs/agents/branch-protection-smoke.md`).
5. The repo `.shiki/policy` permits autonomous low/medium-risk merges (ADR
   0008/0009); the MergeGate policy check is the recorded-authority gate.

The operator's only actions are: register the task and start the loop. **No
manual pre-clearing** means after `shiki loop run` starts, the operator does not
touch the PR, checks, labels, evidence, or merge button.

## The task under test

A genuinely low-risk, in-scope slice with an observable, testable behavior — the
smallest real change that exercises the full path. Candidate: a documentation or
self-contained helper change that ships a failing-then-passing test the
loop-observed TDD step (T2) can run. It must:

- be `risk_level: low` (so MergeGate may auto-merge without Guardian approval);
- carry the default skill set (`tdd`, `code-review`);
- have a `test_command` the loop can run in the worktree (default:
  `python3 -m unittest discover -s tests`);
- touch only files inside its declared locks.

Do not hand-write any of the TDD-evidence, code-review, or PR-body sections —
those are loop-owned state transitions (ADR 0011). If the operator writes them,
the gate is invalid.

## Procedure

```bash
# 1. Register the low-risk task on a frozen goal (planner/orchestrator step).
shiki issue plan --target <repo> --goal-id <GOAL> \
  --title "<low-risk slice>" --scope "<scope>" --risk-level low \
  --acceptance-check "<observable check>"

# 2. Start the autonomous loop and CAPTURE the full transcript as evidence.
shiki loop run --target <repo> --goal-id <GOAL> --max-cycles <N> --interval <s> \
  | tee .shiki/reports/e2e-acceptance-$(date +%Y%m%dT%H%M%SZ).jsonl
```

From start, the loop must self-drive, with no operator input:

1. `dispatch` — headless `claude -p` implementer writes the slice in the
   worktree.
2. **code-review (T3)** — the loop dispatches the independent read-only reviewer
   FIRST (before the TDD gate, so a blocking review short-circuits before any
   test run), parses its structured verdict, records a `code-review` ledger, and
   writes the `## Pre-PR code review` PR-body section. A non-clean verdict (a
   blocking finding, or a parse/dispatch failure) **fails closed to
   `stop_blocked`** — no PR exists yet to anchor a repair packet, so the loop
   stops for diagnosis rather than dispatching a repair. (The bounded repair
   loop is for POST-PR required-check failures; see PATH 2.)
3. **tdd-evidence (T2)** — the loop then runs the task's `test_command` in the
   worktree and records a `type:check` ledger naming skill `tdd` with an EXEC
   evidence ref (loop-observed green). A red run fails closed: `stop_blocked`,
   no PR.
4. `create_pr` — commit + push the worktree implementation, then open the PR
   carrying the `## TDD evidence (loop-observed)` and `## Pre-PR code review`
   sections.
5. `wait_checks` — real GitHub Actions run: Validate Shiki mirror, CCA verdict,
   MergeGate metadata check, MergeGate policy check.
6. `merge` — with all required checks green and risk low, MergeGate auto-merges.
7. `mark_done` + goal completion — the task is marked done and, with the DAG
   satisfied, the goal completes.

## Pass criteria (all required)

- The final loop result document is `outcome: complete`.
- `.shiki/goals/<GOAL>.json` is `status: complete`.
- The PR was opened, all required checks went green, and it was **merged by the
  loop** — confirmed by the merge ledger (`type:mergegate`) and the GitHub merge
  record, not by a human clicking merge.
- A `tdd` (loop-observed) and a `code-review` ledger exist for the task, each
  with an EXEC/verdict evidence ref, and the PR body carries both sections.
- **Zero manual pre-clearing**: the operator touched nothing on the PR, checks,
  labels, evidence, or merge after `shiki loop run` started. The captured
  transcript plus the ledger timeline must show no operator-authored events
  between dispatch and merge.

## Captured evidence (durable)

- The `tee`-d `shiki loop run` JSONL transcript, stored under `.shiki/reports/`.
- The task's `.shiki/ledger/*` entries: dispatch, `tdd` (loop-observed),
  `code-review`, PR creation (`handoff`), and merge (`mergegate`).
- The merged PR URL, its check run conclusions, and the CCA verdict artifact.
- A short report (`.shiki/reports/`) linking the above and explicitly stating
  that no manual pre-clearing occurred.

## If the gate fails

Per ADR 0011 / the Repair Loop: do not hand-clear the PR to "make the gate
pass". Diagnose which transition produced unexpected/missing evidence
(tdd-evidence, code-review, a required check, MergeGate, CCA), file a bounded
repair against the responsible task (T2/T3/T4), and re-run the gate. A gate that
only passes after a human pre-clears evidence is a FAIL — that is exactly the
self-attestation ADR 0011 forbids.
