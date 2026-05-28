# Shiki Decision Control Matrix

This document defines who is allowed to decide execution-control questions, when those decisions are made, and how they are enforced.

Core rule:

> Agent runtimes may propose actions. State transitions are granted only by Shiki policy, GitHub checks, CCA verdicts, MergeGate, or Guardian approval.

## 1. Decision Ownership

| Decision | Owner | When | Required inputs | Output | Enforcement surface |
| --- | --- | --- | --- | --- | --- |
| Is the Goal clear enough to plan? | Planner + Guardian when needed | Goal intake | User request, existing docs, risk signals | Goal readiness verdict | Goal checklist, issue template, ledger |
| Must `grill-with-docs` run? | Planner / Skill Gate | Before PRD or issue split | Ambiguity, domain terms, ADR conflicts, unclear boundaries | Skill invocation or explicit skip rationale | Skill Gate checklist |
| Is a PRD required? | Planner | After Goal context stabilizes | Goal, `grill-with-docs` output, Context & Impact output | PRD required / not required | PRD checklist, CCA evidence check |
| Can work be split into issues? | Planner | After PRD or settled Goal | Acceptance criteria, non-goals, dependencies | Vertical-slice issues | `to-issues`, issue templates |
| Is an issue AFK-ready or HITL? | Triager | Issue triage | Scope, decisions remaining, testability, risk | `ready-for-agent` or HITL/blocker label | triage labels, dispatch check |
| Which runtime should execute? | Runtime Router | After issue is AFK-ready | Task type, risk, skills, repo constraints | Runtime assignment | task metadata, handoff schema |
| Should a branch/worktree be created? | Branch / Worktree Allocator | Immediately before implementation or repair | Task id, locks, dependencies, risk, current branches | Branch name, worktree path, owner, TTL | ledger, lock registry, GitHub branch rules |
| Can the agent start editing? | MergeGate dispatch check | Before mutation | Ready label, dependency closure, lock grant, runtime assignment | `dispatchable: true/false` | preflight workflow, lock file, task state |
| Which CI checks are required? | Verification Planner + CI Router | PRD/task creation; finalized when PR opens | Verification profile, changed files, risk, language stack | Required check set | GitHub Actions, required status checks |
| Can CI be skipped? | MergeGate + Guardian for exceptions | PR creation or check routing | Change type, risk, skip rationale | allowed / denied | required aggregate check, branch rules |
| Is the PR complete? | GitHub CCA | After PR evidence and CI results exist | Issue, PR, diff, checks, review, ledger | structured CCA verdict | required CCA status check |
| Should Codex repair? | Repair Controller | After CCA/CI/review failure | Failure class, checklist deltas, attempt count | repair packet | bounded repair loop, labels |
| Can the PR merge? | MergeGate | After CCA verdict and required checks | dependencies, locks, checks, reviews, risk approval, ledger | merge allowed / blocked | branch protection, rulesets, merge queue |
| Can deployment proceed? | Deployment Gate + Guardian when protected | Post-merge, tag, release, or environment promotion | environment, risk, checks, approvals | deploy allowed / blocked | GitHub environments, deployment protection rules |
| Can an exception override policy? | Guardian only | Any blocked high-risk state | explicit rationale, blast radius, rollback plan | exception decision | signed comment, label, ledger entry |

## 2. Non-Delegable Decisions

The following decisions must not be delegated to Codex or any implementation runtime:

- whether to bypass CI;
- whether to merge;
- whether to deploy to a protected environment;
- whether high-risk security, auth, data, billing, or infra changes are acceptable;
- whether unresolved design ambiguity may be ignored;
- whether a PR is complete;
- whether unrelated refactors may be added to an implementation PR;
- whether to mutate files outside granted locks.

Codex may produce evidence and proposed changes. It does not grant completion, merge, deploy, or exception authority.

## 3. Worktree Policy

Treat worktrees as execution isolation, not as a planning authority.

### Creation rule

A branch/worktree may be created only when all conditions are true:

- task state is `ready-for-agent`;
- dependencies are complete or explicitly not required;
- no lock conflict exists;
- runtime assignment is known;
- risk level does not require unresolved Guardian approval;
- verification profile exists;
- the task has a self-contained handoff.

### Naming convention

```text
branch:   shiki/<goal-id>/<task-id>-<slug>
worktree: .worktrees/<task-id>-<short-sha>
lock:     .shiki/locks/<task-id>.json
ledger:   .shiki/ledger/<goal-id>/<task-id>.jsonl
```

### Required worktree record

```json
{
  "task_id": "TASK-123",
  "goal_id": "GOAL-001",
  "branch": "shiki/GOAL-001/TASK-123-login-validation",
  "worktree_path": ".worktrees/TASK-123-a1b2c3d",
  "runtime": "codex",
  "owner": "shiki-runtime-router",
  "state": "active",
  "locks": ["src/auth/**", "tests/auth/**"],
  "created_at": "ISO-8601",
  "ttl_minutes": 1440,
  "base_ref": "main",
  "head_sha": null
}
```

### Repair worktree rule

Repairs should reuse the PR branch. If the original worktree is stale or missing, create a fresh worktree from the PR branch and append a repair worktree record. Never repair from an untracked local checkout.

### Cleanup rule

A worktree is removed only after one of these states is recorded:

- PR merged;
- task abandoned;
- repair limit reached and escalated;
- branch superseded by another task branch.

## 4. CI/CD Policy

CI/CD is policy-driven. LLM agents may not choose checks ad hoc.

### Verification profile

Each task must declare one profile:

```yaml
verification_profile:
  type: backend_feature | frontend_feature | docs_only | migration | infra | security | release
  required_checks:
    - lint
    - typecheck
    - unit
    - integration
  optional_checks: []
  forbidden_skips:
    - unit
    - security
  requires_guardian: false
```

### CI Router

The CI Router maps `verification_profile` plus changed files to workflows and jobs. It may add checks for risk or file changes. It may not remove required checks unless a Guardian exception is recorded.

### Required aggregate status

Each PR should expose a stable aggregate status check, for example:

```text
shiki-required-checks
shiki-cca-completion
shiki-mergegate
```

Branch protection or rulesets should require these stable checks. Avoid making optional matrix job names the only required checks, because matrix/check naming can change over time.

### CD Gate

Deployment requires a separate gate from merge. Merge means the code can enter the protected branch. Deploy means the code can affect an environment.

Deployment should require:

- successful post-merge or release checks;
- environment-specific approval when configured;
- rollback plan for high-risk environments;
- Guardian approval for production, security, data, billing, infra, or irreversible migration risk;
- deployment ledger entry.

## 5. State Machine

A task may only move forward through these states:

```text
draft
  -> grilled
  -> context_ready
  -> prd_ready
  -> issue_ready
  -> triaged
  -> dispatchable
  -> branch_allocated
  -> implementing
  -> implementation_ready_for_cca
  -> cca_complete
  -> mergegate_ready
  -> merged
  -> goal_reconciled
```

Failure states:

```text
blocked
needs_guardian
insufficient_evidence
repair_required
repairing
repair_limit_reached
abandoned
```

Each transition must have:

- actor;
- input evidence;
- checklist IDs satisfied or failed;
- resulting state;
- ledger entry.

## 6. Dispatch Guard

Before an implementation runtime starts, the dispatch guard must assert:

```json
{
  "dispatchable": true,
  "task_id": "TASK-123",
  "runtime": "codex",
  "dependencies_complete": true,
  "locks_granted": true,
  "guardian_approval_required": false,
  "verification_profile_present": true,
  "handoff_complete": true,
  "worktree_allocated": true
}
```

If any field is false, the runtime must not edit files.

## 7. CCA Verdict Schema

CCA must emit a structured verdict.

```json
{
  "task_id": "TASK-123",
  "pr": 456,
  "head_sha": "abc123",
  "verdict": "complete | repair_required | blocked | needs_guardian | insufficient_evidence",
  "can_merge": false,
  "failed_checklist_items": [],
  "failed_acceptance_criteria": [],
  "scope_drift": [],
  "missing_evidence": [],
  "repair_packet": null,
  "ledger_updates_required": []
}
```

Only this combination may advance to MergeGate readiness:

```json
{
  "verdict": "complete",
  "can_merge": true
}
```

## 8. Repair Control

Repair is not a second implementation project. It is a bounded response to a concrete failure.

Repair may start only when:

- CCA, CI, review, or MergeGate produced a concrete failure;
- a repair packet exists;
- attempt count is below the configured limit;
- original locks still apply or new locks are granted;
- the repair packet includes verification commands.

Default repair limit:

```yaml
repair:
  max_attempts: 3
  after_limit: needs_guardian
```

## 9. Recommended Policy Files

```text
.shiki/
  policies/
    execution.yaml
    runtime-routing.yaml
    worktree.yaml
    ci-router.yaml
    deployment.yaml
    repair.yaml
  schemas/
    cca-verdict.schema.json
    dispatch-guard.schema.json
    worktree-record.schema.json
    repair-packet.schema.json
```

## 10. Minimal Execution Policy Example

```yaml
version: 1

runtime_routing:
  implementation_default: codex
  planning_default: claude
  completion_judge: github_cca

worktree:
  enabled: true
  one_task_per_worktree: true
  deny_on_lock_conflict: true
  branch_prefix: shiki

ci:
  required_aggregate_check: shiki-required-checks
  cca_check: shiki-cca-completion
  mergegate_check: shiki-mergegate
  allow_agent_skip_ci: false

deploy:
  production_requires_guardian: true
  irreversible_migration_requires_guardian: true

repair:
  max_attempts: 3
  default_runtime: codex
  broaden_scope_allowed: false

mergegate:
  require_dependencies_complete: true
  require_locks_satisfied: true
  require_required_checks_green: true
  require_cca_complete: true
  require_review_blockers_resolved: true
  require_risk_approval: true
  require_ledger_complete: true
```

## 11. Operating Principle

Do not ask "which agent should decide?" Ask:

1. Is this a planning decision, execution decision, verification decision, governance decision, or deployment decision?
2. Which policy owns that class?
3. Which GitHub or Shiki artifact enforces the transition?
4. Which evidence proves the transition happened correctly?

This keeps autonomy at the execution layer while preserving deterministic control over state transitions.
