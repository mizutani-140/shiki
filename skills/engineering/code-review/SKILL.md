---
name: code-review
description: Mandatory pre-PR self-review gate for Shiki implementers. Run after the TDD loop is green and before creating or updating a PR. Reviews the task diff for correctness bugs and reuse/simplification/efficiency cleanups, applies fixes on the task branch, and records durable evidence.
---

# Code Review (Pre-PR Implementer Gate)

This is the Shiki wrapper for the implementer's native code-review capability.
In Claude Code it is satisfied by the built-in `/code-review` skill; another
Agent Runtime may satisfy it with an equivalent diff review that produces the
same evidence.

## Position In The Loop

```
tdd (red-green-refactor, all checks green)
  -> code-review (this gate: review the full task diff, apply fixes)
  -> re-run verification
  -> PR creation or update
```

The gate runs in the **Implementer** role on the implementer's own task
branch, before any PR exists or before pushing new commits to an existing PR.
Applying fixes here does not violate the Reviewer rule ("reviewers do not
mutate the implementation branch") because no review handoff has happened yet.

## How To Run

1. Confirm the TDD loop is complete and verification commands pass.
2. Review the entire task diff against the task contract (scope, non-goals,
   acceptance checks, locks). In Claude Code, invoke the native `/code-review`
   skill at an effort matching task risk: low/medium risk → medium effort;
   high/critical risk → high or max effort. The cloud multi-agent deep review
   (ultra) is operator-triggered and not required by this gate.
3. Triage findings:
   - Correctness bugs, contract violations, scope drift: fix now.
   - Quality cleanups (reuse, simplification, efficiency): fix when bounded
     to the task scope; otherwise record as a follow-up note.
   - Findings you reject: record why.
4. Re-run the task's verification commands after fixes.
5. Record evidence (below). Only then create or update the PR.

## Required Evidence

Both of these are required; missing evidence blocks MergeGate readiness:

- A Ledger entry whose summary names this skill (`code-review`) and states
  findings count, applied fixes, and rejected findings with reasons. List the
  entry in the task's `ledger_evidence`.
- A `## Pre-PR code review` section in the PR body summarizing findings and
  resolutions (or stating "no findings" when the review was clean).

## Exceptions

Evidence-only tasks, pure `.shiki` state reconciliation, and trivial
documentation-only changes may skip the gate when the skip is stated in the
PR body and recorded in the ledger. CCA may treat an unjustified skip as
`insufficient_evidence`.
