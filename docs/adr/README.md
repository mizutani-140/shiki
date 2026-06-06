# Architecture Decision Records

This directory holds Shiki's Architecture Decision Records (ADRs): durable
records of hard-to-reverse platform decisions. ADRs are part of the Shiki source
of truth alongside GitHub state, the `.shiki/` mirror, and `CONTEXT.md`.

## How ADRs Work

- New ADRs are added as `docs/adr/NNNN-<short-slug>.md`, where `NNNN` is the next
  zero-padded sequence number.
- The **canonical list of ADRs is the set of files matching `docs/adr/*.md`** in
  this directory. Browse `docs/adr/*.md` for the authoritative, current set.
- Use [`template.md`](template.md) as the starting point for a new ADR.
- Record an ADR when a decision is hard to reverse, surprising without context,
  and a real tradeoff. Do not write an ADR for routine or easily reversible
  choices.

Using the `docs/adr/*.md` glob as the source of truth means other PRs can add an
ADR without editing this index file, avoiding merge contention on a
hand-maintained enumeration.

## Current ADRs (snapshot)

The list below is a convenience snapshot and may lag behind the directory. When
in doubt, list `docs/adr/*.md`.

- [0001 — Keep Shiki As The Platform Name](0001-keep-shiki-as-platform-name.md)
- [0002 — Use AGENTS As The Shared Agent Constitution](0002-use-agents-as-shared-constitution.md)
- [0003 — Require Skill Gate For MergeGate Readiness](0003-require-skill-gate-for-mergegate-readiness.md)
- [0004 — Use GitHub First With A Repository-Local Shiki Mirror](0004-use-github-first-with-shiki-mirror.md)
- [0005 — Use Codex As Front And Claude Code Action As The GitHub Runtime](0005-use-codex-as-front-and-claude-code-action-as-github-runtime.md)
- [0006 — Separate Codex Implementation, CCA Completion, And MergeGate Authorization](0006-separate-codex-implementation-cca-completion-and-mergegate-authorization.md)

## Adding A New ADR

1. Copy `template.md` to `docs/adr/NNNN-<short-slug>.md`.
2. Fill in the title, status, context, decision, and consequences.
3. Set the status to `Proposed`, then `Accepted` once the decision is approved.
4. Open a PR following `CONTRIBUTING.md`. You do not need to edit this index for
   the ADR to be discoverable via `docs/adr/*.md`.
