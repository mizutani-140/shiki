# Security Policy

Shiki is an execution governance layer for AI engineering work. Because it
coordinates source changes, GitHub state, and runtime credentials, security
issues are treated as high-priority Guardian matters.

## Reporting A Vulnerability

Please report security vulnerabilities **privately**. Do not open a public
GitHub issue or pull request for an unpatched vulnerability.

- Preferred: use GitHub's **private vulnerability reporting** (Security tab ->
  "Report a vulnerability") on this repository.
- Alternatively: contact the repository Guardian (`mizutani-140`) directly
  through a private channel.

When reporting, include:

- a description of the vulnerability and its impact;
- the affected component (CLI, workflow, schema, mirror state, etc.);
- reproduction steps or a proof of concept;
- any suggested remediation.

You can expect an initial acknowledgement and a triage decision. Coordinated
disclosure is preferred: please allow time for a fix and Guardian-approved
release before any public disclosure.

## No-Secrets Policy

Shiki must never print, copy, commit, or expose secrets. This is a hard rule for
every runtime and contributor:

- Never commit or print tokens, OAuth files, local auth stores, API keys,
  private credentials, signing material, or `.env` contents.
- Never expose `CLAUDE_CODE_OAUTH_TOKEN`, GitHub tokens, or local Codex OAuth
  material in code, logs, ledger entries, PRs, or comments.
- Shiki must not read local Claude or Codex OAuth credential files, and must not
  print token values.
- GitHub Actions runs must request only the permissions necessary for comments
  and checks. Treat CI output as evidence, but never embed secrets in it.

Secrets such as `CLAUDE_CODE_OAUTH_TOKEN` are set through GitHub repository
secrets (for example via `gh secret set`) and consumed at runtime only. They are
never stored in the repository or the `.shiki/` mirror.

If you discover a leaked secret, treat it as a security incident: report it
privately, and assume the credential is compromised and must be rotated.

## Guardian Role

A **Guardian** is the human or explicitly authorized governance role for
high-risk decisions and exceptions, including secrets, production writes, policy,
budget, security, identity, branch protection, and merge exceptions.

- High-risk and critical changes require Guardian approval before merge.
- Destructive Git operations, history rewrites, force-pushes, auto-merge of
  high-risk work, paid external actions, and production writes require explicit
  Guardian authorization.
- Automation (Codex, CCA, reviewers, CI) must not self-approve these paths.

See `AGENTS.md` ("Safety", "Architecture Gate") and `docs/agents/` for the full
governance contract.
