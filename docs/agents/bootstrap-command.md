# Shiki Bootstrap Command

`bin/shiki` is the single operational entrypoint for repeatable Shiki setup.

## Install Globally Once

```bash
/Users/kio.mizutani/shiki/bin/shiki install-global
```

This creates or updates:

- `~/.local/bin/shiki`
- `~/.claude/commands/shiki.md`
- `~/.codex/skills/shiki/SKILL.md`

Ensure `~/.local/bin` is on `PATH`. Restart Codex or Claude Code if the
running client does not reload commands dynamically.

## Publish This Shiki Repo

```bash
CLAUDE_CODE_OAUTH_TOKEN=... shiki bootstrap-github --repo OWNER/shiki --private
```

The command is idempotent. It will:

- validate `.shiki/`;
- initialize Git if needed;
- create the GitHub repo if missing;
- add `origin` if missing;
- commit and push the current Shiki template;
- set `CLAUDE_CODE_OAUTH_TOKEN` from the environment when present;
- configure branch protection with Shiki required checks when GitHub permissions allow it;
- save defaults in `~/.shiki/config.json`.

After defaults are saved, rerun:

```bash
shiki bootstrap-github
```

## Install Shiki Into A Target Repository

```bash
shiki install-target /path/to/target-repo
```

Use `--force` only when you intentionally want to overwrite existing target files.

## Slash Command

After `shiki install-global`, Claude Code can invoke:

```text
/shiki <goal or task>
```

Codex can use the global `shiki` skill in future sessions and can always call
the CLI directly:

```bash
shiki status
```

## Required GitHub Checks

The bootstrap command attempts to require:

- `Validate Shiki mirror`
- `CCA verdict`
- `MergeGate policy check`

If the GitHub API rejects branch protection because of plan or permission limits, configure these checks manually in branch protection or rulesets.
