# ADR 0004: Use GitHub First With A Repository-Local Shiki Mirror

## Status

Accepted

## Context

Shiki must coordinate multiple agent runtimes without relying on one hidden chat session or one local checkout. The operator wants Codex, Claude Code, Hermes Runner, GitHub Actions, and future runtimes to participate through a durable, auditable workflow.

## Decision

GitHub is the operational source of truth for Goals, Issues, Pull Requests, Checks, Reviews, comments, and merge evidence.

Each target repository also keeps a `.shiki/` mirror for Goals, plans, task DAGs, contracts, locks, ledger entries, reports, and handoffs.

## Consequences

- Runtimes can recover context from GitHub and `.shiki/` without relying on chat memory.
- Pull request boundaries become the preferred coordination surface between runtimes.
- `.shiki/` conflicts must be repaired against GitHub operational state.
- Shiki remains runtime-agnostic instead of becoming a Codex-only, Claude-only, or Hermes-only system.
