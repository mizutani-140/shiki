# ADR 0018: Carried Guardian Approval Across A Proven Base Sync

## Status

Proposed

## Context

Shiki runs on a strictly protected default branch. A high-risk PR merges only with
policy-backed Guardian evidence: the `guardian:approved` label plus a Guardian
review or comment, and — because `require_head_sha` is on — an approval comment
that carries the current 40-character PR head SHA (see `docs/agents/guardian-policy.md`).
The head-SHA binding is deliberate: it forces the Guardian to approve a specific
tree, not a moving branch.

That binding collides with the other thing strict protection requires: a PR must
be up to date with its base before it can merge. Every time an unrelated PR merges
to the default branch, the open PRs must sync their base, which moves each head to
a new SHA and invalidates the head-bound approval each one already had. The cost is
measured, not hypothetical. On 2026-08-01, landing five Contract PRs cost **four
extra Guardian approvals** — one per PR that had to re-sync after another merged —
for **zero content change**. The approvals bought no new information; they
re-attested a tree the Guardian had already attested, now reachable at a different
SHA only because the base moved underneath it.

Two adjacent problems were pure defects and are already fixed, not accepted: the
CCA workflow's shallow base fetch that invalidated every other open PR's approval
on each merge, and the model-transcribed head SHA (both PR #206). What remains is a
policy question: a base sync that changes nothing about the approved work should not
cost a fresh Guardian approval — **but only if Shiki can prove, from git alone,
that the sync changed nothing.**

The proof primitive (`scripts/shiki_sync_proof.verify_pure_base_sync`) and the
Guardian evaluator that consumes it (`shiki_guardian.evaluate_guardian_approval`,
the `guardian_comment_carried` source) were built and tested in isolation. Neither
does anything until a caller passes the flags that turn the carry on, so the
feature was invisible in CI. This ADR records the decision the two of them
implement, and this task wires it into the four CI invocations that judge Guardian
approval.

## Decision

We will carry a **prior-head Guardian comment approval** onto the current head when,
and only when, git proves the move from the approved head to the current head was a
**pure base sync** — the current tree is exactly the approved tree merged with new
base commits, and nothing else.

Scope and mechanism are deliberately narrow:

- **Only the Guardian comment source carries.** The carry joins the human-secondary
  set alongside `github_review` and `guardian_comment`; a GitHub review approval is
  already dismissed by its own commit binding and is out of scope. The
  `guardian:approved` **label leg is still required** — removing the label defeats
  even a valid carry.
- **Tree reproduction is the proof.** `verify_pure_base_sync` proves the sync by
  reproducing the tree: `git merge-tree --write-tree <approved_head> <base>` must
  equal `tree(current_head)`. Diff- and patch-level checks are provably insufficient
  (see Alternatives); only reproducing the actual tree is sound.
- **The conditions are ordered and fail closed.** Global refusals run *before* the
  git proof: the task risk must be exactly `high` (critical never carries), a
  label-only escalation to high/critical refuses, and a retargeted base
  (`base_ref_changed`) refuses. A carry-negating comment from the configured
  Guardian (a freehand revocation, marker or not) is detected before the
  approval-marker gate and hard-blocks. The approval SHA is extracted only after the
  marker, negation, configured-actor, and allowed-author checks pass — a
  non-Guardian's comment is never even parsed for a carry SHA. The git proof runs
  last. Any step that cannot be established refuses the carry.
- **The carried risk is floored never-weaker.** The resolved Guardian risk is the
  head task's risk floored by the base snapshot's, computed identically by the gate
  and by the CCA signal, so the signal can never be more permissive than MergeGate.
  A pure base sync has an empty diff, so the normal contract-immutability rule
  (keyed on the task file appearing in the diff) cannot catch a risk-lowering; the
  never-weaker floor is what does.
- **Default off, and code-pinned, not policy-pinned.** The carry is inert unless a
  caller passes both `--base-sync-carry` and `--default-branch`; carry-off is
  byte-identical to the pre-change gate. The four CI invocations that judge Guardian
  approval pass the flags, and `validate_shiki`'s workflow-invocation contract pins
  every one of them so a future edit that drops a flag fails validation loudly
  rather than silently disabling the feature. The scope of what carries lives in
  code (`_HUMAN_SECONDARY_SOURCES`, the ordered conditions, the never-weaker floor),
  not in the editable `.shiki/guardian-policy.json`, because the guarantee is a
  property of the proof, not a tunable.

## Consequences

A base sync that reproduces the approved tree merges with the approval it already
had. On a backlog where unrelated Goals land concurrently, that removes the
per-sync re-approval tax entirely for high-risk work — four approvals on the
measured 2026-08-01 run, and it does not improve with scale without this decision.
Keeping more than one Contract PR open at a time stops serialising unrelated Goals
(the alternative we would otherwise be forced into; see below).

**Accepted residual.** The merged tree is not the tree the Guardian semantically
reviewed — it is that tree plus whatever the base contributed since the approval.
The Guardian approved a diff against an older base; the carry lets that approval
stand against a newer base it never saw. We accept this, bounded three ways:

1. **Critical is excluded.** Only `risk == high` carries; critical always demands a
   fresh, current-head Guardian approval.
2. **Label escalation is an override.** Escalating the PR's risk to high/critical by
   label refuses the carry, so an operator who wants a fresh look can force one
   without touching code.
3. **The carry is disclosed.** It is recorded as the `guardian_comment_carried`
   source with the head it was carried from, so every carry is auditable after the
   fact rather than indistinguishable from a fresh approval.

What the Guardian keeps is everything that decides whether the *work* is correct:
the label leg, CCA judgment on the actual head, the required checks, contract
immutability, lock scope, and the never-weaker risk floor. What is given up is
narrow and provable: the base moved, and the tree still reproduces.

**New obligations.** The proof and the carry are security-relevant surface. They
carry adversarial tests of the same rigour as `test_shiki_governance_evidence.sh`
(`tests/test_guardian_carry.py`, `tests/test_guardian_carry_parity.py`), and the CI
wiring itself is now a tested contract (`tests/test_workflow_invocation_carry.py`,
plus the `test_shiki_control_plane.sh` and `validate_shiki` checks) so the two
silent-disable failure modes — a dropped flag, and a shallow or duplicated base
fetch — fail loudly.

## Alternatives Considered

**Keep only one Contract PR open at a time.** If nothing else merges to base while a
PR is open, the head never has to move and the approval never invalidates. Rejected:
it serialises unrelated Goals behind one another for a reason that has nothing to do
with their content, which is exactly the throughput Shiki's DAG exists to avoid.

**Accept diff or patch equality instead of tree reproduction.** Tempting, because
"the diff didn't change" reads like "nothing changed." It is unsound, and the
counterexamples are measured, not theoretical. `git merge -s ours <base>` produces a
merge whose `git diff <approved_head> <current_head>` is identical to the approved
diff while *reverting* the base — the diff matches and the tree is wrong. An evil
merge can carry an extra payload confined to base-touched paths, which a
patch-equality check against the approved diff will not surface. Only reproducing the
tree (`merge-tree --write-tree` equals `tree(head)`) rejects both.

**Require the sync to touch paths disjoint from the approved diff.** The idea: if the
base only changed files the PR didn't, the merge is "safe." Rejected on measurement —
across eight real base syncs, four overlapped the approved change on
`.shiki/tasks/*.json`, so a disjointness rule would refuse the carry for half of real
syncs while still not being sound (an overlap can be a clean, reproducing merge).
Tree reproduction handles overlap correctly; disjointness both over-rejects and
under-proves.

**Turn the carry on by policy in `.shiki/guardian-policy.json`.** Rejected: the
carry's soundness is a property of the git proof and the ordered conditions, not a
knob to be widened. Pinning it in code, and pinning the CI invocations in
`validate_shiki`, keeps a policy edit from silently loosening what carries.
