# Shiki Control Commands

Shiki state transitions must go through the CLI once a repository is initialized
with `shiki init TARGET --repo OWNER/REPO`. Control commands refuse to run when
the target does not have a `.shiki` mirror, a git repository, and a GitHub
`origin`.

## Standard Flow

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

Codex then implements only the assigned task scope. If CCA rejects the PR, the
bounded repair loop starts with a repair packet:

```bash
shiki repair packet \
  --task-id T-0001 \
  --pr 123 \
  --failing-item "missing verification evidence" \
  --minimal-change "add the requested evidence only" \
  --verification-command "python3 scripts/validate_shiki.py"
```

When the task is actually accepted, update task state and judge the goal:

```bash
shiki task status T-0001 --status done
shiki goal complete G-0001
```

## State Files

- `.shiki/goals/*.json` records goals and completion conditions.
- `.shiki/tasks/*.json` records vertical-slice tasks.
- `.shiki/dag/*.json` records dependency edges.
- `.shiki/locks/*.json` records active lock ownership.
- `.shiki/worktrees/*.json` records assigned work surfaces.
- `.shiki/repairs/*.json` records bounded repair packets.
- `.shiki/reports/*.json` records goal completion judgments.
- `.shiki/ledger/*.json` records durable evidence for every transition.

## Authority Boundary

Codex may implement and repair only after `dispatch check` is green. CCA judges
completion from PR evidence. MergeGate authorizes state transitions and merge
readiness. GitHub branch protection remains the hard gate.
