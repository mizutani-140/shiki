# Shiki Operations Guide

This guide covers the operational lifecycle of a Shiki installation: **install**,
**upgrade**, **rollback**, and **migration**. It complements
[`docs/usage.md`](usage.md) (which command to use when) and
[`docs/agents/bootstrap-command.md`](agents/bootstrap-command.md) (the full
bootstrap contract).

Shiki is GitHub-first. GitHub Issues/PRs/Checks are the operational source of
truth; the repository-local `.shiki/` mirror is the durable recovery and audit
record.

## Install

Installing Shiki has two layers: the **CLI** (so you can run `shiki ...`) and a
**target installation** (connecting a repository to GitHub with the Shiki
template and `.shiki/` mirror).

### 1. Install the CLI

```bash
bin/shiki install-global
shiki doctor
```

`install-global` creates/updates `~/.local/bin/shiki`, the Claude Code command,
and the Codex skill. Ensure `~/.local/bin` is on `PATH`. `shiki doctor` reports
CLI availability and runtime authentication separately; resolve any login/401
errors before continuing.

### 2. Initialize a Target Repository

Use `shiki start` for the standard, user-facing setup (or `shiki init` for
lower-level control). Both default to a **dry-run** so you can review intended
mutations first.

```bash
# Preview (dry-run):
shiki start /path/to/target-repo --repo OWNER/REPO --private

# Apply, with the Claude Code Action secret available:
CLAUDE_CODE_OAUTH_TOKEN=... shiki start /path/to/target-repo --repo OWNER/REPO --private --execute
```

In execute mode Shiki installs template files, initializes Git if needed, creates
the GitHub repo if missing, writes `.shiki/repo.json`, commits and pushes the
manifest, sets the `CLAUDE_CODE_OAUTH_TOKEN` secret, configures branch
protection, and creates the first task issue and handoff evidence. Missing
required secret input and branch-protection failures are hard failures unless the
corresponding `--no-set-secret` / `--no-protect` exception is passed.

To publish **this Shiki platform repository** itself, use
`shiki bootstrap-platform` (see [`docs/usage.md`](usage.md)).

### Verify the install

```bash
shiki status                       # local CLI configuration
shiki doctor                       # adapter and auth readiness
python3 scripts/validate_shiki.py  # validate the .shiki/ mirror
```

## Upgrade

Upgrading means bringing an existing installation up to the current Shiki
template, CLI, and mirror schema.

1. **Update the CLI.** Pull the latest Shiki platform repo and rerun
   `bin/shiki install-global` so `~/.local/bin/shiki`, the Claude command, and
   the Codex skill point at the new version. Restart Codex or Claude Code if the
   client does not reload commands dynamically.
2. **Refresh target template files with `--force`.** Re-running the bootstrap
   without `--force` is **not** an upgrade: an existing file is kept with a
   `kept existing file:` warning and the run still exits 0, so a stale target
   looks refreshed when nothing changed. Pass `--force` to actually refresh the
   template. Re-run in dry-run first to preview, then `--force --execute` to
   apply. Under `--force` the shipped surface splits three ways so nothing is
   lost and nothing is silently stale:
   - **Project content** — `CONTEXT.md`, `AGENTS.md`, `CLAUDE.md`,
     `.github/CODEOWNERS` — is never overwritten. The incoming template is
     written alongside as `<file>.new` (see [The `.new`
     convention](#the-new-convention) below) so you can merge deliberately.
   - **Governance contract** — `.shiki/config.yaml`,
     `.shiki/guardian-policy.json` — is also never silently kept: `<file>.new`
     is written alongside and the run reports which keys differ, naming
     `mergegate.required_checks` (what branch protection requires) and
     `approval_sources` (what may approve) explicitly.
   - **`.shiki/migrations/state.json`** is preserved outright (no `.new`); it is
     target history, brought forward by `shiki migrate`, not a template.
   - **Everything else** (CLI scripts, workflows, schemas, docs) is overwritten.

   A forced upgrade also migrates the former numeric Shiki decision paths to
   `SADR-NNNN-*`. Before the first write, the installer inventories the exact
   legacy Shiki paths and authorizes each deletion only when the existing
   `.shiki/install-stamp.json` names that path and its SHA-256 digest still
   matches the file. An absent, unreadable, malformed, incomplete, or
   mismatching stamp reports every blocked path and stops with no filesystem
   change. Target-owned `NNNN-*` ADRs are not cleanup candidates. A non-force
   install deletes nothing and retains any still-matching legacy stamp entries
   so a later forced upgrade can prove ownership.

   If the target has pending migrations, `--force` refuses and writes nothing —
   apply migrations first (step 3), then re-run. The run ends with a single
   summary of every `.new` written, so a half-upgrade is visible.
3. **Apply mirror migrations.** Run `shiki migrate` (see
   [Migration](#migration) below) to bring the `.shiki/` mirror schema up to
   date.
4. **Verify.** Run the verification baseline (validate, py_compile, script
   tests) and `shiki doctor` after upgrading.

Because Shiki defaults to dry-run, always review the previewed mutations before
applying an upgrade.

### The `.new` convention

Under `--force`, a file that must not be overwritten (project content and the
governance contract) is kept exactly as the target has it, and the incoming
template is written next to it with a `.new` suffix — for example
`.shiki/config.yaml.new` beside `.shiki/config.yaml`. Nothing merges the two for
you: review each `.new`, fold in the changes you want, then delete the `.new`
file. The end-of-run summary lists every `.new` written so none is missed.

## Rollback

Shiki state lives in two durable places, so rollback is a deliberate, evidenced
operation — not a destructive command.

- **GitHub operational state** (Issues, PRs, Checks, Reviews, merges) is the
  source of truth. Roll back code changes with normal, Guardian-authorized Git
  practices: revert the merge/PR rather than rewriting history. Do not
  force-push, rewrite history, or delete work without Guardian authorization.
- **`.shiki/` mirror state** is versioned in Git. To roll the mirror back, revert
  the commit(s) that changed it through a PR. Never edit existing ledger entries
  to "undo" them — append new ledger entries that record the correction, so the
  audit trail stays intact.
- **Migrations.** Migration application is dry-run by default. Prefer reverting
  the commit that introduced a bad migration result over hand-editing mirror
  files. Destructive migrations require the explicit `--i-understand` flag and
  Guardian judgment.
- **Secrets/branch protection.** Changes to secrets, branch protection, the
  default branch, or other Guardian-owned settings are rolled back only with
  Guardian authorization.

If GitHub and `.shiki/` disagree after a partial rollback, prefer GitHub and
repair the mirror.

## Migration

The `.shiki/` mirror has a versioned schema managed by `shiki migrate`. Migration
state is recorded in `.shiki/migrations/state.json`. See
[`docs/agents/shiki-migrations.md`](agents/shiki-migrations.md) for the migration
registry contract.

Inspect and preview before applying — apply defaults to dry-run:

```bash
shiki migrate status   # show applied/pending migrations and registry status
shiki migrate plan     # preview pending migrations without mutation
shiki migrate apply    # dry-run by default
```

Apply for real, and scope to a specific migration when needed:

```bash
shiki migrate apply --execute                      # apply non-destructive migrations
shiki migrate apply --migration <id> --execute     # apply one migration and its deps
```

Destructive migrations require `--i-understand` in addition to `--execute`, and
should be treated as a Guardian-approved change. Use `--target /path/to/repo` to
operate on a target other than the current directory, and `--json` for
machine-readable output.

After migrating, re-run the verification baseline and `shiki migrate status` to
confirm `pending: 0`.

## Verification Baseline

Run these from the repository root for any install, upgrade, rollback, or
migration, and capture exit codes as durable evidence:

```bash
python3 scripts/validate_shiki.py
python3 -m py_compile scripts/*.py
for sh in scripts/test_shiki_*.sh; do bash "$sh"; done
```
