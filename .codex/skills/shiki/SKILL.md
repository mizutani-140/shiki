---
name: shiki
description: Use when the user invokes Shiki, /shiki, or asks to run the GitHub-first agentic engineering control plane for Goal Seek, Context and Impact, Task DAG, Codex implementation, CCA completion judgment, MergeGate, or bounded repair loops.
---

# Shiki

Shiki is the user's GitHub-first, runtime-agnostic control plane for agentic
engineering.

## Canonical Source Of Truth

<!-- shiki-source-of-truth:start -->
1. GitHub Issues, Pull Requests, Checks, Reviews, comments, and merge evidence are the operational source of truth.
2. The repository-local `.shiki/` mirror records Goals, PRDs, plans, Task DAGs, contracts, locks, ledger entries, CCA verdicts, repair packets, reports, and handoffs.
3. `CONTEXT.md` defines Shiki domain language and glossary decisions.
4. `docs/adr/` records hard-to-reverse platform decisions.
5. Runtime-specific wrappers such as `CLAUDE.md`, `.codex/`, `.claude/`, `.github/prompts/`, and hooks may add stricter instructions but must not weaken the shared constitution.
<!-- shiki-source-of-truth:end -->

Codex CLI does not register this skill as a custom `/shiki` slash command. When
the user types `/shiki` in Codex CLI and gets "Unrecognized command", explain
that this is expected: invoke Shiki in Codex with natural language such as
"Shiki: create a new GitHub-backed target repo ..." or run the shell command
`shiki start ...`.

## Start

Run:

```bash
shiki status
shiki doctor
```

`shiki status` reports the installed Shiki platform/template root and default
config. Its `config.repo` value is not automatically the requested Target
Repository. Do not route work to `/Users/kio.mizutani/shiki` or
`mizutani-140/shiki` unless the user explicitly asks to work on the Shiki
platform repo itself.

Use `shiki doctor` to distinguish Shiki availability from Agent Runtime
authentication. If Claude Code reports `Please run /login` or `API Error: 401
Invalid authentication credentials`, the Claude Code adapter cannot run `/shiki`
until `claude auth login` or `/login` succeeds. Do not block the Codex path on
Claude auth; use `shiki start` from Codex or a terminal when Codex/GitHub are
ready.

When `/shiki` or Shiki is invoked without a clear target, first establish
whether the user wants a new GitHub-backed Target Repository or an existing
repository. For new repo requests, collect the target path and GitHub slug before
inspecting repo contents.

Then inspect the selected target repository's `AGENTS.md`, `CLAUDE.md`,
`CONTEXT.md`, `.shiki/`, `docs/agents/`, and open PR/issue state before changing
files.

If Shiki is not installed in the target repository, do not give the user a
manual sequence. Ask for the missing repo and Goal values one question at a
time, then run the one-command entrypoint:

```bash
shiki start TARGET --repo OWNER/NAME --goal "..." --outcome "..."
```

The default engineering Skill Gate directory is
`/Users/kio.mizutani/Documents/lead-os/skills/engineering` when present. The
start record, plan, and handoff must preserve the selected skills directory.

## Responsibilities

- Claude Code implements and repairs by default (`shiki runner claude`, ADR 0008); Codex implements and repairs tasks explicitly assigned to `codex`.
- Claude Code Action can act as GitHub-side CCA or reviewer.
- CCA judges completion.
- MergeGate authorizes state transitions and merge readiness.
- GitHub branch protection is the hard gate.

## Rules

- For non-trivial goals, enter through `grill-with-docs`. In a bootstrapped Target Repository the default flow is Goal mode: Requirements Definition (grill dialogue + Context & Impact + PRD) ends with the operator's explicit approval = Spec Freeze, recorded as the plan's `spec_freeze` block (`status: frozen`, `approved_by`, `source`). Plans without it are rejected by `shiki plan ingest`/`run`. Codex satisfies the Context & Impact sweep requirement with an equivalent recorded exploration (the Claude Workflow tool is not available in Codex).
- After Spec Freeze, scope-moving discoveries pause the task and return to the operator as a Spec Amendment; non-scope-moving interpretations are recorded in the Assumption Log.
- `/shiki` should guide the user through missing repo/Goal answers one question at a time and then run `shiki start`; direct `init`, `plan`, and `run` calls are lower-level fallback commands.
- Convert the settled `grill-with-docs` result into a machine-readable plan and run it with `shiki plan ingest` followed by `shiki run`.
- For unattended execution, queue the plan with `shiki daemon enqueue-plan` and process it with `shiki daemon run`.
- For headless runtime integration, use `shiki runner next` and `shiki runner execute` to pick up ready tasks and record execution evidence.
- When a ready task is assigned to claude-code (the default), run the implementation adapter with `shiki runner claude --target TARGET --task-id T-XXXX`. When it is assigned to Codex, run `shiki runner codex --target TARGET --task-id T-XXXX` instead of handing the user a manual command. Both create or reuse the task worktree, invoke the headless runtime (`claude -p` or `codex exec`) with the handoff, and record runner evidence. Stop for user input only when the assigned runtime's auth/tooling is missing, dispatch is blocked, or Guardian approval is required.
- Use Context and Impact before implementation.
- Keep tasks as vertical slices with explicit locks and verification.
- Use TDD for implementation work when behavior changes.
- Do not call implementation complete until GitHub evidence, CCA, and MergeGate support it.
- Do not use `shiki install-target` unless the user explicitly asks for local-only template copying.
- Do not bypass branch protection or use admin merge.
- If a workflow change needs a bootstrap exception, ask for explicit Guardian approval first.

## Commands

- `shiki install-global`
- `shiki start /path/to/repo --repo OWNER/REPO --goal "..." --outcome "..."`
- `shiki init /path/to/repo --repo OWNER/REPO`
- `shiki preflight --require-github`
- `shiki plan guide --prompt "..."`
- `shiki plan ingest --plan-file PLAN.json`
- `shiki run --plan P-0001`
- `shiki daemon enqueue-plan --plan-file PLAN.json`
- `shiki daemon run --once`
- `shiki runner next`
- `shiki runner execute --task-id T-0001 --command "..."`
- `shiki runner claude --task-id T-0001`
- `shiki runner codex --task-id T-0001`
- `shiki loop step --goal-id G-0001`
- `shiki loop run --goal-id G-0001`
- `shiki smoke live --plan-file PLAN.json --dry-run`
- `shiki smoke live --plan-file PLAN.json --execute-github`
- `shiki smoke live --plan-file PLAN.json --execute-github --push-branch`
- `shiki goal create --title ... --outcome ...`
- `shiki issue plan --goal-id G-0001 --title ... --scope ... --acceptance-check ...`
- `shiki lock acquire T-0001`
- `shiki dispatch check T-0001`
- `shiki worktree allocate T-0001`
- `shiki github issue --task-id T-0001`
- `shiki github pr --task-id T-0001`
- `shiki handoff task T-0001`
- `shiki handoff repair RP-0001`
- `shiki repair packet --task-id T-0001 --pr 123 --minimal-change ... --verification-command ...`
- `shiki task status T-0001 --status done`
- `shiki goal complete G-0001`
- `shiki status`
