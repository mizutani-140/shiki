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

If Shiki is not installed in the target repository, do not use a local-only
template install by default. Ask for the GitHub repository slug if it is
missing, then run:

```bash
shiki init . --repo OWNER/NAME
```

## Responsibilities

- Codex implements and repairs.
- Claude Code Action can act as GitHub-side CCA or reviewer.
- CCA judges completion.
- MergeGate authorizes state transitions and merge readiness.
- GitHub branch protection is the hard gate.

## Rules

- For non-trivial goals, enter through `grill-with-docs`.
- Use Context and Impact before implementation.
- Keep tasks as vertical slices with explicit locks and verification.
- Use TDD for implementation work when behavior changes.
- Do not call implementation complete until GitHub evidence, CCA, and MergeGate support it.
- Do not use `shiki install-target` unless the user explicitly asks for local-only template copying.
- Do not bypass branch protection or use admin merge.
- If a workflow change needs a bootstrap exception, ask for explicit Guardian approval first.

## Commands

- `shiki install-global`
- `shiki init /path/to/repo --repo OWNER/REPO`
- `shiki preflight --require-github`
- `shiki goal create --title ... --outcome ...`
- `shiki issue plan --goal-id G-0001 --title ... --scope ... --acceptance-check ...`
- `shiki lock acquire T-0001`
- `shiki dispatch check T-0001`
- `shiki worktree allocate T-0001`
- `shiki repair packet --task-id T-0001 --pr 123 --minimal-change ... --verification-command ...`
- `shiki task status T-0001 --status done`
- `shiki goal complete G-0001`
- `shiki status`
