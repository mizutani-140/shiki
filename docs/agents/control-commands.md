# Shiki Control Commands

Shiki state transitions must go through the CLI once a repository is initialized
with `shiki init TARGET --repo OWNER/REPO`. Control commands refuse to run when
the target does not have a `.shiki` mirror, a git repository, and a GitHub
`origin`.

## One Command Start

For normal use, `/shiki` should ask the missing questions and then run:

```bash
shiki start /path/to/repo --repo OWNER/REPO --goal "Goal title" --outcome "Observable outcome"
```

`shiki start` is the single command that performs GitHub-first setup, persists a
settled `grill-with-docs` plan, runs the Shiki orchestrator, creates the first
GitHub task issue, writes handoff evidence, commits the resulting state, and
pushes it unless disabled with `--no-push`.

The default engineering Skill Gate directory is
`/Users/kio.mizutani/Documents/lead-os/skills/engineering` when it exists, or
`~/skills/skills/engineering` otherwise. Override it with `--skills-dir` when a
target repository uses a different skills checkout.

When Claude Code invokes `/shiki`, it should ask for these values one at a time
instead of asking the user to manually chain setup commands:

- GitHub repo slug
- Project name
- Goal title
- Observable outcome
- Completion conditions
- Non-goals
- First vertical-slice task
- Acceptance checks

## Standard Flow

For non-trivial work, do not start by manually creating each task. First run
`grill-with-docs`, then persist its settled output as a plan:

```json
{
  "title": "Goal title",
  "outcome": "Observable outcome",
  "completion_conditions": ["Completion condition"],
  "non_goals": ["Out of scope"],
  "required_skills": ["grill-with-docs", "tdd"],
  "grill_with_docs": {
    "status": "complete",
    "source": "CONTEXT.md",
    "decisions": ["Settled decision"]
  },
  "spec_freeze": {
    "status": "frozen",
    "approved_by": "operator",
    "source": "Operator approved the PRD at the end of Requirements Definition"
  },
  "tasks": [
    {
      "title": "Vertical slice title",
      "scope": "Smallest end-to-end slice",
      "acceptance_checks": ["Public behavior is verified"],
      "locks": ["path:src/example/*"],
      "required_skills": ["tdd"]
    }
  ]
}
```

Then run:

```bash
shiki plan ingest --plan-file PLAN.json
shiki run --plan P-0001
```

`shiki run` creates the Goal, vertical-slice tasks, Task DAG, lock records, the
first dispatchable worktree record, run evidence, and ledger entries. It leaves
dependent or lock-conflicted tasks blocked instead of dispatching them.

For guided setup before the plan exists:

```bash
shiki plan guide --prompt "user goal"
```

The lower-level commands remain available for explicit control or repair:

## Daemon And Runner

For unattended execution on a local or self-hosted machine, queue a settled plan
and process it from the inbox:

```bash
shiki daemon enqueue-plan --plan-file PLAN.json
shiki daemon run --once
```

Omit `--once` when a supervisor such as `launchd`, `systemd`, or a long-running
terminal session should keep polling `.shiki/inbox`.

Headless runners pick up dispatchable tasks and record command evidence:

```bash
shiki runner next
shiki runner execute --task-id T-0001 --command "your-agent-command"
shiki runner claude --task-id T-0001
shiki runner codex --task-id T-0001
```

Use the runner command as the adapter boundary for Claude Code headless, Codex
headless, Hermes Runner, or another runtime. For claude-code-assigned tasks
(the default), use `shiki runner claude`; for Codex-assigned tasks, use
`shiki runner codex`. Both share the same adapter machinery: Shiki materializes
the registered worktree, sends the task handoff to the headless runtime
(`claude -p` or `codex exec`), records stdout/stderr/return code, and moves the
task to `review` on a zero exit. Do not turn this into a user instruction
unless the assigned runtime's auth/tooling is missing, dispatch is blocked, or
Guardian approval is required.

For a frozen Goal, the goal loop drives the whole post-freeze lifecycle —
dispatch, PR creation, CCA rerun-after-green, risk-gated auto-merge
(low/medium), bounded repair dispatch, dependent unblocking, and Goal
completion — stopping only for the repair limit, Guardian gates, blocked
evidence, or completion (a Spec Amendment is operator-initiated: interrupt
the loop, re-grill, re-stamp the freeze, restart):

```bash
shiki loop step --goal-id G-0001
shiki loop run --goal-id G-0001 --max-cycles 50 --interval 30
```

Every loop transition lands as ledger evidence; the engine decision is pure
and the executed action is exactly one per step.

`shiki runner execute` remains the generic adapter for other commands. It is
intentionally explicit: Shiki records the task, command, stdout, stderr, return
code, and Ledger evidence, but the runtime command itself is supplied by the
operator.

## Live Smoke

Before trusting a target repository, run:

```bash
shiki smoke live --plan-file PLAN.json --dry-run
```

When you intentionally want to create GitHub evidence:

```bash
shiki smoke live --plan-file PLAN.json --execute-github
```

The live smoke verifies GitHub auth, repository visibility, plan validity, Shiki
run orchestration, and optional GitHub Issue / PR evidence creation.
For a real PR smoke where the branch does not already exist, use:

```bash
shiki smoke live --plan-file PLAN.json --execute-github --push-branch
```

```bash
shiki goal create \
  --title "Goal title" \
  --outcome "Observable outcome" \
  --completion-condition "Completion condition" \
  --required-skill grill-with-docs \
  --required-skill tdd

shiki issue plan \
  --goal-id G-0001 \
  --title "Vertical slice title" \
  --scope "Smallest end-to-end slice" \
  --acceptance-check "Public behavior is verified" \
  --lock "path:src/example/*" \
  --required-skill tdd

shiki lock acquire T-0001
shiki dispatch check T-0001
shiki worktree allocate T-0001
```

Create durable GitHub and Codex handoff evidence:

```bash
shiki github issue --task-id T-0001
shiki handoff task T-0001
shiki github pr --task-id T-0001
```

The assigned implementer (Claude Code by default, Codex when assigned) then
implements only the assigned task scope. If CCA rejects the PR, the bounded
repair loop starts with a repair packet:

```bash
shiki repair packet \
  --task-id T-0001 \
  --pr 123 \
  --failing-item "missing verification evidence" \
  --minimal-change "add the requested evidence only" \
  --verification-command "python3 scripts/validate_shiki.py"

shiki handoff repair RP-0001
```

When the task is actually accepted, update task state and judge the goal:

```bash
shiki task status T-0001 --status done
shiki goal complete G-0001
```

## State Files

- `.shiki/goals/*.json` records goals and completion conditions.
- `.shiki/plans/*.json` records machine-readable `grill-with-docs` outcomes.
- `.shiki/tasks/*.json` records vertical-slice tasks.
- `.shiki/dag/*.json` records dependency edges.
- `.shiki/locks/*.json` records active lock ownership.
- `.shiki/worktrees/*.json` records assigned work surfaces.
- `.shiki/repairs/*.json` records bounded repair packets.
- `.shiki/reports/*.json` records goal completion judgments.
- `.shiki/runs/*.json` records orchestrator runs.
- `.shiki/handoffs/*.md` records implementer task and repair handoffs.
- `.shiki/inbox/*.json` records queued daemon work.
- `.shiki/runner/*.json` records headless runner command evidence.
- `.shiki/smoke/*.json` records live smoke results.
- `.shiki/ledger/*.json` records durable evidence for every transition.

## Authority Boundary

The assigned implementer runtime may implement and repair only after
`dispatch check` is green. CCA judges
completion from PR evidence. MergeGate authorizes state transitions and merge
readiness. GitHub branch protection remains the hard gate.
