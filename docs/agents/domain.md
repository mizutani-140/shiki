# Domain Docs

Shiki is a single-context platform repository unless `CONTEXT-MAP.md` declares a multi-context layout.

## Layout

- `CONTEXT.md` defines Shiki language. Keep it implementation-free.
- `docs/adr/` records hard-to-reverse decisions.
- `AGENTS.md` is the runtime-neutral agent constitution.
- `CLAUDE.md` is the Claude Code wrapper around `AGENTS.md`.
- `.shiki/` is the Target Repository mirror for Goals, PRDs, plans, Task DAGs, contracts, locks, worktree records, ledger entries, CCA verdicts, repair packets, reports, and handoffs.
- `docs/agents/` tells skills how to consume issue tracker, labels, domain docs, and Skill Gate rules.

## Consumer Rules

- Use Shiki terms exactly when they exist.
- Add glossary terms to `CONTEXT.md` only when the term is part of the domain, not a generic programming concept.
- Create ADRs only for hard-to-reverse, surprising, tradeoff-based decisions.
- Keep product-specific language in the Target Repository, not in Shiki Template docs.
- If an ADR conflicts with a proposed implementation, surface the conflict before editing.
