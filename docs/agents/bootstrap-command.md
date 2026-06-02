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

Check which adapters are currently usable:

```bash
shiki doctor
```

`shiki doctor` separates Shiki CLI availability from runtime authentication. If
Claude Code shows `Please run /login` or `API Error: 401 Invalid authentication
credentials`, log Claude Code in with `claude auth login` or `/login`; Codex and
terminal entrypoints can still use `shiki start` when their own auth is ready.

## Start A Target Repository

```bash
shiki start /path/to/target-repo --repo OWNER/REPO --private
```

This is the standard user-facing Shiki entrypoint. It will:

- install Shiki template files;
- initialize Git if needed;
- create the GitHub repository if missing;
- add `origin` if missing, or fail if an existing `origin` points elsewhere unless `--adopt-existing-repo` is passed;
- write `.shiki/repo.json`;
- commit and push only the Shiki manifest files;
- require `CLAUDE_CODE_OAUTH_TOKEN` when secret setup is enabled;
- hard-fail if required branch protection cannot be configured while protection is enabled;
- collect or consume Goal answers;
- write a machine-readable plan;
- run Shiki orchestration;
- create the first task issue and handoff evidence.

`shiki init` is still available as a lower-level command, but `/shiki` should
prefer `shiki start` unless the user explicitly asks for advanced control.

`shiki start` may run interactively. When values are missing, it asks one
question at a time for the GitHub repo slug, project name, Goal, outcome,
completion conditions, non-goals, and first vertical slice. The selected
engineering Skill Gate directory is recorded in `.shiki/starts/`, the plan, and
handoff evidence. By default, Shiki uses
`/Users/kio.mizutani/Documents/lead-os/skills/engineering` when present.

Do not use `install-target` for normal setup. Shiki is GitHub-first.

### Claude Code Action Secret

`shiki start`, `shiki init`, and `shiki bootstrap-platform` automatically set
the GitHub secret `CLAUDE_CODE_OAUTH_TOKEN` from the environment variable
`CLAUDE_CODE_OAUTH_TOKEN`. Claude Code login by itself does not expose a GitHub
Actions token to child processes.

If the secret is missing, create a long-lived Claude Code token:

```bash
claude setup-token
```

Then export the token as `CLAUDE_CODE_OAUTH_TOKEN` before running Shiki, or set
it directly and rerun Shiki with `--no-set-secret` only when that exception is
intentional:

```bash
gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo OWNER/REPO
```

Shiki must never read local Claude OAuth credential files or print token values.

## Publish This Shiki Platform Repo

```bash
CLAUDE_CODE_OAUTH_TOKEN=... shiki bootstrap-platform --repo OWNER/shiki --private
```

The command is idempotent. It will:

- validate `.shiki/`;
- initialize Git if needed;
- create the GitHub repo if missing;
- add `origin` if missing, or fail if an existing `origin` points elsewhere unless `--adopt-existing-repo` is passed;
- commit and push only the Shiki manifest files;
- require `CLAUDE_CODE_OAUTH_TOKEN` when secret setup is enabled;
- hard-fail if required branch protection cannot be configured while protection is enabled;
- save defaults in `~/.shiki/config.json`.

After defaults are saved, rerun:

```bash
shiki bootstrap-platform
```

## Local-Only Template Copy

```bash
shiki install-target /path/to/target-repo --local-only
```

Use this only for tests, fixtures, or explicit local-only template inspection.
Use `--force` only when you intentionally want to overwrite existing target files.

## Slash Command

After `shiki install-global`, Claude Code can invoke:

```text
/shiki <goal or task>
```

Codex CLI does not currently expose installed skills as custom slash commands,
so `/shiki` is expected to be unrecognized there. In Codex, invoke Shiki with
natural language, for example:

```text
Shiki: create a new GitHub-backed target repository for ...
```

Codex can also call the CLI directly:

```bash
shiki status
shiki doctor
shiki start /path/to/target-repo --repo OWNER/REPO
```

After `shiki start` creates a ready Codex task, the coordinator should continue
autonomously:

```bash
shiki runner codex --target /path/to/target-repo --task-id T-0001
```

Do not present this as a manual next step for the user during the normal Shiki
flow. Run it from the coordinator/runtime that is driving Shiki. Stop for user
input only when Codex auth/tooling is unavailable, dispatch is blocked, or
Guardian approval is required.

## Control Plane Commands

After `shiki init` has connected the target repo to GitHub, use the control
commands for durable execution state:

```bash
shiki plan guide --prompt "..."
shiki plan ingest --plan-file PLAN.json
shiki run --plan P-0001
shiki daemon enqueue-plan --plan-file PLAN.json
shiki daemon run --once
shiki runner next
shiki runner execute --task-id T-0001 --command "..."
shiki runner codex --task-id T-0001
shiki smoke live --plan-file PLAN.json --dry-run
shiki smoke live --plan-file PLAN.json --execute-github
shiki smoke live --plan-file PLAN.json --execute-github --push-branch
shiki goal create --title "..." --outcome "..."
shiki issue plan --goal-id G-0001 --title "..." --scope "..." --acceptance-check "..."
shiki lock acquire T-0001
shiki dispatch check T-0001
shiki worktree allocate T-0001
shiki github issue --task-id T-0001
shiki github pr --task-id T-0001
shiki repair packet --task-id T-0001 --pr 123 --minimal-change "..." --verification-command "..."
shiki handoff task T-0001
shiki handoff repair RP-0001
shiki task status T-0001 --status done
shiki goal complete G-0001
```

See `docs/agents/control-commands.md` for the full sequence.

## Required GitHub Checks

The bootstrap command attempts to require:

- `Validate Shiki mirror`
- `CCA verdict`
- `MergeGate metadata check`
- `MergeGate policy check`

When `.shiki/config.yaml` sets `defaults.required_review: true`, the bootstrap command configures branch protection with at least one required approving PR review. Solo/self-running operation relies on the CCA Review Bridge to create that GitHub review only after CCA completes and Guardian evidence is present when required.

If the GitHub API rejects branch protection because of plan or permission limits, configure these checks manually in branch protection or rulesets.
