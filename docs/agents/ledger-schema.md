# Ledger Schema

The Ledger is the durable evidence record for Shiki execution.

This document defines the minimum logical shape. Target Repositories may implement it as JSONL, YAML files, GitHub comments, artifacts, or a database as long as GitHub and `.shiki/` can reconstruct the state.

## Event Types

The canonical ledger `type` vocabulary is:

- `goal-created`
- `context-impact`
- `task-registered`
- `lock`
- `check`
- `review`
- `cca-verdict`
- `repair`
- `mergegate`
- `completion`
- `handoff`

## Minimum Ledger Entry

```json
{
  "id": "L-0001",
  "type": "cca-verdict",
  "timestamp": "2026-01-01T00:00:00Z",
  "goal_id": "G-0001",
  "task_id": "T-0001",
  "actor": "github-cca",
  "runtime": "claude-code-action",
  "pr": 123,
  "branch": "shiki/T-0001-example",
  "head_sha": "abc123",
  "summary": "CCA returned repair_required because acceptance criterion AC-02 lacked evidence.",
  "evidence": [
    "ci/test passed",
    "PR#123 CCA comment recorded repair_required"
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
