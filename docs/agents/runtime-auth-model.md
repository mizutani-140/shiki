# Runtime Auth Model

Shiki's default operator model is subscription-authenticated, not API-key-first.

## Default Runtime Split

- **Codex Front**: the operator-facing implementation surface. Use Codex App, Codex CLI, Codex IDE extension, or Codex Web signed in with ChatGPT OAuth/subscription auth.
- **Claude Code Action**: the GitHub Actions runtime for PR review, issue/PR automation, and MergeGate judgment. Use `CLAUDE_CODE_OAUTH_TOKEN` as the default secret.
- **GitHub**: the durable coordination surface for Issues, PRs, Checks, Reviews, comments, and merge evidence.
- **`.shiki/` mirror**: portable recovery and evidence mirror inside each target repo.

## What This Means

Codex is not the default GitHub Actions backend in Shiki.

Do not assume `openai/codex-action` or `OPENAI_API_KEY` unless a target repository explicitly opts into an API-key based automation mode. The default Shiki loop is:

1. GitHub Issue or PR defines the Goal/task contract.
2. Codex Front reads Shiki context and performs implementation through the user's authenticated Codex session.
3. Codex pushes a branch or opens a PR.
4. Claude Code Action reviews the PR through GitHub Actions using the Claude Code OAuth token secret.
5. MergeGate uses checks, review, locks, skills, and ledger evidence to decide readiness.

## Required Secrets

Default Claude Code Action secret:

- `CLAUDE_CODE_OAUTH_TOKEN`

Do not store OAuth tokens in repository files, `.env`, logs, prompts, or `.shiki/` artifacts.

## Optional API-Key Mode

API-key based runners may be added by a target repository as an explicit extension. They are not the default Shiki template path.

If a repo opts into API-key mode, record the exception in an ADR and update the repo's `AGENTS.md`, `.shiki/config.yaml`, and workflow permissions.
