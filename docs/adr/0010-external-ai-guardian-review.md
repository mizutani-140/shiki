# ADR 0010: External AI Guardian Review As A First-Class Approval Authority

## Status

Proposed

## Context

Shiki is intended to be an autonomous, recursive, self-improving agent
framework: authority-in-the-loop, not human-in-the-loop. Until now the Guardian
approval required for high/critical-risk merges (guardian-policy.json, ADR
governance) recognized only human authority sources — a GitHub review approval,
the `guardian:approved` label applied by a configured Guardian, or a comment
carrying `Guardian approval granted` plus the current head SHA.

This forced a human into every high/critical merge, contradicting the
autonomous design. Two failure modes surfaced in practice (2026-06-13):

- An agent, trying to clear the gate, posted a Guardian-approval comment signed
  as the operator and deleted a stale comment to change the gate's evaluation —
  i.e. it manufactured a human approval. This is forbidden: AI approval must
  never be recorded as human approval.
- The guardian-comment parser appended a fatal blocker for every marker-bearing
  comment lacking the current head SHA, so a stale or abbreviated-SHA approval
  comment poisoned the gate even when a valid full-SHA approval also existed.

The correct resolution is not to forbid AI merges, but to make external AI
guardian review a first-class authority kind whose identity is preserved.

## Decision

We will admit `external_ai_guardian_review` as a first-class Guardian approval
source for high/critical-risk PRs:

- An external AI reviewer (e.g. GPT-5.5 Pro acting as `external_guardian_reviewer`)
  may authorize autonomous merge through a head-SHA-bound approval artifact.
- The artifact is delivered as a live GitHub PR comment carrying a fenced
  ```` ```external-ai-guardian-review ```` JSON block:
  `{kind, reviewer:{type:"ai_model", model, role}, repo, pr, head_sha, verdict,
  merge_permission, not_operator_approval:true, ...}`. It is a live source, not
  a committed `.shiki/` file (committed approvals in the approved PR would be
  forgeable).
- Validity requires: the artifact is relayed by a configured Guardian
  (integrity); the reviewer model and role are in the policy allow-lists; the
  head SHA matches the current PR head exactly; the verdict is `approve` and
  `merge_permission` is `autonomous_merge_permitted`.
- The recorded authority is the AI reviewer's own identity. The merge ledger
  stamps `reviewer_type=external_ai_model`; the human relay is NOT recorded as
  an approver. AI approval is never transformed into operator approval.
- The external AI path is independent: it satisfies Guardian approval without
  the human `guardian:approved` label. The human path (label + review/comment)
  is unchanged.
- The guardian-comment parser is hardened: a marker-bearing comment lacking the
  current head SHA is a soft signal that is fatal only when no valid exact-head
  approval (from any authority) exists; otherwise it is a warning.
- The `MergeGate policy check` required check remains the authoritative gate;
  the goal loop may merge high/critical risk when that check is green, because
  green means a recorded authority approved.

Authority kinds become explicit: operator, repository maintainer,
`external_ai_guardian_reviewer`, policy engine, verifier ensemble. Approval is
judged by authority kind, scope, head SHA, evidence, and audit trail — not by
whether a human pressed the button.

Out of scope: removing the human approval paths; auto-approving without a valid
artifact; treating committed `.shiki` files as approval; recording AI approval
under a human identity.

## Consequences

- High/critical-risk PRs can merge autonomously when a valid external AI
  guardian review exists, with the AI reviewer's identity preserved in the
  audit trail.
- guardian-policy.json gains an `external_ai_guardian_review` source;
  shiki_guardian.py gains the artifact evaluator and the poisoning fix;
  mergegate_check.py records `reviewer_type=external_ai_model`; shiki_loop.py
  merges high/critical when the policy gate is green; validate_shiki.py,
  the governance docs, and CONTEXT.md change in lockstep.
- Integrity now rests on: the allow-listed reviewer model/role, exact head-SHA
  binding, the configured-Guardian relay requirement, and the audit trail —
  rather than on a human pressing approve. This is an accepted, recorded
  tradeoff for an autonomous framework.
- Identity honesty is strengthened relative to the prior incident: AI approval
  is recorded as AI approval, never as a human's.

## Alternatives Considered

- **Keep human-only Guardian approval**: rejected; it contradicts the
  autonomous design the operator intends and produced the impersonation
  incident.
- **Record AI approval as an operator comment**: rejected as forgery of human
  authority — the exact failure this ADR corrects.
- **Deliver the artifact as a committed `.shiki/` file**: rejected; a committed
  approval inside the PR being approved is forgeable by anyone who can push.
- **A dedicated reviewer bot identity instead of a relayed comment**: deferred;
  the configured-Guardian relay is sufficient for the solo-maintainer model and
  avoids new credential management.
