# Runtime Auth Model

Shiki's default operator model is subscription-authenticated, not API-key-first.

## Default Runtime Split

- **Claude Code**: the default implementer and runner runtime (ADR 0008). `shiki runner claude` dispatches a headless `claude -p` session into the task's registered worktree, authenticated by the operator's Claude subscription login.
- **Codex Front**: an optional implementer surface for tasks explicitly assigned to `codex`. Use Codex App, Codex CLI, Codex IDE extension, or Codex Web signed in with ChatGPT OAuth/subscription auth.
- **Codex App External AI Guardian UI Adapter**: the operator-facing adapter that builds External AI Guardian Review Packets, drives ChatGPT Pro review through the UI, extracts the reviewer verdict, validates the fenced `external_ai_guardian_review` artifact, and relays only validated approvals to GitHub (ADR 0014). It consumes the deterministic `shiki guardian packet|prompt|verify-response` surfaces. **Claude Code implementer/repairer sessions must not drive this Guardian UI path for their own work** — that boundary keeps implementation distinct from approval routing.
- **Claude Code Action**: the GitHub Actions runtime for PR review, issue/PR automation, and MergeGate judgment. Use `CLAUDE_CODE_OAUTH_TOKEN` as the default secret.
- **GitHub**: the durable coordination surface for Issues, PRs, Checks, Reviews, comments, and merge evidence.
- **`.shiki/` mirror**: portable recovery and evidence mirror inside each target repo.

`docs/agents/runtime-registry.md` defines the machine-readable runtime identity
and runtime role contract used by `.shiki/config.yaml`, task
`assigned_runtime`, validator checks, and runtime status output. Auth mode is a
registry field, but the registry is not yet a doctor implementation or provider
abstraction.

## What This Means

Codex is not the default GitHub Actions backend in Shiki.

Each Agent Runtime still has its own login gate before it can invoke Shiki:

- Codex App / Codex CLI must be signed in with ChatGPT/Codex auth.
- Claude Code must be signed in before `/shiki` can run as a Claude slash command.
- GitHub CLI must be authenticated before Shiki can create repositories, issues, PRs, secrets, or branch protection.
  For GitHub Enterprise-compatible targets, Shiki injects `GH_HOST` from
  `.shiki/repo.json` provider config or CLI flags, so `gh auth login -h HOST`
  must be completed for that host.

Run `shiki doctor` to see which entrypoints are currently usable and whether the
repository is ready for Shiki operation. Offline doctor checks cover configured
runtime auth, provider metadata, git origin, workflows, required checks,
CODEOWNERS, manifest layout, runtime assignments, and contract drift. Online
checks are opt-in with `--online` and use `gh` to inspect repository existence,
secrets, branch protection, required checks, code-owner review, and workflow
permissions. See `docs/agents/shiki-doctor.md`.

A Claude Code error such as `Please run /login` or `API Error: 401 Invalid
authentication credentials` means Claude Code failed before Shiki received
control. Fix it with `claude auth login` or `/login` in Claude Code, or start the
same Shiki flow from Codex or a terminal with `shiki start` while Claude auth is
unavailable.

Do not assume `openai/codex-action` or `OPENAI_API_KEY` unless a target repository explicitly opts into an API-key based automation mode. The default Shiki loop is:

1. GitHub Issue or PR defines the Goal/task contract.
2. The assigned implementer performs implementation: Claude Code through `shiki runner claude` and the operator's Claude subscription by default, or Codex Front through the user's authenticated Codex session when the task is assigned to `codex`.
3. The implementer pushes a branch or opens a PR.
4. Claude Code Action reviews the PR through GitHub Actions using the Claude Code OAuth token secret.
5. MergeGate uses checks, review, locks, skills, and ledger evidence to decide readiness.

## Runtime Evidence Boundary

`.shiki/gha` is a GitHub Actions runtime directory. It may contain PR metadata,
changed-file lists, CCA verdicts, review bridge diagnostics, and MergeGate
results produced during a workflow run. Those files must not be committed by a
PR.

MergeGate policy refreshes live GitHub state immediately before evaluation and
checks the live PR head SHA against the checked-out commit. It treats PR-edited
`.shiki` files as proposed mirror changes and compares protected state against
the base branch `.shiki` snapshot before accepting ledger, task, goal, lock, or
repair evidence.

## Required Secrets

Default Claude Code Action secret:

- `CLAUDE_CODE_OAUTH_TOKEN`

Generate a long-lived token with `claude setup-token`. Shiki can set the GitHub
secret automatically during `start`, `init`, or `bootstrap-platform` only when
that token is already available in the current process environment as
`CLAUDE_CODE_OAUTH_TOKEN`. Claude Code login confirms the local interactive
runtime, but it does not by itself give Shiki a GitHub Actions token.

Do not store OAuth tokens in repository files, `.env`, logs, prompts, or `.shiki/` artifacts. Shiki must not read local Claude OAuth credential files to populate GitHub secrets.

## GitHub Provider Auth

Provider configuration is host/protocol configuration for GitHub-compatible
targets. It does not change Shiki's GitHub-first authority model. The GitHub CLI
remains the command backend for repo creation, issues, PRs, secrets, and branch
protection.

For GitHub.com, Shiki uses the existing default `gh` host. For GitHub
Enterprise-compatible hosts, Shiki passes `GH_HOST=HOST` to relevant `gh`
commands and records the host, API base URL, remote protocol, and canonical
remote URL in `.shiki/repo.json`. Legacy repo records without provider fields
are treated as GitHub.com HTTPS.

## Optional API-Key Mode

API-key based runners may be added by a target repository as an explicit extension. They are not the default Shiki template path.

If a repo opts into API-key mode, record the exception in an ADR and update the repo's `AGENTS.md`, `.shiki/config.yaml`, and workflow permissions.

## GitHub Actions Runtime Compatibility

Shiki workflows set `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` so validation
runs exercise GitHub Actions Node 24 compatibility. Official GitHub actions are
kept on Node 24-compatible major versions where Shiki can do so without changing
runtime semantics.

Workflow files that execute `anthropics/claude-code-action` are a bounded
exception during pull-request validation: the action requires the workflow file
content to match the default branch before it can exchange its OIDC token. Node
24 action upgrades for official actions in those workflow files must land
through a dedicated Guardian-approved workflow migration path. The inventory in
`docs/agents/node24-workflow-compatibility.md` records exact two-phase official
action defers for those workflow files and any remaining exact third-party
workflow/action/version exception. The validator records only explicit
workflow/action/version exceptions, not a general allowance for deprecated
official actions.

`ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION` is forbidden in Shiki workflows. If a
third-party action still emits a Node 20 deprecation warning and no compatible
release is available, record the specific action and a follow-up instead of
weakening workflow validation. Advisory Claude review, Guardian approval, and
the CCA Review Bridge remain separate from Node runtime compatibility checks.
