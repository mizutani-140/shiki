# Shiki Migrations

`shiki migrate` manages repository-local `.shiki` state migrations. It is a
dependency-free framework for detecting, previewing, applying, and recording
state-layout changes without rewriting historical Shiki mirror records.

GitHub remains the operational source of truth for Goals, Issues, Pull
Requests, Reviews, Checks, and merge evidence. `.shiki/migrations/state.json`
is repository-local evidence that records which Shiki state migrations have
been applied.

## State

Migration state is stored at:

```text
.shiki/migrations/state.json
```

The state file records:

- `version`
- canonical `source_of_truth`
- applied migration records
- applied timestamp, actor, summary, and evidence

The state file is tracked, required by `.shiki/manifest.json`, and included in
target installation and manifest staging.

## Registry

The deterministic registry lives in `scripts/shiki_migrations.py`. Migration IDs
use this format:

```text
M-YYYYMMDD-NNNN-slug
```

The initial baseline migration is:

```text
M-20260604-0001-baseline
```

It records the existing post-P1.3.5 layout as accepted baseline evidence. It
does not rewrite historical `.shiki` records.

The Guardian policy migration is:

```text
M-20260604-0002-guardian-policy
```

It records `.shiki/guardian-policy.json` as tracked governance state after the
baseline migration.

The state classes migration is:

```text
M-20260605-0002-state-classes
```

It records explicit `.shiki` state classes in `.shiki/manifest.json`,
including mirror, append-only evidence, governance policy, contract,
migration-state, workflow-runtime-evidence, generated, cache, local-only, and
template classes. It does not rewrite historical state.

## Commands

```bash
python3 scripts/shiki.py migrate status --target .
python3 scripts/shiki.py migrate status --json --target .
python3 scripts/shiki.py migrate plan --target .
python3 scripts/shiki.py migrate apply --target .
python3 scripts/shiki.py migrate apply --execute --target .
```

`shiki migrate apply` defaults to dry-run and must not mutate files without
`--execute` or `--i-understand`. Dry-run output lists intended writes, including
`.shiki/migrations/state.json`.

Destructive migrations must require `--i-understand`, even when `--execute` is
present. T-0045 only adds the non-destructive baseline migration.

## Validation

`scripts/validate_shiki.py` validates the migration registry, dependency graph,
state JSON, applied records, baseline application, manifest coverage, and this
documentation.

The committed repository must have no pending migrations. Local targets may
show pending migrations before an operator explicitly runs `shiki migrate apply
--execute`.

## Doctor

`shiki doctor` reports migration readiness through:

- `doctor.migrations.registry`
- `doctor.migrations.state`
- `doctor.migrations.pending`

Doctor is diagnostic-only. It never applies migrations.

## Repair And Ledger

Migrations do not replace repair packets or ledger evidence. If a migration
reveals real state drift, use the Shiki repair loop and ledger evidence to
record the decision and the bounded correction.
