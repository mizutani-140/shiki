# Memory Loop

Shiki learns across Goals. Failures are captured automatically as redacted raw
memories, promoted through machine-checked evidence to operator-approved
distilled rules, injected deterministically into handoffs, and summarized in a
ledger-derived scorecard at goal completion. This document describes the shipped
behavior (proposal `docs/proposals/0001-memory-loop-spec-freeze-review.md`).

## Memory entries

A memory is a current-state document at `.shiki/memories/MEM-<id>.json`
(`filename == id`, enforced by the validator). The audit trail is **not** the
file — it is the append-only `memory_transition` ledger events. Schema:
`.shiki/schemas/memory-entry.schema.json`; cross-file rules are enforced by the
memory-validation block in `scripts/validate_shiki.py`, which calls
`memory_entry_errors` from `scripts/shiki_memory.py` (fail-closed).

Key fields: `status`, `area` (coarse enum), `tags` (free strings), `applies_to`
(Consult-target areas), `claim`, `evidence`, `source`, and the distilled-only
lifecycle fields `rule`, `approved_by`, `approved_at`, `approval_ledger`,
`active`, `supersedes`, `superseded_by`, `revoked_at`, `redaction`.

## Promotion (fail-closed)

```
raw -> investigated -> verified -> distilled
```

No skipping. `verified` requires at least one local evidence reference
(ledger / report / exec). `distilled` requires an operator approval
(`approved_by` + `approval_ledger`) and is **refused in autonomous-execution
context** (the runner/loop sets a context variable that `distill`/`revoke`/
`supersede` check). CLI: `shiki memory capture | list | investigate | promote |
distill | revoke | supersede`.

## Capture (auto, redacted, fail-open)

Four capture points write redacted raw memories without ever breaking the loop:
repair (after guards), loop-stop, CCA-fail, and runner-fail (non-zero
returncode). Captured memories store a short redacted claim plus structured
evidence references only — never command-output bodies or secret-like tokens.
Invalid capture writes nothing and warns.

## Consult (deterministic injection, §3.5)

`write_task_handoff` always emits a `## Distilled Rules` section. Selection
(`select_distilled_rules` in `scripts/shiki_memory.py`) is a pure function:

- eligible = `distilled` AND `active: true` AND `superseded_by: null` AND
  `revoked_at: null`, carrying at least one selector (`area`/`applies_to`/`tags`);
- a rule matches when its `area`/`applies_to` overlaps the derived **consult
  context areas** or its `tags` overlap the derived **consult context tags**;
- order: `last_verified` descending, then MEM id ascending (missing
  `last_verified` sorts last);
- each selected rule is printed with its MEM id; when nothing matches the
  section says `none applicable`.

The handoff is regenerated on every dispatch (no write-if-missing cache), so the
injected rules are never stale. Reading never mutates memory state.

**CI-08 assumption (operator-approved, not a spec amendment).** The frozen spec
selects on `task.area` / `goal.area`, but the task and goal schemas define no
`area`/`tags` field. Rather than amend those schemas, Shiki derives a
non-persisted **consult context** from existing metadata: task lock paths map to
coarse areas and `required_skills` map to tags. No `area`/`tags` field is added
to task or goal records.

`H-12` (`docs/agents/checklists.md`) makes the section blocking when applicable;
CCA checks only the section's presence and the applicable / none-applicable
marker — never the rule selection or content (no double authority between
distilled rules and checklists). Session start (CLAUDE.md, `.claude/commands/
shiki.md`) also reads active distilled rules directly.

## Scorecard (ledger-derived, §3.6)

`goal complete` emits a scorecard computed only from ledger / task / report
state (ledger-id deduped, zeros + warnings for missing sources) plus
distillation **suggestions**. Suggestions never change memory status, and the
scorecard rides in the report file — `goal complete` stdout stays a single
`json_get_last`-compatible JSON object.
