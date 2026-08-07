# Decision Record Namespaces

This directory holds two independent decision namespaces:

- Shiki Architecture Decision Records (SADRs), named
  `SADR-NNNN-<short-slug>.md`, record hard-to-reverse Shiki platform decisions.
- Target Architecture Decision Records (ADRs), named
  `NNNN-<short-slug>.md`, belong to the target product.

The same four-digit number may appear once in each namespace. A platform
`SADR-NNNN` citation resolves only to `SADR-NNNN-*.md`; a target `ADR NNNN`
citation resolves only to `NNNN-*.md`.

## How Decision Records Work

- New Shiki platform decisions use the next zero-padded SADR number.
- New target product decisions use the target's own ADR sequence.
- The canonical list is the files in `docs/adr/`; no hand-maintained index is
  authoritative.
- Use [`template.md`](template.md) as the starting point and choose the filename
  namespace that owns the decision.
- Record a decision when it is hard to reverse, surprising without context, and
  a real tradeoff. Do not write a decision record for routine or easily
  reversible choices.

Using the directory as the source of truth lets independent changes add records
without contending on an enumerated index.

## Adding A Shiki SADR

1. Copy `template.md` to `docs/adr/SADR-NNNN-<short-slug>.md`.
2. Fill in the title, status, context, decision, and consequences.
3. Set the status to `Proposed`, then `Accepted` once the proper authority has
   approved the decision.
4. Open a PR following `CONTRIBUTING.md`.

## Adding A Target ADR

1. Copy `template.md` to `docs/adr/NNNN-<short-slug>.md` using the target's own
   sequence.
2. Cite it as `ADR NNNN`; do not prefix its filename with `SADR-`.
