---
description: Run the Shiki GitHub-first agentic engineering control plane.
argument-hint: "[goal, task, repo path, or Shiki CLI subcommand]"
allowed-tools: Bash(shiki:*), Bash(git status:*), Bash(git branch:*), Bash(git diff:*), Bash(gh pr view:*), Bash(gh pr checks:*), Bash(gh issue view:*), Read, Glob, Grep, Skill, Workflow
---

# Shiki

Use Shiki as the GitHub-first control plane for Goal Seek, Context and Impact,
Task DAG, Codex implementation handoff, CCA completion judgment, MergeGate, and
bounded repair loops.

## Canonical Source Of Truth

<!-- shiki-source-of-truth:start -->
1. GitHub Issues, Pull Requests, Checks, Reviews, comments, and merge evidence are the operational source of truth.
2. The repository-local `.shiki/` mirror records Goals, PRDs, plans, Task DAGs, contracts, locks, ledger entries, CCA verdicts, repair packets, reports, and handoffs.
3. `CONTEXT.md` defines Shiki domain language and glossary decisions.
4. `docs/adr/` records hard-to-reverse platform decisions.
5. Runtime-specific wrappers such as `CLAUDE.md`, `.codex/`, `.claude/`, `.github/prompts/`, and hooks may add stricter instructions but must not weaken the shared constitution.
<!-- shiki-source-of-truth:end -->

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

## Mode Selection

- **Goal mode (default)**: the current directory (or the named target) is
  already a bootstrapped Shiki Target Repository (`.shiki/` exists with a git
  repo and GitHub origin). Run the Goal lifecycle below.
- **Setup mode**: `.shiki/` is missing. Run the start flow further down.

## Goal Mode — Requirements Definition to Spec Freeze to Execution

For a non-trivial Goal in a bootstrapped repository:

1. **Requirements Definition** (one continuous operator dialogue):
   - Run `grill-with-docs` via the Skill tool: one question at a time, with a
     recommended answer each, challenging terms against `CONTEXT.md` and ADRs.
   - Produce Context & Impact with a Workflow parallel exploration sweep
     (mandatory for non-trivial Goals, CI-08) and keep the run summary as
     evidence for the plan.
   - When settled, draft the PRD (`to-prd` when it should be published).
2. **Spec Freeze**: present the PRD/requirements summary and ask the operator
   for explicit approval. On approval, write the plan JSON with BOTH blocks:
   `grill_with_docs.status=complete` and `spec_freeze` (`status: frozen`,
   `approved_by`, `source`). Enumerate required external scopes/permissions
   (scope inventory, SF-02) BEFORE asking for the freeze.
3. **Execute**: `shiki plan ingest --plan-file PLAN.json`, then
   `shiki run --plan P-XXXX`, then drive the Goal autonomously with
   `shiki loop run --goal-id G-XXXX` (per-task dispatch via
   `shiki runner claude --task-id T-XXXX` remains available for manual
   control). Plans without `spec_freeze.status=frozen` are rejected by
   design. The loop auto-merges risk low/medium PRs when all required
   checks and CCA are green, and stops for the repair limit, Guardian
   gates, blocked evidence, or completion. Raise a Spec Amendment by
   interrupting the loop, re-grilling, and re-stamping the freeze.
4. **After freeze**: scope-moving discoveries pause the affected task and come
   back to the operator as a Spec Amendment (scoped re-grill, re-stamped
   freeze). Record the amendment durably: append an entry to the plan's
   `spec_freeze.amendments` list and write a ledger entry naming
   "Spec Amendment" with the operator's decision. Non-scope-moving
   interpretations are recorded in the Assumption Log (a ledger entry naming
   the assumption) and work continues.

## Setup Mode

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
8. Explicit approval to freeze the requirements (Spec Freeze) — set
   `approve_spec_freeze: true` in the answers file only after the operator
   says yes; `shiki start` fails without it

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

- At session start, read active distilled rules from `.shiki/memories/` (status `distilled`, `active: true`, not revoked or superseded) for applicable guidance; the same rules are injected deterministically into task handoffs as the `## Distilled Rules` section (§3.5).
- Treat the assigned implementer runtime (Claude Code by default, Codex when assigned) as implementer, CCA as completion judge, and MergeGate as merge authorization.
- Treat `/shiki` as a guided one-command entrypoint. Do not ask the user to run multiple setup commands.
- For non-trivial goals, use `grill-with-docs`, then Context and Impact, then PRD/issues/triage.
- After `grill-with-docs` is settled, prefer `shiki plan ingest` and `shiki run` over manually calling each lower-level command.
- For unattended execution, queue settled plans with `shiki daemon enqueue-plan` and process them with `shiki daemon run`.
- For headless runner integration, use `shiki runner next` and `shiki runner execute` so execution evidence lands in `.shiki/runner` and the Ledger.
- When a ready task is assigned to claude-code (the default), run the implementation adapter yourself with `shiki runner claude --target TARGET --task-id T-XXXX`. When it is assigned to Codex, run `shiki runner codex --target TARGET --task-id T-XXXX` instead of showing the user a manual command. Both materialize the task worktree, feed the handoff to the headless runtime (`claude -p` or `codex exec`), and record runner evidence. Ask the user only when the assigned runtime's auth/tooling is missing, the task is not dispatchable, or Guardian approval is required.
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
