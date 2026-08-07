# Shiki Usage Guide

This guide explains **which Shiki command to use when**. All commands are
exposed through the `shiki` CLI (`bin/shiki`, backed by `scripts/shiki.py`).
For the full bootstrap contract and control-plane command sequence, see
[`docs/agents/bootstrap-command.md`](agents/bootstrap-command.md).

## At A Glance

| Command | Use when | Mutating? |
| --- | --- | --- |
| `shiki start` | Standard, user-facing setup of a GitHub-backed Target Repository and first run. | Dry-run by default; `--execute` applies mutations. |
| `shiki init` | Lower-level install + GitHub publish of a target repo when you need advanced control over individual steps. | Dry-run by default; `--execute` applies mutations. |
| `shiki install-target` | Local-only template copy for tests, fixtures, or inspection. No GitHub. | Writes local files only. |
| `shiki bootstrap-platform` | Initialize and publish **this Shiki platform repo** itself to GitHub. | Dry-run by default; `--execute` applies mutations. |
| `/shiki` | Invoke Shiki from inside Claude Code as a slash command after `install-global`. | Delegates to the CLI. |

## `shiki start` — the default entrypoint

Use `shiki start` for normal setup. It is the standard user-facing way to connect
a Target Repository to GitHub and kick off the first Goal.

```bash
shiki start /path/to/target-repo --repo OWNER/REPO --private
```

- Defaults to a **dry-run** for uninitialized targets: it prints the intended
  bootstrap/init mutations and does not create a GitHub repo, mutate `origin`,
  commit, push, set secrets, change the default branch, or configure branch
  protection.
- Pass `--execute` (or `--i-understand`) to apply mutations. In execute mode it
  installs template files, initializes Git if needed, creates the GitHub repo if
  missing, writes `.shiki/repo.json`, commits and pushes manifest files, sets the
  `CLAUDE_CODE_OAUTH_TOKEN` secret (unless `--no-set-secret`), configures branch
  protection (unless `--no-protect`), collects Goal answers, writes a plan, runs
  orchestration, and creates the first task issue and handoff evidence.
- May run interactively, asking one question at a time for the repo slug, project
  name, Goal, outcome, completion conditions, non-goals, and first vertical
  slice.

Prefer `shiki start` over `shiki init` unless you explicitly need step-level
control.

## `shiki init` — lower-level GitHub setup

Use `shiki init` when you want the same install-and-publish behavior as
`shiki start` but with finer control over individual flags, and without the
interactive Goal flow.

```bash
shiki init /path/to/target-repo --repo OWNER/REPO
```

- Also dry-run by default; gated by `--execute`.
- Supports remote/provider controls such as `--remote-protocol {https,ssh}`,
  `--github-host`, and `--github-api-url` for GitHub Enterprise-compatible hosts.
- Shares the bounded flags with `shiki start`: `--adopt-existing-repo`,
  `--no-set-secret`, `--no-protect`, `--no-commit`, `--no-push`.

## `shiki install-target` — local-only template copy

Use `shiki install-target` only for tests, fixtures, or explicit local-only
template inspection. It is **not** for normal setup, because Shiki is
GitHub-first.

```bash
shiki install-target /path/to/target-repo --local-only
```

Use `--force` only when you intentionally want to refresh the target template.
For the SADR namespace migration, `--force` deletes only exact legacy Shiki
decision paths whose existing install-stamp digest still matches; any ownership
blocker stops before the first write. Numeric target ADRs are never candidates.

## `shiki bootstrap-platform` — publish the Shiki platform repo

Use `shiki bootstrap-platform` to initialize and publish **this Shiki platform
repository** itself (as opposed to a downstream Target Repository).

```bash
CLAUDE_CODE_OAUTH_TOKEN=... shiki bootstrap-platform --repo OWNER/shiki --private
```

- Idempotent and dry-run by default; pass `--execute` to apply.
- In execute mode it validates `.shiki/`, initializes Git if needed, creates the
  repo if missing, commits and pushes manifest files, requires the
  `CLAUDE_CODE_OAUTH_TOKEN` secret (unless skipped), configures branch protection
  (unless skipped), and saves defaults in `~/.shiki/config.json`.
- `shiki bootstrap-github` is a deprecated alias for this command.

## `/shiki` — the Claude Code slash command

After installing the CLI globally:

```bash
shiki install-global   # installs ~/.local/bin/shiki, the Claude command, and the Codex skill
```

Claude Code can then invoke Shiki as a slash command:

```text
/shiki <goal or task>
```

Codex CLI does not currently expose installed skills as slash commands, so
`/shiki` is expected to be unrecognized there. In Codex, invoke Shiki with
natural language or call the CLI directly (`shiki status`, `shiki doctor`,
`shiki start ...`).

## Diagnostics And State

- `shiki doctor` — check Shiki CLI availability and runtime authentication
  separately. If Claude Code reports a login/401 error, log in with
  `claude auth login` or `/login`.
- `shiki status` — show local Shiki CLI configuration.

## Control-Plane Commands

Once a target repo is connected, durable execution uses control commands such as
`shiki goal create`, `shiki issue plan`, `shiki lock acquire`,
`shiki dispatch check`, `shiki worktree allocate`, `shiki github pr`,
`shiki repair packet`, `shiki handoff task`, and `shiki task status`. See
[`docs/agents/control-commands.md`](agents/control-commands.md) for the full
sequence and [`docs/operations.md`](operations.md) for install/upgrade/rollback/
migration flow.
