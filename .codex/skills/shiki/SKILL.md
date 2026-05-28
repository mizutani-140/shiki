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
- Do not bypass branch protection or use admin merge.
- If a workflow change needs a bootstrap exception, ask for explicit Guardian approval first.

## Commands

- `shiki install-global`
- `shiki install-target /path/to/repo`
- `shiki bootstrap-github --repo OWNER/REPO`
- `shiki status`

