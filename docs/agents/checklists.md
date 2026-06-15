# Shiki Checklists

These checklists are designed to be evaluated by humans, Claude Code, CCA, and MergeGate.

Status values:

- `pass`
- `fail`
- `insufficient_evidence`
- `not_applicable`

Blocking values:

- `blocking`: must pass before progress.
- `advisory`: should be considered but does not block by default.

## G — Goal Readiness Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| G-01 | Goal has a stable issue or durable reference. | blocking |
| G-02 | Outcome is stated in user-facing or operator-facing terms. | blocking |
| G-03 | Completion conditions are verifiable. | blocking |
| G-04 | Non-goals are explicit. | blocking |
| G-05 | Risk level is assigned. | blocking |
| G-06 | Required skills are listed. | blocking |
| G-07 | Evidence requirements are listed. | blocking |
| G-08 | Guardian approval requirement is known. | blocking |

## GD — grill-with-docs Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| GD-01 | Plan was challenged against `CONTEXT.md`. | blocking for non-trivial Goals |
| GD-02 | Relevant ADRs were read or absence was recorded. | blocking for non-trivial Goals |
| GD-03 | Ambiguous terms were resolved or converted to HITL blockers. | blocking |
| GD-04 | Edge scenarios were considered. | advisory |
| GD-05 | Code reality was checked when code could answer. | blocking when applicable |
| GD-06 | New domain terms were added to `CONTEXT.md` when needed. | blocking when applicable |
| GD-07 | ADR was created or explicitly skipped using the ADR criteria. | blocking when applicable |

## CI — Context & Impact Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| CI-01 | Relevant docs and ADRs identified. | blocking |
| CI-02 | Relevant modules, interfaces, seams, callers, and tests identified. | blocking |
| CI-03 | Dependencies and blocked-by relationships identified. | blocking |
| CI-04 | Candidate locks identified. | blocking |
| CI-05 | Risk and architecture-gate triggers identified. | blocking |
| CI-06 | Verification surfaces identified. | blocking |
| CI-07 | Likely repair surfaces identified. | advisory |
| CI-08 | Context & Impact was produced by a Workflow parallel exploration sweep with the run recorded as evidence. | blocking for non-trivial Goals |

## PRD — PRD Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| PRD-01 | Problem statement exists. | blocking |
| PRD-02 | Solution is stated from user/operator perspective. | blocking |
| PRD-03 | User stories are extensive enough for issue slicing. | blocking |
| PRD-04 | Implementation decisions are recorded without volatile code detail. | blocking |
| PRD-05 | Testing decisions are recorded. | blocking |
| PRD-06 | Out-of-scope items are explicit. | blocking |
| PRD-07 | Relevant glossary/ADR links are present when applicable. | blocking |

## SF — Spec Freeze Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| SF-01 | The operator explicitly approved the PRD/requirements before freeze. | blocking |
| SF-02 | Required external scopes and permissions (scope inventory) were enumerated before freeze. | blocking |
| SF-03 | The freeze is recorded as a `spec_freeze` block in the plan and as ledger evidence. | blocking |
| SF-04 | Scope changes after freeze went through an operator-approved, recorded Spec Amendment. | blocking when applicable |
| SF-05 | Non-scope-moving interpretations are recorded in the Assumption Log, not hidden. | blocking when applicable |

## ISS — Issue / Task Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| ISS-01 | Issue links to parent Goal or PRD. | blocking |
| ISS-02 | Issue is a vertical slice, not a horizontal layer task. | blocking |
| ISS-03 | Acceptance criteria are concrete and checkable. | blocking |
| ISS-04 | Blocked-by state is explicit. | blocking |
| ISS-05 | AFK/HITL classification is explicit. | blocking |
| ISS-06 | Runtime assignment is explicit. | blocking |
| ISS-07 | Required skills are explicit. | blocking |
| ISS-08 | Risk label is present. | blocking |
| ISS-09 | Candidate locks are listed. | blocking |
| ISS-10 | Verification commands or required checks are listed. | blocking |
| ISS-11 | CCA checklist profile is listed. | blocking |

## H — Implementer Handoff Checklist (Claude Code or Codex)

| ID | Item | Blocking |
| --- | --- | --- |
| H-01 | Goal id and task id are present. | blocking |
| H-02 | Branch/worktree target is present. | blocking |
| H-03 | Scope and non-goals are present. | blocking |
| H-04 | Dependencies and locks are present. | blocking |
| H-05 | Relevant docs, ADRs, modules, and tests are referenced. | blocking |
| H-06 | Required skills are stated. | blocking |
| H-07 | TDD expectations are stated. | blocking when behavior work |
| H-08 | Acceptance criteria are included. | blocking |
| H-09 | Verification commands are included. | blocking |
| H-10 | Evidence the implementer must produce is included. | blocking |
| H-11 | Prohibited changes are stated. | blocking |
| H-12 | The handoff includes a `## Distilled Rules` section that either lists the applicable active distilled rules (each with its MEM id) or states `none applicable`. CCA verifies only the section's presence and the applicable / none-applicable marker — never the rule selection or rule content (no double authority between distilled rules and checklists). | blocking when applicable |

## TDD — Implementation Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| TDD-01 | Behavior work used public-interface tests. | blocking when applicable |
| TDD-02 | First meaningful change started with a failing test, unless no correct seam exists and this is documented. | blocking when applicable |
| TDD-03 | Tests describe behavior, not implementation details. | blocking |
| TDD-04 | Minimal code was added for each behavior. | advisory |
| TDD-05 | Refactor happened only after green. | blocking when refactor exists |
| TDD-06 | Required tests pass. | blocking |
| TDD-07 | No speculative features were added. | blocking |
| TDD-08 | Missing test seam is documented and architecture follow-up created when applicable. | blocking when applicable |

## PR — Pull Request Evidence Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| PR-01 | PR links Goal and task. | blocking |
| PR-02 | PR states scope and non-goals. | blocking |
| PR-03 | PR lists acceptance criteria. | blocking |
| PR-04 | PR maps changed behavior to tests/checks. | blocking |
| PR-05 | PR includes TDD evidence or justified exception. | blocking |
| PR-06 | PR includes verification commands/results. | blocking |
| PR-07 | PR states changed locks. | blocking |
| PR-08 | PR states risk level and approvals. | blocking |
| PR-09 | PR states CCA checklist profile. | blocking |
| PR-10 | PR links ledger entries or evidence artifacts. | blocking |
| PR-11 | PR discloses skipped checks or manual verification needs. | blocking when applicable |
| PR-12 | PR includes pre-PR code-review evidence (ledger entry naming the skill plus a `## Pre-PR code review` body section) or a justified, recorded exception per the exception classes in `skills/engineering/code-review/SKILL.md`. | blocking |

## V — Verification Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| V-01 | Required unit/integration/e2e tests passed or justified. | blocking |
| V-02 | Required lint/typecheck/build passed or justified. | blocking |
| V-03 | Required migration/security/manual checks passed or justified. | blocking when applicable |
| V-04 | CI status is available and current for the PR head SHA. | blocking |
| V-05 | Verification evidence is durable. | blocking |
| V-06 | Failing or flaky checks are classified. | blocking when applicable |

`shiki doctor --json --target .` may be used as operator readiness evidence for
runtime auth, provider config, git origin, required workflows/checks,
CODEOWNERS, manifest layout, runtime assignments, and contract drift.
`shiki doctor --online` may add live GitHub readiness evidence for repository
existence, secrets, branch protection, required checks, code-owner review, and
workflow permissions. Doctor evidence supplements validation, CCA, MergeGate,
and Guardian approval; it does not replace them.

When CCA artifact evidence is in scope, verification must include the CCA
evidence manifest. The `CCA evidence manifest` records workflow run metadata,
artifact metadata, PR/head identity, and required file digests for
`shiki-cca-evidence`.

When `.shiki` layout or schema changes are in scope, migration evidence must
include the migration registry status, `.shiki/migrations/state.json`, pending
migration count, and whether `shiki migrate apply` was dry-run or executed with
the explicit execute gate.

When `.shiki` state classes are in scope, verification must include
`.shiki/manifest.json` state class validation, MergeGate classification for
unknown and forbidden `.shiki/**` paths, and append-only-evidence coverage for
ledger entries.

## CCA — Completion Judgment Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| CCA-01 | CCA read Goal, PRD/task, PR, diff, checks, reviews, and ledger inputs. | blocking |
| CCA-02 | Every acceptance criterion has status and evidence. | blocking |
| CCA-03 | Required skills were invoked or exceptions recorded. | blocking |
| CCA-04 | Diff is scoped to the task. | blocking |
| CCA-05 | TDD evidence is present when required. | blocking |
| CCA-06 | Required checks pass on current head SHA. | blocking |
| CCA-07 | Dependencies and locks are satisfied. | blocking |
| CCA-08 | Risk and policy-backed Guardian requirements from `.shiki/guardian-policy.json` are satisfied. | blocking |
| CCA-09 | Ledger evidence is complete. | blocking |
| CCA-10 | Verdict is one of the allowed statuses. | blocking |
| CCA-11 | Non-complete verdict includes precise next action. | blocking |

## RP — Repair Packet Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| RP-01 | Repair packet has id, task id, PR number, and attempt number. | blocking |
| RP-02 | Failing checklist items are listed. | blocking |
| RP-03 | Failing acceptance criteria are listed. | blocking |
| RP-04 | Minimal required change is stated. | blocking |
| RP-05 | Prohibited changes are stated. | blocking |
| RP-06 | Required skill is stated. | blocking |
| RP-07 | Verification commands/checks are stated. | blocking |
| RP-08 | Evidence the implementer must produce is stated. | blocking |
| RP-09 | Stop condition is stated. | blocking |

## MG — MergeGate Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| MG-01 | Dependencies complete. | blocking |
| MG-02 | Locks satisfied. | blocking |
| MG-03 | Required checks passed on current head SHA. | blocking |
| MG-04 | CCA verdict is `complete`. | blocking |
| MG-05 | Review blockers resolved. | blocking |
| MG-06 | Risk approvals complete. | blocking |
| MG-07 | Architecture gate resolved. | blocking when applicable |
| MG-08 | Ledger complete. | blocking |
| MG-09 | PR metadata complete. | blocking |
| MG-10 | Merge strategy and target branch are correct. | blocking |
| MG-11 | Runtime CCA/MergeGate evidence comes from workflow artifacts, not PR-committed `.shiki/gha` files. | blocking |
| MG-12 | Protected `.shiki` task, goal, ledger, lock, and repair changes are scoped to the current task and compared against base branch state. | blocking |
| MG-13 | Live GitHub PR state is refreshed immediately before policy evaluation and matches the checked-out head SHA. | blocking |
| MG-14 | `.shiki/manifest.json`, `.shiki/README.md`, install/create directories, and commit exclusions remain consistent. | blocking |
| MG-15 | Required checks in `.shiki/config.yaml` match actual workflow job display names from structured workflow parsing. | blocking |
| MG-16 | Workflow permissions, triggers, and Shiki job names match the validator contract. | blocking |
| MG-17 | Workflow lint, shellcheck, and Node 24 compatibility validation pass or have an explicit bounded exception. | blocking |
| MG-18 | `scripts/shiki.py` remains a thin executable shim and required `shiki_*` CLI modules import, install, and stage without side effects. | blocking |
| MG-19 | Runtime registry names, config role assignments, task `assigned_runtime` values, docs, and install/staging coverage remain consistent. | blocking |
| MG-20 | GitHub provider configuration validates provider kind, host, remote protocol, repo slug, canonical remote URL, API base URL, and legacy `.shiki/repo.json` compatibility. | blocking |
| MG-21 | `.shiki` state classes classify changed paths, block unknown/runtime/cache/local-only paths, and preserve append-only-evidence rules. | blocking |

## GC — Goal Completion Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| GC-01 | All required Task DAG nodes are complete or explicitly out of scope. | blocking |
| GC-02 | All merged PRs link to the Goal. | blocking |
| GC-03 | Goal-level completion criteria are satisfied. | blocking |
| GC-04 | Integration checks pass. | blocking |
| GC-05 | Ledger includes all task, PR, check, CCA, repair, and merge evidence. | blocking |
| GC-06 | Remaining risks are explicit. | blocking |
| GC-07 | Policy-backed Guardian approval exists when required. | blocking |
| GC-08 | Goal issue is ready for `shiki:done`. | blocking |

## Validator Hardening Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| VH-01 | Workflow validation uses structured extraction rather than raw substring matching. | blocking |
| VH-02 | Required check names are compared with actual workflow job display names. | blocking |
| VH-03 | Unsupported JSON Schema features fail closed with clear errors. | blocking |
| VH-04 | `actionlint` and `shellcheck` run in CI; local scripts may skip only when the tools are unavailable. | blocking |
| VH-05 | `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` is exercised and `ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION` is forbidden. | blocking |
| VH-06 | Node runtime defers are exact workflow/action/version entries documented in `docs/agents/node24-workflow-compatibility.md`; broad or wildcard defers are not accepted. | blocking |

## MB — Module Boundary Checklist

| ID | Item | Blocking |
| --- | --- | --- |
| MB-01 | `scripts/shiki.py` is a thin executable shim that delegates parser and main behavior to `shiki_cli.py`. | blocking |
| MB-02 | New `scripts/shiki_*.py` CLI modules import successfully and do not perform mutation or network work at import time. | blocking |
| MB-03 | CLI help remains compatible for existing commands and critical bootstrap flags. | blocking |
| MB-04 | Target installation and manifest staging include every module required by `scripts/shiki.py`. | blocking |
| MB-05 | `scripts/test_shiki_module_boundaries.sh` and `scripts/validate_shiki.py` enforce the module boundary contract. | blocking |
