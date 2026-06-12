# Runtime Registry

Shiki runtime identity is the durable name of an execution or judgment surface.
A runtime role is the job that identity may perform in a Goal or Task flow.

The registry in `scripts/shiki_runtime_registry.py` is the canonical
machine-readable contract for runtime names, allowed roles, execution mode, auth
mode, required local tools, required GitHub secrets, related workflows, and
capability flags. It is a contract definition, not a full adapter execution
engine.

## Descriptor Fields

Each runtime descriptor records:

- `name`: stable runtime identity used in `.shiki` state.
- `display_name`: human-readable name.
- `roles`: allowed runtime role names.
- `execution_mode`: one of `local_cli`, `github_action`, `workflow_job`,
  `human`, `external_runner`, or `placeholder`.
- `auth_mode`: one of `none`, `chatgpt_oauth`,
  `claude_subscription_oauth`, `github_token`, `github_secret`, `manual`, or
  `future`.
- `required_tools`: local tools needed before local execution.
- `required_secrets`: repository secrets needed by GitHub-hosted execution.
- `github_workflows`: related workflow display names.
- capability flags for local execution, GitHub execution, automated review,
  completion judgment, and handoff support.
- `experimental`, `deprecated`, and `requires_rationale` status.
- `description`: short operator-facing summary.

## Supported Runtimes

| Runtime | Roles | execution mode | auth mode | Notes |
| --- | --- | --- | --- | --- |
| `claude-code` | planner, implementer, runner, reviewer | local_cli | claude_subscription_oauth | Local Claude Code planning, review, and the default implementer/runner runtime (ADR 0008), dispatched with `shiki runner claude`. |
| `claude-code-action` | reviewer | github_action | github_secret | GitHub Actions reviewer using `CLAUDE_CODE_OAUTH_TOKEN`. |
| `codex` | implementer, runner | local_cli | chatgpt_oauth | Local Codex implementation and runner execution. |
| `codex-front` | front, implementer | local_cli | chatgpt_oauth | Operator-facing Codex front entrypoint used by `.shiki/config.yaml`. |
| `github-actions` | verifier | workflow_job | github_token | GitHub Actions validation and verifier jobs. |
| `github-cca` | completion_checker | workflow_job | github_token | GitHub-hosted CCA completion judgment. |
| `hermes-runner` | runner | external_runner | future | Future external runner placeholder. |
| `human` | human_gate, reviewer | human | manual | Manual HITL approval or review surface. |
| `other` | all roles | placeholder | future | Legacy fallback; new config use requires explicit rationale. |

## Role Assignment Validation

`.shiki/config.yaml` must define these runtime roles:

- `front`
- `planner`
- `implementer`
- `completion_checker`
- `reviewer`
- `verifier`

The validator checks each configured runtime name against the registry and then
checks whether that runtime supports the assigned runtime role. Task
`assigned_runtime` values must also reference a known runtime that can act as a
planner, implementer, runner, or manual human gate. This preserves historical
task records while rejecting unknown names.

The legacy `other` runtime remains valid for old evidence and emergency
extension points, but `.shiki/config.yaml` must include an explicit role
rationale such as `verifier_rationale` before assigning `other` to a required
role.

`shiki doctor` imports the registry, validates `.shiki/config.yaml` runtime role
assignments, and checks task `assigned_runtime` values against the same runtime
names. These doctor checks use the registry to decide which local auth checks
matter; they do not require Claude or Codex local auth when those local runtimes
are not configured for the target.

## Adapter Contract Boundary

Runner adapters are implemented in `scripts/shiki_runtime_adapters.py`. Each
`RunnerAdapter` binds a registry runtime name to:

- the required local tool;
- an auth check (`claude auth status` / `codex login status` probes);
- the headless execution command (`claude -p` / `codex exec -`) fed by the
  task handoff on stdin;
- a command label recorded in the EXEC runner record and Ledger evidence.

The shared runner machinery in `scripts/shiki_runtime.py` (worktree
materialization, evidence recording, task status transitions) is
runtime-agnostic: `shiki runner claude` and `shiki runner codex` dispatch
through the same `dispatch_runner_task` flow. Adding a runtime means adding
one adapter plus a registry role grant. The registry itself remains the
contract for safe name validation and deterministic status output.

## Out Of Scope

The following are intentionally not implemented by the registry and adapter
layer:

- runtime provider abstraction;
- GitHub host / protocol / provider configuration;
- migration framework changes;
- reviewer bot or GitHub App behavior;
- Guardian policy changes;
- CCA Review Bridge changes;
- branch protection or CODEOWNERS changes.
