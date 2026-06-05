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
