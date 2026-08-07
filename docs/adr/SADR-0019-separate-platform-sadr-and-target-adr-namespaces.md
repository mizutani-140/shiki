# SADR-0019: Separate Shiki Platform SADRs From Target ADRs

## Status

Accepted

## Context

Shiki installs its platform decision records into `docs/adr/` in every target
repository. Shiki and its targets previously used the same numeric
`NNNN-<slug>.md` namespace. A target that already owned ADRs 0013 through 0018
therefore acquired two records for each number during installation, making an
`ADR NNNN` citation ambiguous. Renumbering target decisions would rewrite target
history, while renumbering Shiki decisions per target would destroy their stable
platform identity.

Upgrades also need to remove the former Shiki-owned numeric paths without ever
mistaking a target-owned ADR for platform content. The install stamp already
records exact shipped paths and content digests, so it can prove ownership only
when both still match.

## Decision

Shiki platform decisions use the `SADR` namespace:

- records are named `docs/adr/SADR-NNNN-<slug>.md`;
- live platform citations use `SADR-NNNN`;
- target product decisions retain `docs/adr/NNNN-<slug>.md` and `ADR NNNN`;
- the reference validator resolves the two namespaces independently, so the same
  four-digit number may exist once in each namespace.

Before a forced upgrade writes anything, the installer inventories the exact
former numeric Shiki ADR paths. It may delete a legacy path only when the
existing install stamp contains that exact path and its recorded SHA-256 digest
matches the file currently on disk. All ownership blockers are reported
together and abort the upgrade before its first write. A non-forced install
never performs this cleanup.

Historical `.shiki/` evidence remains unchanged because it records the namespace
that existed when that evidence was produced.

## Consequences

- Targets may use any ADR number without colliding with Shiki's platform SADRs.
- Fresh installs preserve target ADR bytes and install a stable SADR set.
- Forced upgrades fail closed when legacy ownership cannot be proven, and a
  successful upgrade refreshes the stamp without removed legacy paths.
- Current governance, packaging, workflow, prompt, script, and skill references
  must move with the platform records.

## Alternatives Considered

- Collision-only refusal leaves affected targets permanently unupgradable.
- Renumbering target ADRs mutates target-owned history.
- Per-target Shiki renumbering makes platform citations non-portable.
