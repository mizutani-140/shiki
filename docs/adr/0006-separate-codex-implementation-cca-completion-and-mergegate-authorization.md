# ADR 0006: Separate Codex Implementation, CCA Completion, And MergeGate Authorization

## Status

Accepted

## Context

Shiki needs autonomous implementation without letting a single agent decide that its own work is complete, mergeable, or deployable.

The standard pipeline is:

```text
Goal Seek
  -> grill-with-docs
  -> Context & Impact
  -> to-prd
  -> to-issues
  -> triage
  -> Task DAG + locks
  -> Codex Front + tdd
  -> PR + evidence
  -> GitHub CCA completion judgment
  -> MergeGate
  -> bounded Repair Loop
  -> merge
  -> Goal completion judgment
```

## Decision

Use this responsibility split:

- Codex Front implements and repairs only from an authorized task handoff or repair packet.
- GitHub CCA judges whether PR evidence proves completion and emits a structured verdict.
- MergeGate authorizes state transitions and merge readiness.
- Guardian approves high-risk, critical, production, security, policy, and exception paths.

CCA verdicts are limited to:

- `complete`
- `repair_required`
- `blocked`
- `needs_guardian`
- `insufficient_evidence`

Only `complete` may proceed to MergeGate readiness.

## Consequences

- Codex cannot self-declare completion.
- Failed completion judgment produces a bounded repair packet instead of broad rework.
- Merge readiness can be enforced through required GitHub checks.
- Worktree, CI/CD, merge, deployment, and exception decisions stay outside the implementation runtime.
