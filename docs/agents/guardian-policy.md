# Guardian Policy

`.shiki/guardian-policy.json` is the machine-readable Guardian approval policy.
It defines the risk levels that require Guardian approval, the configured
Guardian users and teams, allowed approval sources, solo-maintainer behavior,
and explicit exclusions.

High-risk and critical PRs require policy-backed Guardian evidence:

- the `guardian:approved` label is present;
- the label was applied by a configured Guardian when label actor evidence is
  available;
- plus either an approved GitHub review from a configured Guardian user/team or
  a Guardian approval comment from a configured Guardian user/team;
- when `require_head_sha` is true, the approval comment must include the
  current PR head SHA.

The default policy configures `mizutani-140` as the Guardian user and supports
team slugs syntactically. Team membership must not silently approve a PR when
membership verification is unavailable.

Solo maintainer mode is explicit. If it is enabled, the PR author may count as
Guardian only when their login is listed in the policy, the rationale is
non-empty, the `guardian:approved` label is present, and the approval comment
references the current PR head SHA. If disabled, PR author approval is rejected.

CCA Review Bridge is not Guardian approval. It exists to satisfy ordinary
GitHub required-review policy after CCA verdict enforcement. advisory Claude review is not Guardian approval. Loose ledger text and PR body prose are not Guardian approval evidence.

MergeGate evaluates Guardian approval from live GitHub evidence gathered by the
CCA completion workflow:

- `.shiki/gha/live-guardian-comments.json`
- `.shiki/gha/live-guardian-events.json`
- `.shiki/gha/live-guardian-timeline.json`

If those files are missing for high-risk or critical work, MergeGate blocks.
For lower-risk work, missing Guardian evidence files may be reported as
diagnostic warnings.

To request Guardian approval, provide a review or comment that includes the
approved PR head SHA, then add `guardian:approved`. Guardian approval comments
use the exact marker `Guardian approval granted`. Re-run CCA so MergeGate uses
fresh live evidence.

The head-SHA binding is positional, not a substring match. The approval comment
must carry the current 40-character head SHA as the sole content of its own line,
in the exact shape `shiki guardian status` renders (the policy marker line, a
blank line, then the head SHA alone) — pasted verbatim. The gate parses the SHA
only after the marker, negation, and configured-Guardian actor checks pass, so a
comment from a non-Guardian is never even read for a SHA. A head SHA merely
quoted inside other prose (for example when a Guardian quotes the gate's own
status comment to explain a blocker), an abbreviated SHA, two or more 40-hex
tokens, or an empty PR head all fail the binding and are recorded as the same
soft blocker a stale comment produces — never a silent pass. When the binding
fails the Guardian requirement still resolves as required.

`scripts/test_shiki_governance_evidence.sh` fixes the adversarial cases around
this policy. It verifies that label-only approval, negative text such as
"no Guardian approval evidence is present", stale-head comments, unconfigured
actors, CCA Review Bridge reviews, advisory Claude reviews, and close-but-not
exact approval phrases do not satisfy Guardian approval.

## Carried Guardian Approval Across A Proven Base Sync (SADR-0018)

Strict branch protection requires a PR to be up to date with its base before it
merges, and `require_head_sha` binds a Guardian comment approval to a specific head
SHA. Together they invalidate an existing approval every time the base moves under
an open PR, even when the base sync changes nothing about the approved work. The
base-sync carry lets a Guardian comment approval that bound to a **prior** head
stand for the **current** head when — and only when — git proves the move from the
approved head to the current head was a **pure base sync** (the current tree is
exactly the approved tree merged with new base commits, proved by tree
reproduction). This does not weaken the head-SHA binding for anything else: a
stale-head comment with no such proof remains the soft blocker described above.

The carry is recorded as the `guardian_comment_carried` source and is bounded:

- The `guardian:approved` **label leg is still required**; removing the label
  defeats even a valid carry.
- Only the **Guardian comment** source carries, and only when the task risk is
  exactly **high**. Critical never carries and always demands a fresh, current-head
  approval.
- A label-only escalation to high/critical, a retargeted base, or a configured
  Guardian's freehand revocation each refuse the carry. The proof runs last, after
  the marker, negation, configured-actor, and allowed-author checks; a
  non-Guardian's comment is never parsed for a carry SHA.
- The carried risk is floored **never-weaker** against the base snapshot, computed
  identically by the MergeGate gate and the CCA signal, so the signal can never be
  more permissive than the gate.

The carry is **off by default and byte-identical to the pre-carry gate** unless a
caller passes both `--base-sync-carry` and `--default-branch`. Shiki's four
Guardian-judging CI invocations pass them — the `guardian_approval_signal.py`
signal, the `shiki guardian status` blockers render, and the two `mergegate_check.py`
policy checks — and `validate_shiki`'s workflow-invocation contract pins the flags on
each so a future edit cannot drop them silently. The proof reads git history over
`origin/<default_branch>` and reuses each job's existing base fetch; a shallow
(`--depth`) or second base fetch would make the proof unresolvable and silently
disable the carry, so neither is permitted. CCA reads `guardian_comment_carried` as
a recorded authority in the deterministic signal and does not re-derive it. See
SADR-0018 for the full decision, the rejected diff/patch-equality and path-disjointness
alternatives, and the accepted residual.

## External AI Guardian Review (SADR-0010)

`external_ai_guardian_review` is a first-class approval source for high/critical
risk, distinct from any human approval. An external AI reviewer (e.g. GPT-5.5
Pro acting as `external_guardian_reviewer`) authorizes autonomous merge through
a head-SHA-bound artifact delivered as a live PR comment carrying a fenced
```` ```external-ai-guardian-review ```` JSON block: `{kind, reviewer:{type,
model, role}, repo, pr, head_sha, verdict:"approve",
merge_permission:"autonomous_merge_permitted", not_operator_approval:true}`.

The artifact is valid only when relayed by a configured Guardian (integrity),
the reviewer model/role are allow-listed in `external_ai_guardian_review`, the
head SHA matches the current PR head exactly, and the verdict authorizes merge.
The recorded authority is the AI reviewer's own identity: the merge ledger
stamps `reviewer_type=external_ai_model`, and the human relay is never recorded
as an approver. AI approval is never transformed into operator approval. The AI
path does not require the `guardian:approved` human label.

The guardian-comment parser ignores a stale or abbreviated-SHA approval comment
(records it as a warning) once a valid current-head approval exists from any
authority; such a comment is only a blocker when it is the sole approval
attempt.

## External AI Guardian UI Adapter (SADR-0014)

When the external reviewer is reached through a ChatGPT Pro UI, **Codex App is
the External AI Guardian UI Adapter** — the transport and validation runtime.
**Claude Code is the implementer/repairer and must not drive this Guardian UI
path for its own implementation work.** GPT Pro is the approval Authority,
GitHub carries the live artifact, and MergeGate verifies.

Shiki provides the deterministic, UI-free contract the adapter consumes
(`scripts/shiki_guardian_review.py`, exposed as `shiki guardian` subcommands).
These commands never drive a ChatGPT UI; they produce and verify artifacts:

- `shiki guardian packet --task-id <T> --pr <n> --pr-data <file> [--output <file>]`
  builds an **External AI Guardian Review Packet** from the task contract and
  Codex-gathered PR evidence, injects PR-type review focus areas, and validates
  it against `.shiki/schemas/external-ai-guardian-review-packet.schema.json`.
- `shiki guardian prompt --packet <file>` renders the deterministic GPT Pro
  prompt (reviewer identity/role, Evidence Review → Adversarial Review →
  Authority Verdict, the three verdicts, the GitHub connector as optional
  corroboration, and the fenced approval artifact to emit only when approving).
- `shiki guardian verify-response --packet <file> --response <file>` parses the
  reviewer output and accepts approval ONLY when the verdict is `approve` AND a
  fenced `external-ai-guardian-review` artifact validates against the packet's
  repo / PR / head SHA and the allow-listed reviewer model/role (the same
  `validate_ai_review_artifact` contract the PR-comment path enforces).

**Packet lifecycle.** The packet is review *input* evidence only. It is built
fresh from durable PR/check/task/repository evidence, fed to the reviewer, and
discarded as transport. It is never approval evidence and must not be committed
by the PR under review; if provenance is recorded, record source refs, PR, head
SHA, and a digest — not the packet as trusted state.

**Non-approval routing.** A non-`approve` verdict never changes implementation
directly. `verify-response` routes `request_changes` to a bounded **Repair
Packet** (`route: repair_packet`) and `insufficient_evidence` to **Evidence-only
/ evidence repair** work (`route: evidence_packet`). An `approve` verdict whose
artifact is missing or fails validation is rejected (`route: rejected`), never
merged. Human Guardian approval paths remain available as fallback.
