# Triage Labels

Target Repositories may customize labels, but the Shiki Template assumes these categories exist.

## Goal Lifecycle

- `shiki:goal` - issue is a Shiki Goal.
- `shiki:planning` - Goal is being clarified or decomposed.
- `shiki:ready` - Goal has enough context for execution planning.
- `shiki:blocked` - Goal cannot proceed without external input.
- `shiki:done` - Goal is complete and evidence is recorded.

## Runtime Assignment

- `runtime:codex` - implementation, tests, or repair should run through Codex.
- `runtime:claude` - planning, review, judgment, or documentation should run through Claude Code.
- `runtime:human` - Guardian or operator decision is required.
- `runtime:hermes` - Hermes Runner or another orchestration layer owns execution.

## Risk

- `risk:low` - eligible for automation and goal-loop auto-merge after required checks.
- `risk:medium` - requires normal review; goal-loop auto-merge permitted once all MergeGate evidence is green (ADR 0008).
- `risk:high` - requires Guardian approval before merge.
- `risk:critical` - no auto-merge; explicit approval and audit evidence required.

## MergeGate

- `mergegate:waiting` - merge evidence is incomplete.
- `mergegate:blocked` - check, lock, dependency, risk, or review blocks merge.
- `mergegate:ready` - MergeGate conditions are satisfied.
- `repair:needed` - a bounded Repair Loop is required.

## Skill Gate

- `skill:setup`
- `skill:grill-with-docs`
- `skill:zoom-out`
- `skill:to-prd`
- `skill:to-issues`
- `skill:triage`
- `skill:tdd`
- `skill:code-review`
- `skill:diagnose`
- `skill:architecture`
- `skill:prototype`
