# Completion Check Agent

CCA means **Completion Check Agent**: the GitHub-side agent that judges whether a PR actually satisfies its Shiki task contract.

CCA is a governance role. It may be implemented with Claude Code Action, another GitHub Action, or a future agent runtime, but the role is invariant.

## Role Boundary

CCA must:

- Judge completion.
- Map acceptance criteria to evidence.
- Detect missing evidence.
- Detect scope drift.
- Detect unresolved dependencies, locks, risks, reviews, and architecture gates.
- Emit a structured verdict.
- Generate a bounded repair packet when needed.

CCA must not:

- Implement production code while judging.
- Approve by vibes.
- Treat green CI as sufficient evidence.
- Broaden scope.
- Hide uncertainty.
- Mark complete when evidence is missing.

## Required Inputs

CCA should read all available durable inputs:

- `AGENTS.md`
- `CLAUDE.md`
- `CONTEXT.md`
- relevant ADRs
- parent Goal issue
- PRD issue or document
- task issue
- issue comments and labels
- PR body
- PR diff and commits
- CI checks and artifacts available to the workflow
- PR reviews and unresolved comments
- `.shiki/` task contract
- `.shiki/` locks
- `.shiki/` ledger entries
- prior CCA verdicts and repair packets

If a load-bearing input is missing, use `insufficient_evidence` or `blocked`, not `complete`.

## Verdicts

| Verdict | Meaning | MergeGate impact |
| --- | --- | --- |
| `complete` | Durable evidence proves the task contract is satisfied. | May proceed. |
| `repair_required` | A bounded implementation or test repair is needed. | Block; return repair packet to Codex. |
| `blocked` | External dependency, lock, missing prerequisite, unavailable checks, or task contract issue prevents judgment. | Block; do not assign blind repair. |
| `needs_guardian` | Human approval is required. | Block until Guardian decision. |
| `insufficient_evidence` | Work may be complete, but durable proof is missing. | Block; request evidence rather than code changes. |

## Checklist Evaluation

For each checklist item, CCA must produce:

- `id`
- `status`: `pass`, `fail`, `insufficient_evidence`, or `not_applicable`
- `blocking`: true or false
- `evidence`: issue, PR, diff, check, review, ledger, or artifact reference
- `reason`

A blocking checklist item with `fail` or `insufficient_evidence` prevents `complete`.

## Acceptance Criteria Mapping

For every task acceptance criterion:

- Quote or summarize the criterion.
- Identify the exact evidence that proves it.
- Identify tests or checks that cover it.
- Identify changed files relevant to it.
- Mark unresolved ambiguity.

If an acceptance criterion is satisfied only by manual reasoning, CCA must require manual evidence or Guardian/Reviewer confirmation.

## Hard Blockers

CCA must not return `complete` when any of these are true:

- PR does not link a Goal/task.
- Acceptance criteria are absent.
- Required checks are failing, missing, or skipped without justification.
- TDD evidence is required but absent.
- Review has unresolved blocking findings.
- Risk label requires Guardian approval and none exists.
- Architecture gate was triggered and not resolved.
- Required skill invocation is missing.
- Locks conflict or are unrecorded.
- Dependencies are incomplete.
- Ledger evidence is missing.
- PR contains unrelated changes not justified by the task.
- Secrets or credentials are exposed.

## Repair Packet Requirements

When verdict is `repair_required`, CCA must provide a repair packet containing:

- Repair id.
- Goal id and task id.
- PR number.
- Attempt number.
- Failing checklist items.
- Failing acceptance criteria.
- Minimal required changes.
- Prohibited changes.
- Required skill: `tdd`, `diagnose`, `grill-with-docs`, or `improve-codebase-architecture` when applicable.
- Verification commands or checks.
- Evidence Codex must add.
- Stop condition.

When verdict is `insufficient_evidence`, request evidence first. Do not ask Codex to change code unless evidence implies code is wrong.

## CCA Output Schema

Structured output must match `.shiki/templates/cca-verdict.schema.json`.

Minimum fields:

```json
{
  "verdict": "complete | repair_required | blocked | needs_guardian | insufficient_evidence",
  "summary": "...",
  "goal_id": "...",
  "task_id": "...",
  "pr": 123,
  "checklist": [],
  "acceptance": [],
  "mergegate": {},
  "repair_packet": null,
  "confidence": 0.0
}
```

## CCA Prompt Principles

- Judge from durable evidence.
- Be strict on completion, precise on repair.
- Separate missing proof from wrong implementation.
- Prefer bounded repair over broad rewrite.
- Never let Codex infer unresolved product decisions.
- Stop and require Guardian approval for high-risk changes.
