# Contributing To Shiki

Shiki is a GitHub-first agentic engineering control plane. Contributions follow
the same governed loop that Shiki applies to any Target Repository: work flows
from an approved Goal through grilling, a PRD, vertical-slice issues, a scoped
task, a PR, CCA judgment, and MergeGate.

Read `AGENTS.md` (the shared constitution) and `CONTEXT.md` (the glossary)
before contributing. `CLAUDE.md` adds Claude Code-specific rules.

## Contribution Flow

```text
Goal -> grill-with-docs -> PRD -> Spec Freeze -> Issues -> Task -> PR -> CCA -> MergeGate
```

1. **Goal** — start from a user-approved Goal with an outcome, completion
   conditions, non-goals, risk level, and success signals.
2. **grill-with-docs** — for any non-trivial change, challenge the plan against
   domain language, Shiki SADRs, target ADRs, and code reality before writing a PRD.
3. **PRD** — capture settled product/engineering intent (`to-prd`).
4. **Spec Freeze** — the operator explicitly approves the PRD/requirements;
   the plan records a `spec_freeze` block and no plan runs without it
   (SADR-0009). Post-freeze scope changes require an operator-approved,
   recorded Spec Amendment.
5. **Issues** — decompose into independently grabbable vertical-slice issues
   (`to-issues`), then `triage` them for readiness, risk, runtime, and skills.
6. **Task** — register an executable task in the `.shiki/` mirror with scope,
   non-goals, dependencies, locks, runtime, risk, required skills, acceptance
   checks, and the expected branch (`shiki issue plan ...`).
7. **PR** — implement on a scoped branch and open one PR per task.
8. **CCA** — GitHub CCA judges completion and emits a structured verdict.
9. **MergeGate** — merge only when every gate passes.

Trivial documentation-only changes may skip the PRD/issues steps when the skip
is justified and recorded as evidence.

## Branch Naming

Use one Goal/task per branch, one branch per PR. Branch names include the Goal or
task id and a short slug:

```text
shiki/<GOAL-OR-TASK-ID>/<slice>-<slug>
# examples:
shiki/T-0001-context-impact
shiki/G-0012/docs-onboarding
```

Worktrees are disposable execution surfaces; GitHub and `.shiki/` hold durable
state.

## Scope And Locks

- Touch only the files your task declares. Acquire locks with
  `shiki lock acquire <task_id>` and never edit another task's files.
- Do not refactor unrelated code or perform unrelated cleanup; that belongs in a
  separate Goal.
- Remove only imports, variables, functions, and files orphaned by your change.

## Pull Request Requirements

Every PR uses `.github/pull_request_template.md` and must fill in:

- **Goal**, **Task**, **Issue**, **Runtime**, and **Risk**.
- **Scope** and **Non-Goals**.
- **Locks** registered for the task.
- **Required Skills** that were used or explicitly waived.
- **Acceptance Checks** with results.
- **Evidence**: branch, checks, CCA verdict, review, and ledger ids.
- The **MergeGate** checklist.

PRs must link the Goal and task issue and include TDD and verification evidence.
Reviewers leave findings as PR comments, check output, or ledger entries and do
not silently mutate the implementation branch.

## Running The Verification Baseline

Run these from the repository root and capture exit codes as evidence:

```bash
python3 scripts/validate_shiki.py            # validate the .shiki/ mirror
python3 -m py_compile scripts/*.py           # compile all CLI scripts
for sh in scripts/test_shiki_*.sh; do bash "$sh"; done   # script test suite
```

Add the acceptance checks specific to your task on top of this baseline. Do not
claim checks passed without command output or CI evidence, and do not broaden
scope to make tests pass.

## Evidence Is Durable

- Append **new** `.shiki/ledger/L-*.json` entries; never edit existing ledger
  entries.
- Do not hand-edit `.shiki/goals/*.json` or `.shiki/manifest.json` in a task PR.
- Conversation state is not durable truth. If a decision matters, record it in
  GitHub, `.shiki/`, `CONTEXT.md`, a Shiki SADR, or a target ADR.

## Architecture Gate

Escalate before implementation when a change touches schemas/migrations, public
APIs or contracts, auth/secrets/identity, shared/generated types, infrastructure
or CI policy, package structure, three or more structural files, more than 100
net new lines in a core module, three or more new files, or any high-risk label.
When triggered, run `grill-with-docs` or `improve-codebase-architecture` and
record durable decisions in a Shiki SADR or target ADR as applicable (see `docs/adr/`).

## Safety

- Never print, copy, commit, or expose secrets (see `SECURITY.md`).
- Do not use destructive Git commands, force-push, rewrite history, or auto-merge
  high-risk work without Guardian authorization.
- Do not perform paid external actions or production writes without explicit
  authorization.
