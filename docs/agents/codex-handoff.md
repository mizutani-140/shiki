# Codex Handoff

Codex Front is an optional implementer and repairer for Shiki source changes,
used when a task is explicitly assigned to `codex`. The default implementer is
Claude Code through `shiki runner claude` (ADR 0008); the handoff contract
below applies to both runtimes.

Run Codex through the operator-facing Codex App, Codex CLI, Codex IDE extension, or Codex Web signed in with ChatGPT OAuth/subscription auth. Do not assume `openai/codex-action` or `OPENAI_API_KEY` in the default Shiki path.

Codex should receive a self-contained handoff. It should not infer product decisions from chat history.

Coordinator rule: do not stop after printing a Codex command for the user to
run. For a ready task assigned to Codex, run:

```bash
shiki runner codex --task-id T-0001
```

Use `--target TARGET` when the current directory is not the Target Repository.
This is the autonomous implementation adapter: it materializes the task
worktree, invokes `codex exec` with the handoff, and records runner/Ledger
evidence. Ask the user only when Codex authentication/tooling is unavailable,
dispatch is blocked, or Guardian approval is required.

## Task Handoff Template

```markdown
# Codex Task Handoff

## Identity
- Goal id:
- Task id:
- Parent issue / PRD:
- Target branch/worktree:
- PR target branch:

## Runtime role
You are the Implementer for this Shiki task.
Do not claim completion. Produce implementation and evidence for GitHub CCA and MergeGate.

## Scope
- Build:
- Do not build:
- Files/modules likely in scope:
- Files/modules out of scope:

## Context
- Domain terms:
- Relevant ADRs:
- Relevant docs:
- Relevant modules/interfaces/seams:
- Similar tests/prior art:

## Dependencies and locks
- Blocked by:
- Required locks:
- Lock conflicts to avoid:

## Required skills
- [ ] tdd
- [ ] code-review
- [ ] diagnose
- [ ] zoom-out
- [ ] improve-codebase-architecture
- [ ] other:

## TDD expectations
- Public interface to test:
- Behaviors to test:
- First tracer-bullet behavior:
- Correct test seam:
- If no correct seam exists, document why and stop for architecture guidance.

## Acceptance criteria
- [ ] ...

## Verification commands
- `...`

## Evidence to produce
- PR body updates:
- TDD evidence:
- Check output:
- Ledger entry:
- Known skipped checks:

## Prohibited changes
- Do not change:
- Do not refactor:
- Do not add:

## CCA checklist profile
- Required checklists:
- Blocking items:
```

## Repair Handoff Template

```markdown
# Codex Repair Handoff

## Identity
- Repair id:
- Goal id:
- Task id:
- PR:
- Attempt:

## Runtime role
You are the Repairer for this bounded repair packet.
Fix only the failures listed here. Do not broaden scope.

## CCA verdict
- Verdict:
- Summary:

## Failing checklist items
- ...

## Failing acceptance criteria
- ...

## Required skill
- tdd | code-review | diagnose | grill-with-docs | improve-codebase-architecture | evidence-only

## Minimal required change
- ...

## Prohibited changes
- ...

## Verification commands
- `...`

## Evidence to add
- ...

## Stop condition
Stop if the repair requires a product decision, architecture decision, new dependency, lock conflict, or Guardian approval not listed in this packet.
```

## Codex Completion Statement

Codex may state:

> Implementation evidence is ready for CCA.

Codex must not state:

> The task is complete.

Completion is decided by CCA and MergeGate.
