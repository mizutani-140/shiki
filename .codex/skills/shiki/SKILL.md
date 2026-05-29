---
name: shiki
description: Use when the user invokes Shiki, /shiki, or asks to run the GitHub-first agentic engineering control plane for Goal Seek, Context and Impact, Task DAG, Codex implementation, CCA completion judgment, MergeGate, or bounded repair loops.
---

# Shiki

Shiki is the user's GitHub-first, runtime-agnostic control plane for agentic
engineering.

## Start

Run:

```bash
shiki status
```

Then inspect the target repository's `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`,
`.shiki/`, `docs/agents/`, and open PR/issue state before changing files.

If Shiki is not installed in the target repository, do not give the user a
manual sequence. Ask for the missing repo and Goal values one question at a
time, then run the one-command entrypoint:

```bash
shiki start . --repo OWNER/NAME --goal "..." --outcome "..."
```

The default engineering Skill Gate directory is
`/Users/kio.mizutani/Documents/lead-os/skills/engineering` when present. The
start record, plan, and handoff must preserve the selected skills directory.

## Responsibilities

- Codex implements and repairs.
- Claude Code Action can act as GitHub-side CCA or reviewer.
- CCA judges completion.
- MergeGate authorizes state transitions and merge readiness.
- GitHub branch protection is the hard gate.

## Rules

- For non-trivial goals, enter through `grill-with-docs`.
- `/shiki` should guide the user through missing repo/Goal answers one question at a time and then run `shiki start`; direct `init`, `plan`, and `run` calls are lower-level fallback commands.
- Convert the settled `grill-with-docs` result into a machine-readable plan and run it with `shiki plan ingest` followed by `shiki run`.
- For unattended execution, queue the plan with `shiki daemon enqueue-plan` and process it with `shiki daemon run`.
- For headless runtime integration, use `shiki runner next` and `shiki runner execute` to pick up ready tasks and record execution evidence.
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
