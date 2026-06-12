# Shiki Execution Control Policy

This document defines who decides worktree, branch, CI/CD, runtime, repair, and merge actions; when those decisions occur; and how Shiki prevents agents from acting outside the approved state machine.

## Core Control Rule

Separate every operational decision into four roles:

1. **Recommender**: proposes the right action based on docs, context, risk, and task state.
2. **Authorizer**: permits the state transition.
3. **Executor**: performs only the authorized action.
4. **Verifier**: proves whether the result satisfies the contract.

No single coding agent should hold all four roles for material source changes.

Default assignment:

| Role | Default actor | Scope |
| --- | --- | --- |
| Recommender | Claude Planner / Coordinator | Goal shaping, grill-with-docs, Context & Impact, CI profile suggestion, risk classification, worktree need, runtime suggestion. |
| Authorizer | MergeGate / Orchestrator / Guardian | Dispatch, locks, branch/worktree allocation, CCA readiness, merge readiness, high-risk approvals, CD approvals. |
| Executor | Claude Code (default implementer) / Codex Front / GitHub Actions / CD runner | TDD implementation, repair, tests, build, deploy job execution. |
| Verifier | CI, CCA, CODEOWNERS, reviewers, MergeGate | Test/check evidence, completion judgment, scope/risk review, merge gate enforcement. |

Codex may recommend fixes during repair, but it does not authorize scope changes, skip checks, select production deployment, self-approve, or judge completion.

## State Machine

```text
Goal proposed
  -> goal-ready
  -> grill-with-docs-complete
  -> context-impact-complete
  -> prd-ready
  -> spec-frozen
  -> issues-ready
  -> triaged
  -> dispatchable
  -> locks-reserved
  -> branch/worktree-allocated
  -> implementation-running
  -> PR-open
  -> CI-running
  -> CCA-judging
  -> CCA-complete | repair-required | blocked | needs-guardian | insufficient-evidence
  -> mergegate-ready
  -> merged
  -> CD-preview | CD-staging | CD-production-gated
  -> goal-complete
```

A state transition is valid only when the required evidence for the previous state exists in GitHub and/or `.shiki/`.

## Decision Matrix

| Decision | Who recommends | Who authorizes | When | Control surface | Evidence |
| --- | --- | --- | --- | --- | --- |
| Need `grill-with-docs` | Planner | MergeGate / Planner policy | Goal Seek, before PRD | `AGENTS.md`, `skill-gate.md`, issue labels | Goal note, questions, resolved assumptions |
| Need PRD | Planner | Planner policy / Guardian if disputed | After grill-with-docs | Goal issue, `.shiki/prds/` | PRD link and acceptance model |
| Issue slicing | Planner | MergeGate / triage | After PRD | `to-issues`, labels | Vertical-slice issues, Task DAG |
| Runtime assignment | Planner | Orchestrator / MergeGate | During triage | labels: `runtime:*`, `.shiki/tasks/*.json` | task handoff |
| Worktree needed | Planner / Orchestrator | Orchestrator / MergeGate | Before implementer dispatch | `.shiki/worktrees/*.json`, locks | registered worktree record |
| Branch name | Orchestrator | Orchestrator | Before implementer dispatch | branch policy | branch ref in task/ledger |
| Lock reservation | Context & Impact | MergeGate | Before dispatch and before merge | `.shiki/locks/`, labels, PR metadata | lock id, owner, scope |
| CI profile | Planner | MergeGate | During issue triage, rechecked at PR | labels, `.shiki/policy.yaml`, workflow matrix | required check list |
| Required status checks | Repo maintainer / Guardian | GitHub branch protection / rulesets | Repository setup, updated via ADR | GitHub settings | branch/ruleset config, ADR |
| CCA judgment | CCA | GitHub required check / MergeGate | Every non-draft PR update | GitHub Action check | structured CCA verdict |
| Repair assignment | CCA | MergeGate | After failed CCA/CI/review | repair packet, issue/PR comment | repair id and attempt count |
| Merge readiness | MergeGate | GitHub branch protection / Guardian if needed | After CCA complete | required checks, review rules, rulesets | mergegate check output |
| Preview deployment | CD policy | GitHub Actions / environment rules | PR or post-merge, depending repo | workflow and environment | deployment record |
| Staging deployment | CD policy | environment gate / Guardian if needed | After merge or after CCA complete | workflow and environment | deployment record, approvals |
| Production deployment | Release owner / Guardian | environment required reviewers / deployment protection | Release window only | GitHub environment, deployment protection, release issue | approval, release notes, rollback plan |
| Rollback | CD monitor / Guardian | Release owner / Guardian | After deploy failure or incident | deployment workflow | rollback commit/deploy evidence |

## Worktree Policy

Here, **worktree** means an isolated Git working tree used by a local or self-hosted agent runner for one task or repair packet.

### Default

- One executable task maps to one branch.
- A worktree is used only when the executor is local/self-hosted or when parallel execution would otherwise collide in one checkout.
- GitHub-hosted Actions normally use isolated checkouts and do not need persistent Shiki worktrees.

### Who decides

- Planner may suggest `worktree:required` during Context & Impact.
- Orchestrator authorizes and creates/registers the worktree at dispatch.
- Codex receives an assigned path and branch. Codex must not invent extra worktrees or switch to unrelated branches.
- MergeGate validates that locks and PR metadata match the registered worktree before CCA/MergeGate readiness.

### When required

Use `worktree:required` when any of these apply:

- Multiple agent tasks may run concurrently in the same repository.
- The task is long-running and should not block other checkouts.
- The task needs a clean branch while another task is under review.
- A repair packet must be isolated from an ongoing implementation branch.
- A risky or large change needs a disposable execution surface.

Use `worktree:not-required` when:

- The task is documentation-only and serial.
- The task is executed entirely in a GitHub Actions checkout.
- There is no concurrent local execution.
- The action is read-only planning, CCA, or review.

### Worktree lifecycle

```text
planned -> registered -> active -> PR-open -> frozen -> merged/abandoned -> archived -> removed
```

Required worktree record:

```json
{
  "task_id": "T-0001",
  "goal_id": "G-0001",
  "branch": "shiki/T-0001-short-slug",
  "path": "../.worktrees/T-0001-short-slug",
  "runtime": "claude-code",
  "state": "active",
  "locks": ["path:src/auth/*"],
  "created_by": "shiki-orchestrator",
  "created_at": "ISO-8601",
  "pr": null
}
```

## CI Policy

CI is not selected by Codex at implementation time. CI profile is part of the task contract.

### CI profile selection

Planner suggests a CI profile during `to-issues` / triage. MergeGate re-evaluates it at PR time using labels, diff paths, risk level, and task metadata.

Default profiles:

| Profile | Trigger | Required checks |
| --- | --- | --- |
| `ci:docs` | docs-only change | markdown/docs lint if configured |
| `ci:unit` | localized code behavior | lint, typecheck, unit tests |
| `ci:integration` | API, DB, adapters, cross-module behavior | lint, typecheck, unit, integration tests |
| `ci:e2e` | user journey, UI route, auth/session, critical flows | lint, typecheck, unit, integration, e2e |
| `ci:security` | auth, secrets, permissions, dependency/security-sensitive surfaces | normal checks plus security scans or manual Guardian review |
| `ci:infra` | workflows, deployment, Docker, Terraform, env config | workflow validation, dry-run/plan checks, Guardian review where needed |
| `ci:full` | high-risk or broad change | all repository-required checks |

MergeGate must fail when the selected profile is weaker than the diff requires.

### CI authority

- Codex may run local checks and report output.
- GitHub Actions is the durable CI source of truth.
- CCA maps CI/check evidence to acceptance criteria.
- MergeGate blocks merge if required checks are missing, failed, stale, skipped without justification, or run against the wrong SHA.

## CD Policy

Deployment is not part of normal Codex implementation unless the task is explicitly a deployment task.

| Environment | Default timing | Authorizer | Required control |
| --- | --- | --- | --- |
| Preview | PR open/synchronize when safe | CD workflow policy | no production secrets; ephemeral or isolated target |
| Staging | after CCA complete or after merge, repo-specific | environment gate / release owner | deployment check and rollback note |
| Production | release window after merge | Guardian / required reviewers | protected environment, release issue, rollback plan, monitoring signal |

Production deployment requires explicit authorization. A coding agent must not decide production rollout, bypass environment rules, or approve its own deployment.

## GitHub Enforcement Controls

Use GitHub controls as hard gates wherever possible:

- Required status checks for CI, CCA, and MergeGate.
- Branch protection or rulesets to prevent merges without required checks and reviews.
- CODEOWNERS for domain ownership review.
- Protected environments with required reviewers for staging/production deployment.
- Custom deployment protection rules when external readiness systems must approve release.
- Reusable workflows to standardize CI/CD across repositories.

Prompt instructions are useful, but GitHub gates are authoritative for merge and deployment.

## Labels

Suggested label families:

```text
stage:goal-ready
stage:prd-ready
stage:issues-ready
stage:triaged
state:dispatchable
state:blocked
runtime:codex
runtime:claude
runtime:github-actions
worktree:required
worktree:not-required
ci:docs
ci:unit
ci:integration
ci:e2e
ci:security
ci:infra
ci:full
cd:preview
cd:staging
cd:production
risk:low
risk:medium
risk:high
risk:critical
cca:complete
cca:repair-required
cca:blocked
cca:needs-guardian
mergegate:ready
mergegate:blocked
repair:attempt-1
repair:attempt-2
repair:attempt-3
repair:limit-reached
```

Labels are not sufficient by themselves. MergeGate must compute the effective policy from labels, diff, task metadata, and `.shiki/policy.yaml`.

## Control Mechanism

Implement Shiki control as a deterministic policy check, not only as agent instructions.

Minimum commands a Shiki CLI or scripts should expose:

```text
shiki goal check <goal-id>
shiki plan check <goal-id>
shiki dispatch check <task-id>
shiki worktree allocate <task-id>
shiki ci profile <pr-number>
shiki cca check <pr-number>
shiki mergegate check <pr-number>
shiki repair packet <pr-number>
shiki deploy check <environment> <ref>
```

Each command should return machine-readable JSON and a non-zero exit code when the state transition is not allowed.

## Non-Negotiable Blocks

Always block when any of these are true:

- Codex tries to implement without a task handoff or repair packet.
- A task has unresolved `blocked-by` dependencies.
- A task lacks required locks or has conflicting locks.
- A worktree is unregistered or points at the wrong branch.
- A PR has no task id, acceptance criteria, verification evidence, or CCA profile.
- CI profile is weaker than the changed files/risk require.
- CCA verdict is not `complete`.
- Production deployment lacks environment approval and rollback plan.
- A high/critical risk action lacks Guardian approval.
- Any actor attempts to bypass branch protection, required reviews, CCA, or MergeGate.
