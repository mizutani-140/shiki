# Shiki Implementation Policy

This document defines the default implementation policy for Shiki Target Repositories.

The intended flow is:

```text
Goal Seek
  -> grill-with-docs
  -> Context & Impact
  -> to-prd
  -> to-issues
  -> triage
  -> Task DAG + locks
  -> assigned implementer (Claude Code default / Codex optional) + tdd + code-review
  -> PR + evidence
  -> GitHub CCA completion judgment
  -> MergeGate
  -> bounded Repair Loop if needed
  -> merge
  -> Goal completion judgment
```

## Canonical Source Of Truth

<!-- shiki-source-of-truth:start -->
1. GitHub Issues, Pull Requests, Checks, Reviews, comments, and merge evidence are the operational source of truth.
2. The repository-local `.shiki/` mirror records Goals, PRDs, plans, Task DAGs, contracts, locks, ledger entries, CCA verdicts, repair packets, reports, and handoffs.
3. `CONTEXT.md` defines Shiki domain language and glossary decisions.
4. `docs/adr/` records hard-to-reverse platform decisions.
5. Runtime-specific wrappers such as `CLAUDE.md`, `.codex/`, `.claude/`, `.github/prompts/`, and hooks may add stricter instructions but must not weaken the shared constitution.
<!-- shiki-source-of-truth:end -->

## Non-Negotiables

1. **Planning precedes implementation.** Non-trivial Goals start with `grill-with-docs` and Context & Impact.
2. **Domain language is controlled.** Terms must be checked against `CONTEXT.md`; hard-to-reverse tradeoffs must be checked against ADRs.
3. **PRD precedes issue decomposition.** `to-prd` is used once the context is settled enough to become durable intent.
4. **Issues are vertical slices.** `to-issues` must produce independently grabbable tracer-bullet tasks, not horizontal layer tickets.
5. **Triage controls dispatch.** Only `ready-for-agent` / AFK tasks may be assigned to Codex without human input.
6. **The assigned implementer implements; CCA judges.** Claude Code is the default implementer and repairer (ADR 0008); Codex Front implements tasks explicitly assigned to `codex`. GitHub CCA is the default completion judge.
7. **TDD is default for behavior work.** One failing behavior test, minimal code, pass, repeat. Refactor only after green.
8. **No implementation is complete until CCA and MergeGate agree.** Green CI is necessary but not sufficient.
9. **Repair is bounded.** CCA failures become repair packets. Default automatic repair limit is 3 attempts.
10. **Evidence is durable.** Important decisions and verification must live in GitHub and/or `.shiki/`, not chat memory.
11. **Spec Freeze gates execution.** No plan runs without an operator-approved `spec_freeze` block (ADR 0009). Post-freeze scope changes require an operator-approved, recorded Spec Amendment; non-scope-moving interpretations are recorded in the Assumption Log.

## Phase 0 — Repository Setup

Required before the first serious Goal:

- Run `setup-matt-pocock-skills` if issue tracker, triage labels, or domain docs are missing.
- Install or define `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `docs/adr/`, and `docs/agents/`.
- Define GitHub labels for lifecycle, risk, runtime, skill, CCA, and MergeGate states.
- Define PR and issue templates.
- Define `.shiki/` templates and schemas.
- Configure CI checks required by branch protection.
- Configure CCA workflow or documented equivalent.

Exit criteria:

- A new Goal issue can be created.
- A PR can be judged by CCA.
- MergeGate can block merges through required checks or equivalent governance.

## Phase 1 — Goal Seek

Inputs:

- User request.
- Existing repo context.
- Prior Goals/issues if relevant.

Outputs:

- Goal issue.
- Completion criteria.
- Non-goals.
- Risk level.
- Initial Skill Gate selection.
- Initial evidence requirements.

Hard stop conditions:

- Outcome is ambiguous.
- Completion cannot be verified.
- Risk level cannot be classified.
- Guardian approval is obviously required.

## Phase 2 — grill-with-docs

Default for non-trivial Goals.

Process:

- Challenge terms against `CONTEXT.md`.
- Check ADRs before reopening decisions.
- Inspect code when code can answer a question.
- Ask one question at a time when the operator is available.
- Provide a recommended answer with each question.
- Record resolved terms in `CONTEXT.md`.
- Create ADRs only for hard-to-reverse, surprising, tradeoff-based decisions.
- Convert unresolved load-bearing questions into HITL blockers.

Outputs:

- Resolved design-tree decisions.
- Updated glossary terms when needed.
- ADRs when justified.
- HITL blockers when decisions remain unresolved.

## Phase 3 — Context & Impact

For non-trivial Goals, produce this phase with a Workflow parallel
exploration sweep and record the run as evidence (CI-08).

Outputs must include:

- Relevant documents and ADRs.
- Relevant modules, interfaces, seams, callers, and tests.
- Candidate locks.
- Dependencies.
- Risk and architecture-gate triggers.
- Required verification surfaces.
- Likely repair surfaces.

This phase must be recorded in GitHub and/or `.shiki/`.

## Phase 4 — PRD

Use `to-prd` when the Goal is settled enough.

The PRD must include:

- Problem statement.
- Solution.
- User stories.
- Implementation decisions.
- Testing decisions.
- Out of scope.
- Further notes.
- Links to relevant Goal, domain terms, and ADRs.

Phase 4 ends with Spec Freeze: the operator explicitly approves the PRD, the
plan records a `spec_freeze` block (status, approved_by, source), and the SF
checklist applies. Phases 1-4 form Requirements Definition — one continuous
operator dialogue with exactly one approval gate on the happy path.

CCA may mark downstream PRs `insufficient_evidence` if the task contract relies on decisions missing from the PRD.

## Phase 5 — Issues and Task DAG

Use `to-issues` to create vertical-slice issues.

Each issue must include:

- Parent Goal/PRD.
- What to build.
- Acceptance criteria.
- Blocked-by.
- AFK/HITL classification.
- Runtime assignment.
- Required skills.
- Risk label.
- Candidate locks.
- CCA checklist profile.
- Verification commands or check names.

The Task DAG is created from these issues and their dependencies.

## Phase 6 — Codex TDD Implementation

Codex receives a self-contained handoff.

Required behavior:

- Work one task at a time.
- Use an isolated branch/worktree.
- Confirm dependencies and locks.
- Use TDD for behavior work.
- Write one test for one behavior, see it fail, implement minimal code, see it pass.
- Repeat until acceptance criteria pass.
- Refactor only after green.
- Run the `code-review` pre-PR gate on the full task diff, apply fixes on the
  task branch, and record the findings as ledger evidence and a
  `## Pre-PR code review` PR body section.
- Run required checks.
- Update PR body and ledger evidence.

Codex must not claim completion. It may state that implementation is ready for CCA.

Coordinator dispatch rule:

- If the task is ready and assigned to `claude-code` (the default), the
  coordinator runs `shiki runner claude --task-id T-XXXX`; if it is assigned to
  `codex`, the coordinator runs `shiki runner codex --task-id T-XXXX` instead
  of telling the user to run the runtime manually.
- The runner adapter is responsible for materializing the worktree, invoking
  the headless runtime (`claude -p` or `codex exec`) with the task handoff,
  and recording runner/Ledger evidence.
- Stop and ask the user only when the assigned runtime's auth/tooling is
  unavailable, dispatch is blocked, or Guardian approval is required.
- For a frozen Goal, prefer `shiki loop run --goal-id G-XXXX`: it executes the
  dispatch / PR / CCA-rerun / auto-merge (risk low/medium) / repair /
  unblock / completion cycle autonomously and stops only for the repair
  limit, Guardian gates, blocked evidence, or completion. A Spec Amendment is
  operator-initiated: interrupt the loop, re-grill, re-stamp, restart.

## Phase 7 — PR Evidence

Every implementation PR must include:

- Goal link.
- Task link.
- Scope and non-goals.
- Acceptance criteria checklist.
- TDD evidence.
- Pre-PR code-review evidence (`## Pre-PR code review` section and ledger reference).
- Verification commands and results.
- Changed locks.
- Risk level.
- CCA checklist profile.
- Ledger references.
- Known limitations or skipped checks.

A PR missing this information is not CCA-ready.

## Phase 8 — GitHub CCA Completion Judgment

CCA evaluates:

- Acceptance criteria.
- Diff scope.
- TDD evidence.
- CI/check results.
- Review state.
- Dependencies and locks.
- Risk and Guardian requirements.
- Ledger completeness.
- Documentation and ADR updates.
- Scope drift.

CCA emits one of:

- `complete`
- `repair_required`
- `blocked`
- `needs_guardian`
- `insufficient_evidence`

Only `complete` can continue to MergeGate readiness.

## Phase 9 — Repair Loop

If CCA or MergeGate rejects completion:

1. Generate a repair packet.
2. Assign repair to Codex unless explicitly overridden.
3. Codex fixes only the listed failures.
4. Codex reruns required checks.
5. CCA re-judges.
6. Stop after 3 failed automatic attempts.

Repair packets must be bounded. They must say what failed, what evidence is missing, what to change, what not to change, and how to verify.

## Phase 10 — MergeGate

MergeGate allows merge only when:

- Dependencies are complete.
- Locks are satisfied.
- Required checks pass.
- CCA verdict is `complete`.
- Review blockers are resolved.
- Risk approvals exist.
- Ledger is complete.
- PR metadata is complete.
- No architecture gate remains unresolved.

## Phase 11 — Goal Completion Judgment

After all task PRs merge, judge the parent Goal:

- All Task DAG nodes are complete or explicitly out of scope.
- Integration checks pass.
- User-facing completion criteria are satisfied.
- Ledger records all branches, PRs, checks, reviews, CCA verdicts, repairs, and merges.
- Remaining risks are explicit.
- Goal issue can be labeled `shiki:done`.

## Failure Classification

| Class | Meaning | Next action |
| --- | --- | --- |
| Implementation failure | Code does not satisfy behavior. | Codex repair with `tdd` or `diagnose`. |
| Evidence failure | Behavior might be done, but proof is missing. | Add checks, PR evidence, ledger entry, or artifacts. |
| Scope drift | PR changes unrelated behavior. | Revert unrelated changes or split into new Goal. |
| Dependency block | Upstream task or lock not complete. | Block until dependency resolves. |
| Architecture block | Decision needs ADR or deep-module discussion. | `grill-with-docs` or `improve-codebase-architecture`. |
| Risk block | Security/production/policy change needs human approval. | Guardian decision. |

## Phase 5.5 — Execution Decision Control

Before implementation, apply `docs/agents/decision-control.md`.

Required outputs:

- runtime assignment;
- branch/worktree allocation decision;
- lock grant;
- verification profile;
- dispatch guard result;
- CI/CD policy classification;
- Guardian requirement decision;
- ledger entry for the transition.

Hard stop conditions:

- no verification profile;
- unresolved dependency;
- lock conflict;
- HITL issue incorrectly marked AFK-ready;
- high-risk decision without Guardian approval;
- missing handoff;
- missing branch/worktree record when worktree execution is enabled.

No implementation runtime may edit files until the dispatch guard is true.

## Cross-Cutting Execution Control

Worktree, CI/CD, runtime, repair, and merge decisions are not ad hoc implementation details. They are governed by `docs/agents/execution-control.md`.

Default control split:

```text
Planner recommends -> Orchestrator/MergeGate authorizes -> Codex/GitHub Actions executes -> CI/CCA/Review/MergeGate verifies -> Guardian approves high-risk exceptions
```

A task is not dispatchable until its runtime, locks, branch/worktree requirement, CI profile, CCA checklist profile, risk level, and required evidence are known.

A PR is not mergeable until MergeGate has recomputed the effective policy from task metadata, labels, diff paths, risk level, CI status, CCA verdict, reviews, and ledger evidence.
