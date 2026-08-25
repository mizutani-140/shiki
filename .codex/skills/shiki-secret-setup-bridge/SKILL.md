---
name: shiki-secret-setup-bridge
description: Sets up required Shiki environment variables and GitHub Secrets without exposing secret values. Use when Claude Code or Shiki asks for required env vars, required_secrets, GitHub Secrets, API keys, OAuth tokens, LINE Messaging API credentials, Anthropic keys, or setup completion before implementation can continue.
---

# Shiki Secret Setup Bridge

## Purpose
Use Codex as the bridge for Shiki secret setup. Codex discovers required secret names, opens official provider setup surfaces, helps the operator obtain values, writes them to GitHub Secrets without printing them, verifies names only, and tells Claude Code/Shiki that setup is complete.

This skill is separate from external AI Guardian review. Secret setup handles credential creation and transfer; Guardian review handles `external_ai_guardian_review` artifacts.

## Start

1. Identify the target repo and branch/issue/task context from GitHub, `.shiki/`, Claude transcript, or the user's message.
2. Extract required secret names from `required_secrets`, task contracts, workflow files, README/docs, or Claude output. Do not infer unknown secrets from vague errors.
3. Classify each secret by provider and destination: GitHub repo secret, org secret, environment secret, or local `.env`.
4. Build a setup checklist with secret names only. Never include secret values in chat, logs, files, shell history, PRs, issues, or commits.

## Source Priority

Use this order when deciding how to obtain a value:

1. Shiki durable state: GitHub issue/PR/checks/comments, `.shiki/`, task contracts, and workflow files.
2. Skill provider registry and known official docs/console URLs.
3. Current official provider docs or console pages opened in Chrome/Browser Use.
4. Web search only when the provider is unknown, the official URL has changed, or the runbook fails. Search official domains first.

## Safety Rules

- No iTerm automation. Use Codex terminal, GitHub connector, Chrome/Browser Use, Claude transcript files, or explicit user handoff.
- Do not echo, display, copy into chat, save, or commit secret values.
- Disable shell tracing before secret commands: `set +x`.
- Prefer clipboard/stdin transfer over command-line literals.
- Key/token creation, OAuth token creation, login, permission grant, and sensitive data transmission require action-time user confirmation.
- CAPTCHA, 2FA, billing, production permission changes, and unclear provider prompts are user handoff points.
- Never commit `.env`, token files, OAuth stores, screenshots of keys, or copied provider pages containing secrets.

## Provider Registry

- `ANTHROPIC_API_KEY`: Open the Anthropic Console/API key page or official Anthropic API key docs. The operator creates/copies the key. Register only as a secret.
- `CLAUDE_CODE_OAUTH_TOKEN`: Use `claude setup-token` from a trusted local terminal. The operator confirms token creation. Register the token as a GitHub Secret.
- `LINE_CHANNEL_ACCESS_TOKEN`: Open LINE Developers Console for the Messaging API channel. The operator creates/copies a long-lived channel access token.
- `LINE_USER_ID`: Open the LINE Developers Console Basic settings page or use a verified webhook/event source. Register the user ID as a secret when it is used for push delivery.
- `OPENAI_API_KEY`: Use the OpenAI Platform key setup flow/tooling when available. Register only as a secret; do not inline instructions when the platform key flow is available.
- `GITHUB_TOKEN`: Prefer the built-in GitHub Actions `GITHUB_TOKEN` when the workflow can use it. For custom PATs, confirm scope and necessity before creation.

## GitHub Secret Write Pattern

Use stdin/clipboard and verify names only:

```bash
set +x
pbpaste | gh secret set SECRET_NAME --repo OWNER/REPO
gh secret list --repo OWNER/REPO
```

For local files, use a user-approved destination such as `.env.local` only when the task explicitly requires local runtime secrets. Keep it untracked and verify `.gitignore` before writing.

## Verification

For GitHub Secrets:

1. Run `gh auth status` and confirm required scopes without printing token values.
2. Run `gh secret list --repo OWNER/REPO`.
3. Confirm every required secret name appears.
4. Do not claim the value is correct until a workflow or runtime check that consumes it passes.

For local env:

1. Verify the variable is present without printing it, e.g. `[ -n "${NAME:-}" ]`.
2. Run the smallest command that proves the runtime can authenticate.
3. Record only variable names and pass/fail status.

## Completion Handoff

Return a `secret_setup_report` with:

- repo and task/issue context;
- required secret names;
- destination for each secret;
- source docs or console URL used;
- verification command and result;
- unresolved/manual items;
- explicit statement that no secret values were exposed.

Then tell Claude Code/Shiki:

```text
Required secrets have been configured and verified by name only. Continue implementation. Do not request, print, or commit secret values.
```

If any required secret is missing, stop and report the missing name, provider, official setup surface, and next required user action.
