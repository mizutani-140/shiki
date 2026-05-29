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
```

If the current repository does not have Shiki installed, do not hand the user a
manual checklist. Ask only for the missing values, one question at a time, then
run `shiki start`.

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
command:

```bash
shiki start . --answers-file ANSWERS.json
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
