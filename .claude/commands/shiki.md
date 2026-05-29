---
description: Run the Shiki GitHub-first agentic engineering control plane.
argument-hint: "[goal, task, repo path, or Shiki CLI subcommand]"
allowed-tools: Bash(shiki:*), Bash(git status:*), Bash(git branch:*), Bash(git diff:*), Bash(gh pr view:*), Bash(gh pr checks:*), Read, Glob, Grep
---

# Shiki

Use Shiki as the GitHub-first control plane for Goal Seek, Context and Impact,
Task DAG, Codex implementation handoff, CCA completion judgment, MergeGate, and
bounded repair loops.

## First Action

Run:

```bash
shiki status
shiki doctor
```

Important: `shiki status` reports the installed Shiki platform/template root and
default config. Its `config.repo` value, such as `mizutani-140/shiki`, is not the
operator's requested Target Repository. Do not select that repo as the Goal
target unless the user explicitly says they want to work on the Shiki platform
repo itself.

If `shiki doctor` reports that Claude Code is not authenticated, tell the user
that Claude Code must be logged in before `/shiki` can run inside Claude Code.
The usual fixes are `claude auth login` in a terminal or `/login` inside Claude
Code. Do not treat this as a Shiki CLI failure: Claude Code slash commands need
Claude authentication before Shiki receives control. If Codex is authenticated,
the same Shiki flow can still start from Codex or a terminal with
`shiki start`.

If `$ARGUMENTS` is empty and the current directory is not already a Shiki Target
Repository, ask first whether the user wants to create a new GitHub-backed
Target Repository or work inside an existing repository. Prefer the new target
repository path when the user says "new repo", "create repo", "new project", or
similar. Do not inspect or plan work in `/Users/kio.mizutani/shiki` just because
`shiki status` points there.

If the selected target repository does not have Shiki installed, do not hand the
user a manual checklist. Ask only for the missing values, one question at a
time, then run `shiki start TARGET --answers-file ANSWERS.json`.

Required start questions:

1. GitHub repo slug: `OWNER/REPO`
2. Project name
3. Goal title
4. Outcome / completion result
5. Completion conditions
6. Non-goals
7. First vertical-slice task and acceptance checks

Ask these in the `grill-with-docs` style: one question at a time, with a
recommended answer when enough context exists. Explore the repository instead
of asking when the answer is discoverable locally.

Once enough answers are known, create a temporary answers JSON and run one
command against the selected target path:

```bash
shiki start TARGET --answers-file ANSWERS.json
```

Use `shiki init`, `shiki plan ingest`, or `shiki run` directly only for repair,
debugging, or explicit advanced control. The normal user-facing entrypoint is
`shiki start`.

The default engineering Skill Gate directory is
`/Users/kio.mizutani/Documents/lead-os/skills/engineering` when present. Preserve
the selected skills directory in the start record, plan, and handoff evidence.

## Operating Rules

- Treat Codex as implementer, CCA as completion judge, and MergeGate as merge authorization.
- Treat `/shiki` as a guided one-command entrypoint. Do not ask the user to run multiple setup commands.
- For non-trivial goals, use `grill-with-docs`, then Context and Impact, then PRD/issues/triage.
- After `grill-with-docs` is settled, prefer `shiki plan ingest` and `shiki run` over manually calling each lower-level command.
- For unattended execution, queue settled plans with `shiki daemon enqueue-plan` and process them with `shiki daemon run`.
- For headless runner integration, use `shiki runner next` and `shiki runner execute` so execution evidence lands in `.shiki/runner` and the Ledger.
- When a ready task is assigned to Codex, do not stop after showing the user a `codex` command. Run the implementation adapter yourself with `shiki runner codex --target TARGET --task-id T-XXXX`. This materializes the task worktree, feeds the handoff to `codex exec`, and records runner evidence. Ask the user only when Codex auth/tooling is missing, the task is not dispatchable, or Guardian approval is required.
- Register durable state through Shiki commands: `goal create`, `issue plan`, `lock acquire`, `dispatch check`, `worktree allocate`, `repair packet`, `task status`, and `goal complete`.
- Use `shiki github issue`, `shiki github pr`, and `shiki handoff` to create durable GitHub and Codex evidence instead of free-form handoff text.
- Do not claim completion from local work alone. Completion requires PR evidence, CCA, and MergeGate.
- Do not use `shiki install-target` unless the user explicitly asks for a local-only template copy.
- Do not bypass branch protection. Do not use admin merge.
- For workflow changes that cannot pass CCA until merged, require explicit Guardian approval before any temporary protection exception.

## User Input

Use the command arguments as the goal or task prompt:

```text
$ARGUMENTS
```
