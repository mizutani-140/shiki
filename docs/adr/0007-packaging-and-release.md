# ADR 0007: Packaging And Release For Shiki

## Status

Accepted

## Context

Shiki is a GitHub-first agentic engineering control plane distributed as a
repository template plus a dependency-free, standard-library CLI under
`scripts/`. Until now the repository had no declared version, no release
mechanism, and no documented install/upgrade/rollback path. P2.2 (Goal G-0012,
issue #101) requires:

- a versioning policy (P2.2.1),
- a release workflow (P2.2.2),
- a packaging/install path (P2.2.3),
- a documented platform-runtime vs target-template boundary (P2.2.4),
- an upgrade/migration path (P2.2.5),
- rollback guidance (P2.2.6).

Constraints from the Shiki constitution (`AGENTS.md`) shape the design:

- The default CLI is the dependency-free `scripts/shiki.py` shim; packaging must
  not introduce third-party runtime dependencies or break the shim.
- Releases must run inside the operator's subscription-authenticated toolchain
  and must not require extra secrets beyond the built-in `GITHUB_TOKEN`
  (consistent with ADR 0005's no-API-key-by-default stance).
- `.shiki/manifest.json` already encodes the install surface (`install.include`,
  `install.create_directories`, `install.exclude_from_commit`), and it must not
  be mutated as part of this change.
- This change touches a new workflow plus packaging structure, so it is
  architecture-gate / high-risk and requires Guardian approval.

This is a hard-to-reverse platform decision (it sets the version source of
truth, the release trigger, and the public install contract), so it is recorded
as an ADR.

## Decision

### Versioning

- Use Semantic Versioning, starting at `0.1.0`.
- The canonical version is the top-level `VERSION` file, duplicated as the
  `version` field in `pyproject.toml`; both are bumped together.
- The release tag is `v<version>`; the release workflow refuses to publish a tag
  that does not match `VERSION`.

### Packaging

- Add `pyproject.toml` as metadata-only: project name, version, description,
  `requires-python = ">=3.11"`, license, and URLs.
- Do **not** declare a `[project.scripts]` console entry point. `scripts/` is not
  packaged as an importable distribution and `scripts/shiki.py` is a
  dependency-free shim; a console entry would require restructuring scripts into
  an installable package and risk breaking the shim. The supported invocation
  paths are documented instead (pipx-from-git for a pinned checkout, or running
  the shim directly with `python3 scripts/shiki.py`).

### Release workflow

- Add `.github/workflows/release.yml`, triggered on `v*` tag pushes (plus a
  `workflow_dispatch` for republishing an existing tag).
- The workflow verifies the tag matches `VERSION`, runs
  `python3 scripts/validate_shiki.py` as a gate, and publishes a GitHub Release
  with `gh release create --generate-notes --verify-tag`.
- The workflow uses only `GITHUB_TOKEN` with `contents: write`; no extra secrets.
- It pins `actions/checkout@v5` to satisfy the Node 24 workflow policy enforced
  by the validator.

### Boundaries and lifecycle

- Document the platform-runtime vs target-template boundary at the doc level in
  `docs/releasing.md`, referencing `.shiki/manifest.json` `install.include`
  /`template` semantics without editing the manifest.
- Document upgrade via re-install plus `shiki migrate {status,plan,apply}`, and
  rollback via pinning to a prior tag and superseding bad releases with a new
  PATCH (never re-tagging or force-deleting published tags).

## Alternatives Considered

1. **`[project.scripts]` console entry (`shiki = "...:main"`).** Rejected for
   now: it requires turning `scripts/` into an installable package and changing
   import targets, which is out of scope, risks the dependency-free shim, and
   pulls in build-backend complexity. Revisit if/when the CLI is restructured
   into a proper package.
2. **Third-party release action (e.g. `softprops/action-gh-release`).**
   Rejected: `gh release create` with the built-in `GITHUB_TOKEN` keeps the
   workflow dependency-free and avoids pinning/maintaining an extra third-party
   action.
3. **Publishing to PyPI.** Rejected for now: Shiki is distributed as a
   repository template + git-installable CLI, not a PyPI library; PyPI would add
   credentials/secrets and a different versioning contract. Can be added later as
   its own ADR if a packaged distribution is needed.
4. **Dynamic version (read `VERSION` via a build backend `dynamic` field).**
   Rejected to keep `pyproject.toml` backend-agnostic and dependency-free; the
   small duplication between `VERSION` and `pyproject.toml` is guarded by the
   release workflow's tag/VERSION check.

## Consequences

- Shiki gains a single canonical version, a tag-triggered GitHub Release flow,
  and documented install/upgrade/rollback paths.
- Bumping a release requires editing two files (`VERSION` and `pyproject.toml`);
  the release workflow enforces they agree with the tag.
- The CLI remains dependency-free and is invoked via the shim or pipx-from-git;
  no console entry point is provided yet.
- The target-template surface remains governed solely by `.shiki/manifest.json`;
  releasing versions the whole platform but ships only `install.include` to
  targets.
- Future PyPI distribution or a console entry point are deferred and would each
  warrant their own ADR.
