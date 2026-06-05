# Shiki State Classes

`.shiki/manifest.json` is the canonical state class contract. `kind` describes
an artifact, while `state_class` controls trust and PR mutation policy.

GitHub operational source of truth remains live GitHub Issues, Pull Requests,
labels, reviews, comments, checks, artifacts, and branch protection. It is
classified as `github-operational-source` and is not stored directly under
`.shiki`.

## Classes

| State class | Trust model | PR mutation rule |
| --- | --- | --- |
| `github-operational-source` | GitHub live state is authoritative. | Not repository state. |
| `mirror` | Repository-local mirror of GitHub state. | Current task or current goal updates only. |
| `append-only-evidence` | Durable ledger evidence with evidence refs when available. | Append only for current task evidence. |
| `governance-policy` | Machine-readable governance config. | Declared locks and Guardian coverage required. |
| `contract` | Schemas, prompts, templates, and runtime contracts. | Declared locks required. |
| `migration-state` | Repository-local migration records. | Current-task migration evidence required. |
| `workflow-runtime-evidence` | GitHub Actions artifact evidence. | Forbidden in PR files. |
| `generated` | Regenerated from canonical inputs. | Forbidden unless promoted to a tracked contract. |
| `cache` | Cache data, not durable evidence. | Forbidden. |
| `local-only` | Local runtime data. | Forbidden. |
| `template` | Files copied into target repositories. | Declared locks required. |

## Enforcement

MergeGate classifies changed `.shiki/**` paths through
`scripts/shiki_state_classes.py`. Unknown `.shiki/**` paths block. Changes to
`workflow-runtime-evidence`, `cache`, or `local-only` classes block because PR
authors must not commit runtime evidence, cache data, or local-only state.

`append-only-evidence` is reserved for `.shiki/ledger/**`. PRs may add new
current-task ledger entries listed by the task. Existing ledger entries must not
be modified or deleted.

`mirror` paths such as `.shiki/tasks/**`, `.shiki/goals/**`, and
`.shiki/locks/**` remain proposed mirror updates. MergeGate checks that task
and goal updates are scoped to the current task and Goal, and it compares
protected mirror state against the base branch snapshot.

## Validation And Doctor

`scripts/validate_shiki.py` requires every manifest directory and file entry to
declare a known `state_class`, requires every state class to have a policy, and
checks that tracked `.shiki/**` paths are represented by the manifest.

`shiki doctor` reports state class readiness through:

- `doctor.state_classes.manifest`
- `doctor.state_classes.unknown_paths`
- `doctor.state_classes.runtime_only`
- `doctor.state_classes.append_only`

Doctor is diagnostic-only. It does not mutate files or replace CCA, MergeGate,
Guardian approval, or GitHub branch protection.

## Migrations

State class introduction is recorded by
`M-20260605-0002-state-classes`. The migration records the new manifest
semantics without rewriting historical `.shiki` state.
