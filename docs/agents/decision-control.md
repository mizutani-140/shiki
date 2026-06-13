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
| Can the spec be frozen? | Operator only | End of Requirements Definition, before issue split | PRD/requirements summary, scope inventory (SF-02), open-question list | `spec_freeze` block in the plan + ledger evidence | `require_grilled_plan` (plan ingest/run), SF checklist |
| Can a frozen spec change? | Operator only (Spec Amendment) | When implementation contradicts the frozen spec | Contested decisions, scoped re-grill result | recorded amendment + re-stamped freeze | SF checklist, ledger evidence |
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
- whether to record an external AI reviewer's verdict as a human (operator) approval — `external_ai_guardian_review` artifacts must preserve the AI reviewer identity and never be transformed into operator approval;
- whether a frozen spec may change (Spec Amendment approval is operator-only);
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

### State ID and Ledger Write Policy

Historical Shiki state records may use legacy sequential IDs such as `G-0012`,
`T-0033`, and `L-0109`. Validators must continue to accept those records so
older evidence remains durable.

New Shiki-generated state records use collision-resistant, filename-safe IDs:

```text
<PREFIX>-YYYYMMDDTHHMMSSffffffZ-<8 hex>
```

Examples:

```text
G-20260603T121530123456Z-a1b2c3d4
T-20260603T121530123456Z-9f8e7d6c
L-20260603T121530123456Z-feedface
```

Contiguous or gapless IDs are not a safety property. Audit integrity comes from
durable evidence, matching filenames and JSON IDs, duplicate-ID rejection, and
append-only ledger writes.

Ledger writes are append-only. New ledger entries must be created with
no-overwrite semantics and retry on ID collision. Existing ledger files must not
be replaced during append. Mutable Shiki state records should be written through
atomic replace helpers where practical.

### Protected Mirror Evidence Policy

Runtime CCA and MergeGate evidence under `.shiki/gha` is workflow-generated and
must not be committed by PRs. PR-mutated `.shiki` mirror files are proposed
state changes, not trusted authority.

CCA artifact evidence is cross-checked with
`.shiki/gha/cca-evidence-manifest.json`, which records the workflow run,
artifact, PR/head metadata, and SHA-256 digests for required runtime evidence
files. New ledger entries can add machine-readable `evidence_refs` for PRs,
workflow runs, artifacts, and ledger digests so MergeGate does not rely only on
loose evidence text.

`.shiki/manifest.json` is the canonical repository-local layout contract for
the mirror. It defines official directories, required files, runtime-only
paths, install-time directory creation, and commit exclusions. `.shiki/README.md`
is human-readable documentation validated against the manifest; it is not the
source of truth.

Manifest entries also define state classes. State classes distinguish
repository-local mirrors, append-only evidence, governance policy, contracts,
migration state, workflow-runtime-evidence, generated data, cache data,
local-only data, and templates. MergeGate uses the class policy to block
unknown `.shiki/**` paths and PR-authored `workflow-runtime-evidence` such as
`.shiki/gha`.

Required empty tracked state directories are represented with `.gitkeep` or
created according to the manifest. Runtime-only directories such as
`.shiki/gha` are generated on demand and must not be committed.

`.shiki/migrations/state.json` records repository-local migration state and
applied migration evidence. It is tracked Shiki mirror evidence, not a
replacement for GitHub operational state, repair packets, ledger evidence, CCA,
MergeGate, or Guardian approval. `shiki migrate apply` is dry-run by default and
requires explicit `--execute` or `--i-understand` before writing migration
state; destructive migrations require `--i-understand`.

### CLI Module Boundary Policy

`scripts/shiki.py` is the executable Shiki CLI shim. Parser construction and
command dispatch live in `scripts/shiki_cli.py`; implementation behavior lives
in dependency-free standard-library `shiki_*` modules. The module boundaries are
documented in `docs/agents/shiki-cli-architecture.md` and validated by
`scripts/test_shiki_module_boundaries.sh`.

New Shiki CLI modules must not perform git, GitHub, filesystem mutation,
network, or command execution work at import time. Target installation and
manifest staging must include every module required by the shim.

### Runtime Registry Policy

Runtime assignment is validated against the canonical registry in
`scripts/shiki_runtime_registry.py` and documented in
`docs/agents/runtime-registry.md`. Runtime identity is distinct from runtime
role: `.shiki/config.yaml` assigns roles such as `front`, `planner`,
`implementer`, `completion_checker`, `reviewer`, and `verifier`, while task
`assigned_runtime` records the concrete runtime identity that will receive the
handoff.

The registry defines the supported runtime names, allowed roles, execution
mode, auth mode, required tools and secrets, related workflows, and capability
flags. It is a validation and adapter-contract surface only; it does not grant a
provider abstraction, migration framework, or registry-driven dispatch
authority.

### GitHub Provider Configuration Policy

Shiki is GitHub-first, but GitHub host and remote protocol assumptions must be
explicit. `scripts/shiki_provider.py` defines the dependency-free provider
contract for GitHub-compatible targets, including provider kind, host, SSH/HTTPS
remote protocol, web/API base URLs, canonical remote URL, repo API paths, and
`GH_HOST` mapping for GitHub CLI commands.

Only `provider=github` is supported. GitHub.com HTTPS is the compatibility
default for legacy `.shiki/repo.json` records. GitHub Enterprise-compatible
hosts may use custom host/API URL values, but this does not add non-GitHub
providers, a provider plugin system, or a migration framework. Existing-origin
mismatch remains a hard failure unless `--adopt-existing-repo` is explicitly
passed.

### Doctor Diagnostic Policy

`shiki doctor` is a diagnostic surface, not a decision owner and not an
auto-remediation tool. It may report runtime auth, provider config, git origin,
GitHub repository state, secrets, branch protection, required workflows/checks,
CODEOWNERS, manifest layout, runtime registry assignment, Node workflow policy,
and `validate_shiki.py` contract drift.

Default doctor checks are offline. Live GitHub repository checks require
`--online` and use `gh` with the configured provider host. Doctor must not read
or print secret values, mutate branch protection, set secrets, create labels,
submit reviews, or change files. A passing doctor report is useful readiness
evidence, but it does not replace CCA, MergeGate, Guardian approval, branch
protection, or required GitHub reviews.

### Workflow Runtime Compatibility Policy

Workflow runtime compatibility is part of MergeGate evidence. Shiki workflow
JavaScript actions must pin explicit versions, forbid
`ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION`, and set
`FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` on workflows that exercise JavaScript
actions. Official GitHub actions must use validator-approved Node
24-compatible majors, except for exact CCA / Claude workflow two-phase defers
recorded in the compatibility inventory.

The only accepted Node runtime defer is an exact workflow/action/version entry
recorded in `docs/agents/node24-workflow-compatibility.md`. A broad defer for a
workflow, action owner, wildcard version, or all third-party actions is not a
decision Shiki runtimes may make.

MergeGate refreshes live GitHub PR state immediately before policy evaluation
and uses that live state for PR labels, reviews, review decision, head SHA, and
status rollup. It also compares protected `.shiki` paths against a base-branch
snapshot before accepting proposed mirror changes.

Ledger evidence is append-only: new ledger files may be added only when linked
from the current task, while existing ledger files must not be modified or
deleted by PRs.

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

### Guardian approval evidence

Guardian approval for high-risk or critical work is defined by `.shiki/guardian-policy.json`. MergeGate accepts only policy-backed live GitHub sources:

- the required `guardian:approved` label, applied by a configured Guardian when label actor evidence is available;
- an approved GitHub review from a configured Guardian user or team;
- or a Guardian approval comment from a configured Guardian that includes the current PR head SHA when the policy requires it.

Label-only approval is not enough for high-risk or critical work when comment/head or review evidence is required. Ledger prose, PR body text, and structured ledger fields are not Guardian approval evidence. Negative or explanatory text such as "no Guardian approval evidence is present" must not satisfy MG-06.

### CCA Review Bridge

For solo/self-running operation, Shiki may submit an automated GitHub PR review approval only after CCA returns `complete`.

The CCA Review Bridge is not advisory Claude review and is not Guardian approval. It exists only to satisfy `required_review: true` after CCA has judged the PR complete and Guardian evidence is already present when required. The bridge must not approve if the authenticated GitHub identity is the PR author, and it must refresh `.shiki/gha/pr.json` after submitting the review so MergeGate policy reads current review state.

The bridge uses REST PR review creation after CCA verdict enforcement succeeds. If the repository's `GITHUB_TOKEN` cannot create approvals even when `can_approve_pull_request_reviews=true`, configure a non-author reviewer bot token or GitHub App installation token with `Pull requests: write`; do not weaken `required_review` or substitute advisory Claude review.

When `.shiki/config.yaml` sets `defaults.required_review: true`, Shiki branch protection must require at least one approving PR review. The CCA Review Bridge is the automation path that can satisfy that GitHub review requirement in solo operation after CCA and Guardian gates have already passed; it does not replace MergeGate policy or Guardian evidence.

### CODEOWNERS Governance

Critical Shiki governance paths are covered by `.github/CODEOWNERS` and owned by the configured Guardian owner. Branch protection must require code owner reviews so changes to workflows, MergeGate, CCA, bootstrap, core contracts, and root agent instructions receive machine-checkable owner review.

CODEOWNERS review is separate from advisory Claude review, Guardian approval evidence, and the CCA Review Bridge. The bridge may create the required GitHub review after CCA completes, but it does not replace path-owner governance for critical files.

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
  -> spec_frozen
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

## 12. Validator And Workflow Checks

Shiki validation treats workflow structure as a contract. Workflow names,
triggers, top-level permissions, job display names, job-level permissions, and
`uses:` actions are extracted structurally from workflow YAML. A required check
is satisfied only by an actual workflow job display name; comment text, run
output, job ids, or unrelated strings do not satisfy MergeGate required-check
configuration.

JSON Schema validation is intentionally bounded and dependency-free. Shiki
supports the schema keywords used by its own contracts and fails closed on
unsupported composition or reference features such as `$ref`, `oneOf`, `anyOf`,
`allOf`, `format`, `dependencies`, and `if`/`then`/`else`.
