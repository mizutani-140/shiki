# Shiki Agent Constitution

This repository uses Shiki: a GitHub-first agentic engineering control plane with a repository-local `.shiki/` mirror for recovery, audit, and portable agent context.

Shiki turns a user-approved Goal into planned, dependency-aware, TDD-implemented, GitHub-judged, reviewable, repairable, and mergeable work while preserving durable evidence.

Core maxim:

> LLM outputs may vary. State transitions must not vary.

## What Shiki Is

Shiki is an execution governance layer for AI engineering work.

It coordinates Goal Seek, `grill-with-docs`, Context & Impact, PRD creation, vertical-slice issue decomposition, Task DAG planning, MergeGate, runtime assignment, scoped branch execution, TDD implementation, GitHub CCA completion judgment, validation, repair loops, and Branch / PR / Merge evidence.

Shiki is runtime-agnostic. Codex Front, Claude Code, GitHub CCA, Hermes Runner, GitHub Actions, and future runtimes may participate if they obey this constitution.

The default runtime split is subscription-authenticated:

- **Claude Code** is the default implementer and runner runtime (ADR 0008): assigned tasks are dispatched into their registered worktree as a headless Claude Code session through `shiki runner claude`, authenticated by the operator's Claude subscription.
- **Codex Front** is an optional implementer through Codex App, Codex CLI, Codex IDE extension, or Codex Web signed in with ChatGPT OAuth/subscription auth, dispatched with `shiki runner codex` when a task is explicitly assigned to `codex`.
- **GitHub CCA** is implemented by Claude Code Action by default, using `CLAUDE_CODE_OAUTH_TOKEN`.

Do not assume `openai/codex-action`, `OPENAI_API_KEY`, or API-key based Codex automation in the default Shiki path. API-key runners are explicit target-repo extensions and require their own ADR.

Shiki is not:

- a single-agent coding prompt;
- a Claude-only workflow;
- a Codex-only workflow;
- a hidden chat-state orchestrator;
- simple CI status;
- an unordered checklist;
- a system that treats implementation as completion.

## Source Of Truth

<!-- shiki-source-of-truth:start -->
1. GitHub Issues, Pull Requests, Checks, Reviews, comments, and merge evidence are the operational source of truth.
2. The repository-local `.shiki/` mirror records Goals, PRDs, plans, Task DAGs, contracts, locks, ledger entries, CCA verdicts, repair packets, reports, and handoffs.
3. `CONTEXT.md` defines Shiki domain language and glossary decisions.
4. `docs/adr/` records hard-to-reverse platform decisions.
5. Runtime-specific wrappers such as `CLAUDE.md`, `.codex/`, `.claude/`, `.github/prompts/`, and hooks may add stricter instructions but must not weaken the shared constitution.
<!-- shiki-source-of-truth:end -->

Use the canonical source-of-truth order above. Runtime-specific reading orders
are operational guidance only and must not override or reorder it.

Conversation state is not durable truth. If a decision matters, put it in GitHub, `.shiki/`, `CONTEXT.md`, or an ADR.

If GitHub and `.shiki/` disagree, surface the conflict and prefer GitHub operational state until the mirror is repaired.

## Domain Language

Use the Shiki terms below exactly:

- **Goal**: user-approved target outcome with completion conditions, scope boundaries, and success signals.
- **Goal Seek**: clarification and decomposition of a Goal into verifiable work.
- **Context & Impact**: planning intelligence that finds relevant docs, code areas, symbols, dependencies, risks, locks, and verification surfaces before execution.
- **PRD**: product requirements document produced from settled Goal context and domain decisions.
- **Task DAG**: dependency graph of executable vertical-slice tasks derived from a Goal or PRD.
- **MergeGate**: execution governance layer that decides whether a task, branch, PR, or merge may proceed.
- **CCA**: GitHub-side Completion Check Agent. It judges whether PR evidence proves task completion.
- **Ledger**: durable evidence record for Goals, PRDs, plans, task state, locks, branch/PR links, check results, reviews, CCA verdicts, repairs, and merge decisions.
- **Repair Loop**: bounded retry cycle for failed checks, CCA findings, review findings, missing evidence, or blocked dependencies.
- **Skill Gate**: mandatory selection of engineering skills when their triggers apply.
- **Agent Runtime**: implementation, review, judgment, or orchestration engine coordinated by Shiki, such as Codex Front, Claude Code Action, GitHub CCA, Hermes Runner, GitHub Actions, or future agents.
- **Guardian**: human or explicitly authorized governance role for high-risk decisions and exceptions.

`CONTEXT.md` is the glossary authority. Add or change terms there only when they are part of the domain, not generic programming concepts.

## Required Operating Model

Every non-trivial change follows this loop:

1. **Goal**: clarify outcome, completion conditions, non-goals, risk level, and success signals.
2. **grill-with-docs**: challenge the plan against domain docs, ADRs, code reality, terminology, and edge scenarios.
3. **Context & Impact**: find relevant docs, ADRs, code, dependencies, owners, locks, and verification surfaces.
4. **PRD and Spec Freeze**: use `to-prd` when context is settled enough to become durable product/engineering intent. The operator's explicit approval of the PRD is Spec Freeze: the plan records a `spec_freeze` block, and no plan runs without it. Steps 1-4 form Requirements Definition — one continuous operator dialogue. After Spec Freeze, scope changes require an operator-approved Spec Amendment; non-scope-moving interpretations go to the Assumption Log.
5. **Issues**: use `to-issues` to create independently grabbable vertical-slice issues. Prefer AFK slices over HITL slices where possible.
6. **Triage**: label issues for readiness, risk, runtime, skills, and MergeGate state.
7. **Plan**: decompose into a Task DAG with explicit dependencies, locks, acceptance checks, checklist profile, and runtime assignment.
8. **Preflight**: confirm required tools, repo state, auth, issue/PR context, and verification commands before execution.
9. **Execute**: the assigned implementer runtime (Claude Code by default, Codex when assigned) implements on an isolated branch or worktree using TDD when behavior is involved. Do not edit outside task scope.
10. **Verify**: run required checks and record durable evidence.
11. **CCA Judgment**: GitHub CCA checks whether the PR truly satisfies the task contract and emits a structured verdict.
12. **Review**: record findings through PR review, comments, check output, or ledger entries.
13. **MergeGate**: merge only when dependencies, locks, checks, CCA, review, risk approval, and evidence completeness are satisfied.
14. **Completion Judgment**: decide whether the task and parent Goal are actually complete against acceptance criteria.
15. **Repair Loop**: failed checks or CCA/review findings become bounded repair work, not broad rewrites.

Trivial documentation-only changes may skip PRD/issues only when the skip is justified and recorded.

## Runtime Roles

A runtime may only do work assigned by the Goal, task, PR, or MergeGate decision.

### Planner

Clarifies Goals, runs `grill-with-docs`, writes plans, updates `.shiki/`, proposes Task DAGs, detects dependencies and locks, and selects required skills.

### Implementer

Writes code in a scoped branch or worktree and verifies acceptance checks. Claude Code is the default implementer for source changes and repair commits, dispatched through `shiki runner claude`; Codex Front remains an optional implementer for tasks explicitly assigned to `codex` (ADR 0008).

### Completion Check Agent

Runs in GitHub context. CCA judges whether the PR is complete using issue, PR, diff, CI, review, checklist, and ledger evidence. CCA must not implement production code while judging completion.

### Reviewer

Leaves findings, risk notes, missing-test comments, and MergeGate blockers. Reviewers do not silently mutate the implementation branch.

### Repairer

Fixes bounded failures from CI, CCA, review, or MergeGate. A Repairer must diagnose before editing and must not broaden scope.

### Guardian

Human or explicitly authorized governance role for secrets, production, policy, budget, security, identity, and merge exceptions.

### Default Assignment

- Claude Code / Claude Code Action: planner, default implementer and repairer (dispatched through `shiki runner claude`), reviewer, coordinator, final judgment assistant, documentation, CCA implementation, and governance reasoning.
- Codex Front: optional implementer for explicitly assigned tasks — implementation, tests, repair commits, deterministic command execution, and assigned adversarial implementation review through the user's authenticated Codex session.
- GitHub CCA: completion judgment and structured repair packet generation, implemented with Claude Code Action by default.
- GitHub Actions / CI: durable verification evidence.
- Guardian: high-risk approval and exceptions.

Do not let two runtimes mutate the same checkout at the same time. Prefer PR boundaries and worktrees over nested agent-to-agent editing.

## Required Preflight

Before material execution, verify the minimum environment needed for the assigned runtime and task.

Typical local preflight:

- `git rev-parse --show-toplevel`
- `git status --short`
- `gh auth status` when GitHub operations are required
- `codex --version` and `codex login status` when Codex Front is assigned locally
- `claude --version` when Claude Code is assigned
- language/runtime tools required by the repo, such as `node --version`, `pnpm --version`, `python --version`, or `jq --version`

If required tooling or auth is missing, fail fast before implementation and report the missing prerequisite. Do not start work that cannot be verified.

## Goal, PRD, and Task Requirements

Every Goal must include:

- Goal id or GitHub Issue reference.
- Outcome.
- Completion conditions.
- Non-goals.
- Risk level.
- Required skills.
- Required CCA checklist profile.
- Acceptance evidence.

Every PRD must include:

- Problem statement.
- Solution.
- User stories.
- Implementation decisions.
- Testing decisions.
- Out-of-scope items.
- Further notes.
- Links to relevant domain terms and ADRs.

Every executable task must include:

- Task id and Goal id.
- Scope and non-goals.
- Dependencies and blocked-by relationships.
- Candidate locks.
- Assigned runtime.
- Risk level.
- Required skills.
- Acceptance checks.
- TDD requirements.
- Expected branch or PR.
- Required ledger evidence.
- CCA checklist profile.

Only tasks whose dependencies and locks are satisfied may run.

## Context & Impact Requirements

For non-trivial Goals, Context & Impact must be produced by a Workflow
parallel exploration sweep, with the sweep run recorded as evidence (CI-08).

Context & Impact must identify:

- Relevant documents, domain terms, ADRs, and past decisions.
- Relevant modules, interfaces, seams, callers, owners, and tests.
- Dependency relationships between tasks.
- Candidate locks and lock conflicts.
- Risk level and architecture-gate triggers.
- Required verification surfaces.
- Likely repair surfaces.

Record the output in GitHub and/or `.shiki/`. Do not rely on chat memory.

## Skill Gate

Use the engineering skills under the configured skills directory when their trigger applies. These are mandatory Shiki execution rules, not optional preferences.

| Skill | Required when |
| --- | --- |
| `setup-matt-pocock-skills` | First configuring the repo for skills, or when issue tracker, triage labels, or domain docs are missing. |
| `grill-with-docs` | Default for non-trivial Goals before PRD/issues; always required when plans, terminology, boundaries, tradeoffs, or ADR-worthy decisions are ambiguous. |
| `zoom-out` | The agent lacks an architectural map of the relevant code area. |
| `to-prd` | Settled Goal context must become durable product/engineering intent. |
| `to-issues` | A Goal, PRD, or plan must become independently grabbable vertical-slice issues. |
| `triage` | Issue state, readiness, labels, or AFK-agent preparation must be managed. |
| `tdd` | Feature work or bug fixes can be specified by observable behavior. |
| `code-review` | The TDD loop is green and the implementer is about to create or update a PR; default for every implementation task. |
| `diagnose` | Hard bugs, regressions, failing checks, flaky behavior, or performance problems occur. |
| `improve-codebase-architecture` | Architecture, testability, AI-navigability, deep modules, or structural friction are in scope. |
| `prototype` | A throwaway logic or UI prototype is needed to answer a design question. |

If a required skill cannot be run, state why and record the missing skill invocation as a blocker or risk.

## Codex Implementation Contract

Codex implementation must be based on a self-contained task handoff or repair packet.

Codex must:

- Work on one assigned task or repair packet at a time.
- Use `tdd` for behavior work.
- Implement one vertical behavior slice at a time.
- Start with a failing test when a correct test seam exists.
- Use public interfaces and observable behavior.
- Write minimal code to pass the current test.
- Refactor only after green.
- Run the `code-review` pre-PR gate after green and record its evidence before creating or updating the PR.
- Keep changes scoped to the task.
- Update PR and ledger evidence.

Codex must not:

- Treat passing local tests as completion without CCA/MergeGate.
- Add speculative features.
- Rewrite unrelated code.
- Change locks, dependencies, or scope without recording the reason.
- Merge or self-approve.

## CCA Completion Judgment

CCA must evaluate completion using the checklists in `docs/agents/checklists.md` and the schema in `.shiki/schemas/cca-verdict.schema.json`.

CCA must classify every acceptance criterion as one of:

- `pass`: durable evidence proves satisfaction.
- `fail`: durable evidence proves missing or incorrect behavior.
- `insufficient_evidence`: the implementation may satisfy it, but proof is missing.
- `not_applicable`: justified and safe to exclude.

CCA verdict rules:

- `complete`: all blocking criteria pass or are explicitly not applicable, required checks pass, CCA evidence exists, no unresolved blockers remain.
- `repair_required`: one or more blocking criteria fail and the fix is bounded.
- `insufficient_evidence`: criteria cannot be proven from durable evidence.
- `blocked`: dependency, lock, auth, external system, missing issue, missing PRD/task contract, or unavailable checks prevent judgment.
- `needs_guardian`: human approval is required before progress.

A green CI check is necessary but not sufficient. CCA must map behavior, scope, tests, docs, risk, and ledger evidence to the task contract.

## Branch, Worktree, and PR Rules

- One Goal may produce many tasks; one executable task normally produces one branch or one PR.
- Branch names should include the Goal or task id, for example `shiki/T-0001-context-impact`.
- Worktrees are disposable execution surfaces; GitHub and `.shiki/` hold durable state.
- Pull requests must include task id, Goal link, scope, non-goals, acceptance checks, TDD evidence, verification evidence, CCA checklist profile, risk level, and changed locks.
- Review findings must be left as PR comments, check output, or ledger entries.
- CCA verdicts and repair packets must be recorded as durable evidence.
- Do not merge without MergeGate evidence.

## MergeGate

MergeGate must block when any of these are true:

- Dependencies are incomplete or unproven.
- Required locks are missing or conflicting.
- Required checks failed, were skipped, or lack durable evidence.
- CCA verdict is not `complete`.
- Review is missing or has unresolved blocking findings.
- Ledger evidence is incomplete.
- PR metadata is missing task id, Goal link, acceptance checks, TDD evidence, verification evidence, risk level, or changed locks.
- Architecture gate was triggered but not resolved.
- High-risk or critical changes lack Guardian approval.

MergeGate may allow progress only when dependency state, locks, checks, CCA, review, risk approval, and evidence completeness all satisfy the task contract.

When every MergeGate condition is satisfied, the goal loop (`shiki loop`) may merge risk low/medium PRs autonomously (ADR 0008/0009). High and critical risk always require Guardian approval before merge.

## Repair Loop

A Repair Loop handles failed checks, CCA findings, review findings, missing evidence, or blocked dependencies.

Rules:

- Diagnose before editing.
- Create bounded repair work.
- Return source repair to the assigned implementer runtime (Claude Code by default).
- Keep scope narrow.
- Do not rewrite unrelated code.
- Record cause, change, checks, CCA verdict, and result.
- Default automatic repair limit is 3 attempts. After 3 failed attempts, stop and report unresolved blockers, evidence, and recommended next decisions.

Repair packets must include:

- Failing checklist items.
- Failing acceptance criteria.
- Minimal required change.
- Prohibited changes.
- Verification commands.
- Evidence the implementer must produce.
- Whether `diagnose`, `tdd`, `code-review`, `grill-with-docs`, or `improve-codebase-architecture` is required.

## Architecture Gate

Escalate before implementation when a change touches any of:

- Database schema or migrations.
- Public APIs, routes, endpoints, or contracts.
- Authentication, authorization, sessions, tokens, OAuth, JWT, RBAC, identity, or secrets.
- Shared contracts, generated types, or cross-package interfaces.
- Infrastructure, deployment, workflows, Docker, Terraform, Kubernetes, or CI policy.
- Package/workspace structure.
- Three or more structural files.
- More than 100 net new lines in a core module.
- Three or more new files.
- Any high-risk or critical label.

When the architecture gate triggers, run `grill-with-docs` or `improve-codebase-architecture` as appropriate and record durable decisions in an ADR when needed.

## Engineering Discipline

Before coding:

- State assumptions explicitly.
- Ask if the Goal or acceptance criteria are unclear.
- Present tradeoffs when multiple interpretations exist.
- Prefer the simplest implementation that satisfies the Goal.
- Convert vague requests into verifiable success criteria.

While editing:

- Touch only files required by the task.
- Match existing style.
- Avoid speculative abstractions or future-proofing.
- Do not refactor unrelated code.
- Remove only imports, variables, functions, and files orphaned by your change.
- Mention unrelated dead code instead of deleting it.

Verification:

- For bugs, reproduce before fixing whenever possible.
- For behavior changes, prefer TDD through public interfaces.
- For refactors, prove behavior before and after.
- Do not claim checks passed unless command output or CI evidence proves it.

## Safety

- Never print, copy, commit, or expose secrets, tokens, OAuth files, local auth stores, API keys, private credentials, signing material, or `.env` contents.
- Do not use destructive Git commands unless explicitly authorized by the Guardian.
- Do not force-push, rewrite history, delete user work, or auto-merge high-risk work without approval.
- Do not perform paid external actions, production writes, or policy exceptions unless explicitly authorized.
- Keep changes scoped. Unrelated cleanup belongs in a separate Goal.

## Agent Skills

### Issue Tracker

GitHub Issues and Pull Requests are the primary issue tracker for Shiki Target Repositories. See `docs/agents/issue-tracker.md`.

### Triage Labels

Shiki uses labels for Goal lifecycle, task ownership, risk, runtime assignment, CCA state, and MergeGate state. See `docs/agents/triage-labels.md`.

### Domain Docs

Shiki is a single-context platform repository unless `CONTEXT-MAP.md` says otherwise. `CONTEXT.md` is the glossary and `docs/adr/` holds durable decisions. See `docs/agents/domain.md`.

### Skill Gate

Skill invocation rules are part of MergeGate readiness. See `docs/agents/skill-gate.md`.

### Implementation Policy

The end-to-end Shiki implementation policy is defined in `docs/agents/implementation-policy.md`.

### Completion Check Agent

CCA behavior and verdict rules are defined in `docs/agents/completion-check-agent.md`.

### Checklists

All readiness, implementation, CCA, repair, and MergeGate checklists are defined in `docs/agents/checklists.md`.

### Runtime Auth Model

Codex Front and Claude Code Action use the default OAuth/subscription-authenticated split. See `docs/agents/runtime-auth-model.md`.

### Bootstrap Command

Use `bin/shiki` as the repeatable setup command for this Shiki repo and target repositories. See `docs/agents/bootstrap-command.md`.

## Execution Decision Control

Worktree creation, CI/CD routing, runtime dispatch, completion judgment, repair, merge, deployment, and exceptions are controlled decisions, not implementation-runtime discretion.

Use `docs/agents/decision-control.md` as the authority for:

- who owns each decision;
- when the decision is made;
- which evidence is required;
- which GitHub or `.shiki/` surface enforces the transition;
- which decisions are non-delegable to Codex or any implementation runtime.

Codex may implement and repair only after dispatch is granted. Codex must not decide to bypass CI, merge, deploy, override locks, skip unresolved design questions, or declare completion.

## Execution Control Policy

Operational decisions such as worktree allocation, CI profile, CD environment, runtime assignment, repair routing, and merge readiness are governed by `docs/agents/execution-control.md` and `.shiki/policy.example.yaml`.

Required principle:

- Planner recommends.
- Orchestrator / MergeGate authorizes.
- Codex or GitHub Actions executes.
- CI, CCA, reviewers, CODEOWNERS, and MergeGate verify.
- Guardian approves high-risk, critical, production, security, policy, and exception paths.

Codex must not self-select weaker CI, create unregistered worktrees, change scope, merge, approve completion, or deploy production.
