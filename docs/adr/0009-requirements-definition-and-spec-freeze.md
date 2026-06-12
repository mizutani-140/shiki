# ADR 0009: Merge Grill And Planning Into Requirements Definition With Spec Freeze

## Status

Proposed

## Context

The operating model runs Goal Seek, `grill-with-docs`, Context & Impact, PRD,
issue decomposition, and Task DAG planning as separate steps, each with its own
checklist family and state transition. In practice the operator experiences
this as repeated interactive sessions, and the boundary between "plan settled"
and "implementation may start" is implicit: the de facto marker is
`grill_with_docs.status=complete` in the plan JSON, which `shiki start` can
synthesize from a short interview without a real freeze decision.

Two operational failures motivated a redesign (recorded operator lessons):
required scope surfaced twice mid-implementation because requirements were
never frozen against an explicit out-of-scope boundary, and autonomous
execution stalled because every downstream step re-opened questions the
planning phase should have settled.

The Goal lifecycle should have exactly one human gate on the happy path, with
everything after it autonomous (ADR 0008 provides the implementation runtime
for this).

## Decision

We will restructure the pre-implementation loop:

- **Requirements Definition** is the single interactive phase. It combines
  Goal Seek, `grill-with-docs`, Context & Impact review, and PRD drafting into
  one continuous operator dialogue, entered through the `/shiki` command's
  Goal mode (no separate `/goal` command). Context & Impact for non-trivial
  Goals is produced by a Workflow-tool parallel exploration sweep recorded as
  evidence.
- **Spec Freeze** is the state transition where the operator approves the PRD.
  It is recorded as an explicit `spec_freeze` block in the plan contract and a
  ledger entry. Required external scopes and permissions (the scope inventory)
  must be enumerated before freeze.
- Task decomposition, DAG construction, and implementation run autonomously
  after Spec Freeze and are validated against the frozen PRD's scope
  boundaries. The post-freeze loop auto-merges risk low/medium PRs once
  MergeGate is satisfied, auto-dispatches bounded repairs, and re-evaluates
  the DAG after each merge. It stops only for: Spec Amendment, the repair
  attempt limit, `needs_guardian`, high/critical risk, or Goal completion.
- **Spec Amendment** is the only way to change a frozen spec: affected tasks
  pause, the contested decisions are re-grilled with the operator, and freeze
  is re-stamped with the amendment recorded. Only the operator approves
  amendments. Non-scope-moving interpretations are recorded in the
  **Assumption Log** without pausing work and are challengeable by CCA.
- The plan schema change is additive: `grill_with_docs.status=complete` is
  retained for compatibility, and `spec_freeze` is added alongside it, with a
  registered migration backfilling existing plans.

Out of scope: changing CCA verdict semantics, MergeGate required checks, or
the Guardian policy; applying the migration to existing Target Repositories
(separate Goal after the 0.2.0 release).

## Consequences

- The operator gates a Goal exactly once. Post-freeze interruptions become a
  signal of planning failure, measurable as Spec Amendment frequency.
- The state machines gain a `spec_frozen` state after `prd_ready`; checklist
  families gain Spec Freeze and Amendment items; CCA judges scope drift
  against the frozen PRD instead of an implicit plan.
- The `/shiki` command flow, plan schema, `validate_shiki.py`,
  `shiki_tasks.py`, `shiki_bootstrap.py` synthesis, and the duplicated
  operating-model text (README, AGENTS.md, CLAUDE.md, CONTRIBUTING.md,
  docs/agents/*) must change together; validator cross-reference checks guard
  the sweep.
- `shiki start` may no longer stamp a synthesized freeze: the freeze must come
  from a real Requirements Definition dialogue or an explicitly recorded
  trivial-change justification.
- Front-loading all decisions makes Requirements Definition sessions longer
  and raises the cost of a careless freeze; the Assumption Log keeps trivia
  from forcing amendments, and Spec Amendment keeps real discoveries bounded
  instead of silent.

## Alternatives Considered

- **Freeze after task decomposition**: more operator control over granularity,
  rejected because it lengthens the interactive phase and per-task CCA +
  MergeGate already guard decomposition quality.
- **Two-stage freeze (requirements, then plan)**: most rigorous, rejected as
  over-complex for a solo-operator platform and contrary to the one-gate goal.
- **Risk-tiered amendment self-approval**: rejected; deciding what is
  "low risk" would itself be an LLM judgment, hollowing out the freeze.
- **Renaming the `grill_with_docs` plan field**: rejected; a rename breaks
  every installed target's plans and start records for no semantic gain.
