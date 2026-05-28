# .shiki Mirror

`.shiki/` is the repository-local mirror for Shiki state.

GitHub is the operational source of truth for Goals, Issues, Pull Requests, Checks, Reviews, and merge evidence. This directory mirrors the same state in portable files so an agent runtime can recover context without depending on chat history.

## Layout

- `config.yaml`: repository-level Shiki defaults.
- `goals/`: Goal records or goal notes.
- `plans/`: planning artifacts produced by Goal Seek and Context & Impact.
- `tasks/`: executable task contracts.
- `dag/`: task dependency graphs.
- `contracts/`: runtime handoffs and task-specific contracts.
- `locks/`: lock declarations and lock decisions.
- `ledger/`: durable evidence entries.
- `worktrees/`: registered local or self-hosted worktree records.
- `gha/`: generated GitHub Actions evidence. This path is ignored and should not be committed.
- `reports/`: completion, repair, and handoff reports.
- `state/`: derived state snapshots. Do not treat this as stronger than GitHub.
- `schemas/`: JSON Schemas for Shiki artifacts.
- `templates/`: starter templates for Shiki artifacts.

## Rule

If GitHub and `.shiki/` disagree, prefer GitHub operational state and create a repair task to reconcile the mirror.
