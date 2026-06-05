# Shiki Doctor

`shiki doctor` is Shiki's repository readiness diagnostic. It reports whether a
target repository is ready for Shiki operation without mutating repository
files, GitHub settings, secrets, labels, branch protection, or workflow state.

## Modes

Offline mode is the default:

```bash
shiki doctor --target .
shiki doctor --json --target .
```

Offline checks are local and dependency-free except for the existing runtime
auth probes used by Shiki entrypoint status. They cover:

- Shiki CLI entrypoint availability.
- configured local runtime auth readiness for Codex / Claude when those
  runtimes are assigned in `.shiki/config.yaml`.
- GitHub CLI availability/auth status.
- `.shiki/repo.json` provider metadata, with legacy missing metadata reported
  as a warning.
- git repository, current branch, and origin/provider match.
- required workflow file presence.
- `.shiki/config.yaml` required checks matching workflow job display names.
- CODEOWNERS coverage for critical Shiki paths.
- `.shiki/manifest.json` layout, required files/directories, and tracked
  runtime-only evidence.
- `.shiki/manifest.json` state class health through
  `doctor.state_classes.manifest`, `doctor.state_classes.unknown_paths`,
  `doctor.state_classes.runtime_only`, and `doctor.state_classes.append_only`.
- `.shiki/migrations/state.json` state validity, migration registry validity,
  and pending migration count through `doctor.migrations.state`,
  `doctor.migrations.registry`, and `doctor.migrations.pending`.
- CCA evidence manifest wiring through `doctor.evidence_integrity.manifest`.
- runtime registry import and config/task runtime assignments.
- `scripts/validate_shiki.py` contract drift status when available.

Online mode is opt-in:

```bash
shiki doctor --online --target .
```

Online checks use `gh` and the configured provider host from
`.shiki/repo.json`. They check:

- `gh auth status` for the configured host.
- repository existence and default branch.
- required secret existence without reading secret values.
- branch protection required checks.
- approving review count when `required_review: true`.
- code-owner review requirement when review enforcement is active.
- repository workflow permission defaults and pull request review approval
  permission for the CCA Review Bridge.

If GitHub permissions are insufficient for an online check, doctor reports a
warning or failure with remediation instead of crashing.

## JSON Contract

`--json` emits a stable object:

```json
{
  "status": "pass",
  "target": "/repo",
  "summary": {
    "pass": 0,
    "warn": 0,
    "fail": 0,
    "skip": 0
  },
  "findings": [
    {
      "id": "doctor.provider.repo_json",
      "status": "pass",
      "title": "Repository provider config",
      "summary": "Provider config is valid.",
      "remediation": "",
      "details": {}
    }
  ]
}
```

Finding ids are stable enough for automation. Status values are:

- `pass`: check passed.
- `warn`: check could not prove readiness but does not necessarily block local
  operation.
- `fail`: check found a condition that blocks readiness.
- `skip`: check is not applicable or was not requested.

Overall status is `fail` if any finding fails, `warn` if no finding fails but at
least one finding warns, and `pass` otherwise.

## Exit Codes

Default human and JSON modes exit non-zero only when the overall status is
`fail`.

Use strict mode when warnings should fail automation:

```bash
shiki doctor --json --strict --target .
```

`--strict` exits non-zero for `warn` or `fail`.

## Secret Safety

Doctor checks secret metadata only. It never reads or prints secret values,
environment token values, or token substrings. Secret findings report only:

- checked / unknown;
- configured / missing;
- remediation for setting or granting access to the secret metadata.

## Relation To validate_shiki.py

`scripts/validate_shiki.py` remains the authoritative repository contract
validator used by CI. Doctor surfaces that validator result as
`doctor.contract.validate_shiki` and adds operator-facing remediation around
repository readiness, runtime auth, provider metadata, and optional live GitHub
state.

Guardian policy diagnostics report `.shiki/guardian-policy.json` readiness
through `doctor.guardian.policy`, `doctor.guardian.approvers`, and
`doctor.guardian.solo_maintainer`. Online mode also checks whether GitHub issue
comments/events APIs are readable as `doctor.guardian.github_events`; this is
diagnostic only and never substitutes for MergeGate Guardian approval.

## Non-Goals

Doctor does not:

- auto-fix files or GitHub settings;
- create or apply migrations;
- mutate branch protection;
- set secrets;
- approve reviews;
- change Guardian, MergeGate, CCA Review Bridge, CODEOWNERS, or provider
  behavior;
- validate provider plugins beyond the current GitHub-compatible provider
  config.
