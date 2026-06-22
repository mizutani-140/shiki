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

`scripts/test_shiki_governance_evidence.sh` fixes the adversarial cases around
this policy. It verifies that label-only approval, negative text such as
"no Guardian approval evidence is present", stale-head comments, unconfigured
actors, CCA Review Bridge reviews, advisory Claude reviews, and close-but-not
exact approval phrases do not satisfy Guardian approval.

## External AI Guardian Review (ADR 0010)

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

## External AI Guardian UI Adapter (ADR 0014)

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
