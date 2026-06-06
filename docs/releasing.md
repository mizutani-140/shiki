# Releasing Shiki

This document defines how Shiki is versioned, released, installed, upgraded, and
rolled back, and how the platform-runtime boundary differs from the
target-template boundary. The durable decision record is
[docs/adr/0007-packaging-and-release.md](adr/0007-packaging-and-release.md).

Shiki is a GitHub-first agentic engineering control plane. The operational
source of truth is GitHub (Issues, PRs, Checks, Reviews, merges); the
repository-local `.shiki/` mirror records durable evidence. Releasing follows the
same governance discipline as any other Shiki change: scoped branch, evidence,
MergeGate, and (for high-risk packaging changes) Guardian approval.

## Versioning policy (P2.2.1)

- Shiki uses [Semantic Versioning](https://semver.org/) `MAJOR.MINOR.PATCH`.
- The canonical version lives in the top-level [`VERSION`](../VERSION) file and is
  duplicated as the `version` field in [`pyproject.toml`](../pyproject.toml).
  Both must be updated together when bumping a release.
- Bump rules:
  - **MAJOR**: backward-incompatible changes to the Shiki constitution
    (`AGENTS.md`), schemas under `.shiki/schemas/`, workflow contracts, the CLI
    surface, or the install/manifest layout that require a target-repo migration.
  - **MINOR**: backward-compatible new capabilities (new CLI subcommands, new
    skills, new optional workflow jobs, additive schema fields).
  - **PATCH**: backward-compatible fixes, docs, and evidence-only changes.
- Pre-1.0 (`0.y.z`) caveat: while Shiki is `0.x`, the MINOR position may carry
  breaking changes; pin to an exact tag in target repositories.
- The release tag is the version prefixed with `v` (for example version `0.1.0`
  is tag `v0.1.0`). The release workflow refuses to publish a tag that does not
  match the `VERSION` file.

## Release process (P2.2.2)

Releases are tag-triggered and produced by
[`.github/workflows/release.yml`](../.github/workflows/release.yml). The workflow
uses only the built-in `GITHUB_TOKEN` (with `contents: write`) and requires no
additional secrets.

1. On a scoped branch, bump `VERSION` and the `version` field in
   `pyproject.toml` to the new SemVer value.
2. Open a PR, satisfy MergeGate (checks, CCA `complete`, review, ledger
   evidence, and Guardian approval for high-risk packaging changes), and merge.
3. From the merged commit on `main`, create and push the matching tag:

   ```bash
   git checkout main && git pull
   git tag v0.1.0
   git push origin v0.1.0
   ```

4. The `Shiki Release` workflow runs on the `v*` tag push. It:
   - verifies the tag matches the `VERSION` file,
   - runs `python3 scripts/validate_shiki.py` as a release gate,
   - publishes a GitHub Release for the tag with auto-generated notes via
     `gh release create --generate-notes --verify-tag`.

   The workflow can also be re-run for an existing tag via `workflow_dispatch`
   with the `tag` input.

5. Confirm the GitHub Release appears under the repository Releases page. The
   release and the tag are the durable release evidence.

## Install paths (P2.2.3)

Shiki ships as a repository template plus a dependency-free, standard-library
CLI in `scripts/`. There are no third-party Python runtime dependencies. The CLI
is intentionally **not** exposed as a `[project.scripts]` console entry point
because `scripts/shiki.py` is a dependency-free shim and `scripts/` is not
packaged as an importable distribution (see the ADR for the rationale).

Supported install paths:

- **Script install (official, recommended):** clone the repository (pinned to a
  release tag) and run the dependency-free shim's global install subcommand:

  ```bash
  git clone https://github.com/mizutani-140/shiki
  cd shiki
  git checkout v0.1.0
  python3 scripts/shiki.py install-global
  ```

  Run `python3 scripts/shiki.py install-global --help` to see the available
  options. This is the supported way to expose Shiki for global use; it does not
  rely on a packaged console entry point.

- **Run the dependency-free shim directly (no install required):**

  ```bash
  git clone https://github.com/mizutani-140/shiki
  cd shiki
  git checkout v0.1.0
  python3 scripts/shiki.py --help
  ```

  Requires Python `>=3.11` (matching `pyproject.toml`). No `pip install` step is
  needed to run the CLI.

- **`pipx` is NOT supported.** Because `pyproject.toml` declares no
  `[project.scripts]` console entry and `scripts/` is not packaged as an
  importable distribution, `pipx install git+https://…` would not expose a usable
  `shiki` command. Use the script-install path above instead. A `pipx`/PyPI
  install path may be reconsidered only after a future packaging restructure
  (see [docs/adr/0007-packaging-and-release.md](adr/0007-packaging-and-release.md)).

- **Install into a target repository:** use the bootstrap path documented in
  [docs/agents/bootstrap-command.md](agents/bootstrap-command.md) (`bin/shiki`)
  and the CLI install subcommands (`shiki install-target`,
  `shiki install-command`, `shiki install-global`).

## Platform-runtime vs target-template boundary (P2.2.4)

Shiki distinguishes two roles for files in this repository, both expressed in
[`.shiki/manifest.json`](../.shiki/manifest.json) (do not edit the manifest as
part of a release docs change):

- **Platform runtime** — the code, schemas, workflows, and docs that *operate*
  this Shiki platform repository. This includes `scripts/` (the CLI runtime),
  `.github/workflows/`, the validator, and the platform's own `.shiki/` evidence.
  These run Shiki; they are not all copied verbatim into target repositories.

- **Target template** — the subset that is *installed into* a Target Repository
  when it adopts Shiki. This subset is defined by `install.include` in
  `.shiki/manifest.json` (for example `.shiki/manifest.json`,
  `.shiki/config.yaml`, `.shiki/guardian-policy.json`, `.shiki/schemas/**`, and
  `.shiki/templates/**`), together with the directories listed under
  `install.create_directories`. Paths under `install.exclude_from_commit`
  (such as `.shiki/gha/**`) are deliberately not committed in targets.

A release therefore versions the whole platform, but only the `install.include`
template surface is what target repositories receive. Changes that alter
`install.include`, schemas, or the manifest layout are MAJOR-eligible because
they affect every installed target.

## Upgrade and migration (P2.2.5)

Target repositories upgrade by re-installing the newer template surface and then
applying any repository-local state migrations:

1. Re-run the install/bootstrap path for the target against the new Shiki version
   (see Install paths above).
2. Apply repository-local `.shiki/` state migrations with the migrate command:

   ```bash
   python3 scripts/shiki.py migrate status   # show current migration state
   python3 scripts/shiki.py migrate plan     # preview pending migrations (no mutation)
   python3 scripts/shiki.py migrate apply     # apply pending migrations (defaults to dry-run)
   ```

   `shiki migrate apply` defaults to a dry-run; review the plan, then apply for
   real once the plan is confirmed. Migration state is tracked under
   `.shiki/migrations/`.

3. Re-validate the upgraded mirror:

   ```bash
   python3 scripts/validate_shiki.py
   ```

When a release is MAJOR (constitution, schema, workflow-contract, or manifest
layout change), the release notes must call out the required migration steps.

## Rollback (P2.2.6)

If a release introduces a regression:

1. **Pin/downgrade the install** to the last known-good tag by checking out that
   tag and re-running the script install:

   ```bash
   git checkout v0.0.0
   python3 scripts/shiki.py install-global   # re-install the prior version globally
   ```

2. **Roll back repository-local state** if a migration was applied: inspect
   `python3 scripts/shiki.py migrate status`, and restore the prior `.shiki/`
   mirror state from git history for the affected target. Because `.shiki/`
   evidence is append-only, prefer restoring via version control rather than
   editing existing ledger entries.

3. **Do not delete or re-tag a published release tag** to "fix" a bad release.
   Instead, publish a new PATCH release (for example `v0.1.1`) that supersedes
   it, and note the superseded version in the release notes. Re-tagging or
   force-deleting tags is a destructive Git operation and requires Guardian
   authorization.

4. Record the rollback decision and evidence in the `.shiki/` ledger and on the
   relevant GitHub issue/PR.
