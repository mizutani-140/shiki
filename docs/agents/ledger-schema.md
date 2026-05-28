# Ledger Schema

The Ledger is the durable evidence record for Shiki execution.

This document defines the minimum logical shape. Target Repositories may implement it as JSONL, YAML files, GitHub comments, artifacts, or a database as long as GitHub and `.shiki/` can reconstruct the state.

## Event Types

- `goal.created`
- `goal.grilled`
- `context_impact.recorded`
- `prd.created`
- `issues.created`
- `task.registered`
- `task.dispatched`
- `lock.acquired`
- `lock.released`
- `implementation.started`
- `test.red`
- `test.green`
- `refactor.completed`
- `pr.opened`
- `check.completed`
- `review.completed`
- `cca.completed`
- `repair.created`
- `repair.completed`
- `mergegate.completed`
- `pr.merged`
- `goal.completed`

## Minimum Ledger Entry

```json
{
  "event_id": "evt_...",
  "event_type": "cca.completed",
  "timestamp": "2026-01-01T00:00:00Z",
  "actor": "github-cca",
  "runtime": "claude-code-action",
  "goal_id": "G-0001",
  "task_id": "T-0001",
  "pr": 123,
  "branch": "shiki/T-0001-example",
  "head_sha": "abc123",
  "summary": "CCA returned repair_required because acceptance criterion AC-02 lacked evidence.",
  "evidence": [
    {"type": "check", "ref": "ci/test", "status": "pass"},
    {"type": "comment", "ref": "PR#123 comment"}
  ],
  "links": ["https://github.com/org/repo/pull/123"],
  "data": {}
}
```

## Rules

- Every material state transition must have a ledger event.
- CCA verdicts must include the PR head SHA they judged.
- Repair packets must link back to the CCA verdict or failed check that produced them.
- MergeGate must not rely on ledger entries for a different head SHA.
- Chat memory is not ledger evidence.
