# Shiki

Shiki is a GitHub-first agentic engineering control plane with a repository-local
`.shiki/` mirror for recovery, audit, and portable agent context.

Shiki turns a user-approved **Goal** into planned, dependency-aware,
TDD-implemented, GitHub-judged, reviewable, repairable, and mergeable work while
preserving durable evidence at every step.

> Core maxim: **LLM outputs may vary. State transitions must not vary.**

## What Shiki Is

Shiki is an execution governance layer for AI engineering work. It coordinates
Goal Seek, grilling, Context & Impact analysis, PRD creation, vertical-slice
issue decomposition, Task DAG planning, MergeGate authorization, runtime
assignment, scoped branch execution, TDD implementation, GitHub Completion Check
Agent (CCA) judgment, validation, repair loops, and Branch / PR / Merge
evidence.

Shiki is runtime-agnostic. The default runtime split is subscription
authenticated:

- **Codex Front** is the operator-facing implementation surface (Codex App, CLI,
  IDE extension, or Web) signed in with ChatGPT OAuth.
- **GitHub CCA** is the completion judge, implemented by Claude Code Action using
  `CLAUDE_CODE_OAUTH_TOKEN`.
- **Claude Code** is the default planner, reviewer, coordinator, and CCA
  implementation.
- **GitHub Actions / CI** produces durable verification evidence.

Shiki is *not* a single-agent coding prompt, a Claude-only or Codex-only
workflow, a hidden chat-state orchestrator, simple CI status, an unordered
checklist, or a system that treats implementation as completion.

## The Operating Loop

Every non-trivial change follows this loop:

1. **Goal** — clarify outcome, completion conditions, non-goals, risk, signals.
2. **grill-with-docs** — challenge the plan against domain docs, ADRs, and code.
3. **Context & Impact** — find relevant docs, code, dependencies, locks, and
   verification surfaces.
4. **PRD** — capture settled product/engineering intent.
5. **Issues** — create independently grabbable vertical-slice issues.
6. **Triage** — label readiness, risk, runtime, skills, and MergeGate state.
7. **Plan** — decompose into a Task DAG with explicit dependencies and locks.
8. **Preflight** — confirm tools, repo state, auth, and verification commands.
9. **Execute** — the assigned implementer (Claude Code by default, Codex when assigned) implements on an isolated branch/worktree using TDD.
10. **Verify** — run required checks and record durable evidence.
11. **CCA Judgment** — GitHub CCA emits a structured completion verdict.
12. **Review** — record findings as PR comments, check output, or ledger entries.
13. **MergeGate** — merge only when every gate is satisfied.
14. **Completion Judgment** — confirm the task and parent Goal are truly done.
15. **Repair Loop** — failed checks become bounded repair work, not rewrites.

## Source Of Truth

1. GitHub Issues, Pull Requests, Checks, Reviews, and merge evidence are the
   operational source of truth.
2. The repository-local `.shiki/` mirror records Goals, PRDs, plans, Task DAGs,
   contracts, locks, ledger entries, CCA verdicts, repair packets, and handoffs.
3. `CONTEXT.md` defines Shiki domain language.
4. `docs/adr/` records hard-to-reverse platform decisions.

If GitHub and `.shiki/` disagree, prefer GitHub and repair the mirror.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `AGENTS.md` | The runtime-neutral Shiki agent constitution. |
| `CLAUDE.md` | Claude Code-specific wrapper over the constitution. |
| `CONTEXT.md` | Domain glossary and language authority. |
| `bin/shiki` | Operational entrypoint / bootstrap command. |
| `scripts/` | Shiki CLI (`shiki.py`), state helpers, validators, and tests. |
| `.shiki/` | Repository-local mirror: goals, tasks, DAGs, locks, ledger, schemas, policy. |
| `docs/adr/` | Architecture Decision Records. |
| `docs/agents/` | Operational contracts (checklists, decision control, skill gate). |
| `.github/` | Workflows, PR template, and CODEOWNERS. |
| `prompts/`, `skills/` | Runtime prompts and engineering skill registry. |

## Quick Start

Install the CLI once and check adapter readiness:

```bash
bin/shiki install-global
shiki doctor
```

Start a GitHub-backed Target Repository (dry-run by default):

```bash
shiki start /path/to/target-repo --repo OWNER/REPO --private
```

Pass `--execute` to apply bootstrap/init mutations once you have reviewed the
dry-run plan. See:

- **[docs/usage.md](docs/usage.md)** — when to use `shiki start` vs `shiki init`
  vs `install-target` vs `bootstrap-platform` vs `/shiki`.
- **[docs/operations.md](docs/operations.md)** — install, upgrade, rollback, and
  migration flow.
- **[docs/agents/bootstrap-command.md](docs/agents/bootstrap-command.md)** — the
  full bootstrap contract and control-plane commands.

## Contributing & Security

- See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the Goal -> grill -> PRD ->
  issues -> task -> PR -> CCA -> MergeGate contribution flow.
- See **[SECURITY.md](SECURITY.md)** to report vulnerabilities and for the
  no-secrets policy.

## License

Shiki is released under the MIT License. See [LICENSE](LICENSE).
