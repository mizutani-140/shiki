# Shiki Control Commands

Shiki state transitions must go through the CLI once a repository is initialized
with `shiki init TARGET --repo OWNER/REPO`. Control commands refuse to run when
the target does not have a `.shiki` mirror, a git repository, and a GitHub
`origin`.

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

Codex then implements only the assigned task scope. If CCA rejects the PR, the
bounded repair loop starts with a repair packet:

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
- `.shiki/handoffs/*.md` records Codex task and repair handoffs.
- `.shiki/ledger/*.json` records durable evidence for every transition.

## Authority Boundary

Codex may implement and repair only after `dispatch check` is green. CCA judges
completion from PR evidence. MergeGate authorizes state transitions and merge
readiness. GitHub branch protection remains the hard gate.
