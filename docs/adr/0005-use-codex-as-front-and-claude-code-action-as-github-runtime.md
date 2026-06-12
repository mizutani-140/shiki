# ADR 0005: Use Codex As Front And Claude Code Action As The GitHub Runtime

## Status

Accepted (implementer default superseded by ADR 0008; the auth model and the
Claude Code Action GitHub runtime decision remain in force)

## Context

Shiki should run inside the operator's subscription-authenticated toolchain. The intended default is not to run Codex as an API-key backed GitHub Action.

The operator uses Codex as the front surface for implementation and long-running Goal work. Claude Code runs in GitHub Actions for review, automation, and MergeGate-oriented judgment.

## Decision

Use this default split:

- Codex Front: implementation, tests, local repair, and operator-facing Goal work through Codex App, Codex CLI, Codex IDE extension, or Codex Web signed in with ChatGPT OAuth/subscription auth.
- Claude Code Action: GitHub PR review and automation using `CLAUDE_CODE_OAUTH_TOKEN`.
- GitHub Actions: validation evidence and Claude Code Action execution.

The Shiki template must not include `openai/codex-action` as the default implementation path.

## Consequences

- Shiki avoids requiring `OPENAI_API_KEY` for the default workflow.
- Codex work happens through the user's authenticated Codex front surface, not as a hidden backend Action.
- Claude Code Action is configured for OAuth-token based subscription usage by default.
- API-key based automation remains possible only as an explicit target-repo extension with its own ADR.
