# Evidence Integrity

CCA verdict evidence is workflow-generated runtime evidence. PR-authored `.shiki/gha` evidence is not trusted and remains blocked by MergeGate mutation policy.

## CCA Evidence Manifest

The Shiki CCA Completion workflow writes `.shiki/gha/cca-verdict.json` and then
generates `.shiki/gha/cca-evidence-manifest.json` before uploading the
`shiki-cca-evidence` artifact.

The manifest records:

- repository and PR number;
- current PR head SHA;
- workflow name, run id, run attempt, job, and event name;
- artifact name and runtime evidence path;
- required evidence file paths and SHA-256 digests;
- verdict status, Goal id, Task id, PR number, and verdict head SHA.

MergeGate downloads the artifact, refreshes live PR state, and validates the
manifest against the live PR head, task/Goal ids, verdict metadata, and required
file digests. CCA completion cannot be satisfied merely by editing a PR file.

## Ledger References

New ledger entries may include machine-readable `evidence_refs`:

- `github-pr` for PR number and head SHA;
- `github-workflow-run` for workflow run identity;
- `github-artifact` for the CCA evidence artifact and manifest path;
- `ledger-digest` for the entry canonical SHA-256 digest.

Historical ledger entries without `evidence_refs` remain valid. When
`evidence_refs` or `ledger_integrity` are present, validation checks their shape
and digest consistency.

## State Classes

`.shiki/manifest.json` classifies `.shiki/ledger/**` as
`append-only-evidence`. New current-task ledger entries may be appended, but
existing ledger files must not be modified or deleted by PRs. Runtime CCA and
MergeGate files under `.shiki/gha/**` classify as `workflow-runtime-evidence`
and must come from GitHub Actions artifacts, not committed PR files.

Cache, generated, and local-only state classes must not be trusted as durable
evidence. Mirror state is useful context, but GitHub operational state remains
authoritative when live GitHub state and `.shiki` disagree.

## Adversarial Evidence Tests

`scripts/test_shiki_governance_evidence.sh` is the regression suite for forged,
stale, and missing governance evidence. It covers forged Guardian evidence,
forged CCA verdicts and manifests, malformed ledger references and integrity
digests, stale PR head/check/task mirror state, forbidden state-class mutations,
missing required evidence, exact Guardian approval markers, and workflow static
wiring.

These fixtures intentionally exercise both direct evidence helper APIs and
MergeGate fixture runs. They protect the existing trust boundary: PR-authored
runtime evidence, loose ledger text, stale head SHA data, malformed
`evidence_refs`, and cache/local-only state must not satisfy CCA, MergeGate, or
Guardian gates.

## Boundary

This digest model is not external signing. It does not use KMS, GPG, Sigstore,
or a GitHub App. Future work may add a stronger hash-chain or signature model.
