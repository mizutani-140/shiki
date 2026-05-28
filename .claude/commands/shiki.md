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

If the current repository does not have Shiki installed and the user asked to
set up the repository, run:

```bash
shiki install-target .
```

## Operating Rules

- Treat Codex as implementer, CCA as completion judge, and MergeGate as merge authorization.
- For non-trivial goals, use `grill-with-docs`, then Context and Impact, then PRD/issues/triage.
- Do not claim completion from local work alone. Completion requires PR evidence, CCA, and MergeGate.
- Do not bypass branch protection. Do not use admin merge.
- For workflow changes that cannot pass CCA until merged, require explicit Guardian approval before any temporary protection exception.

## User Input

Use the command arguments as the goal or task prompt:

```text
$ARGUMENTS
```

