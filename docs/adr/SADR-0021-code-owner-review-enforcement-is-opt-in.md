# SADR-0021: GitHub Code-Owner Review Enforcement Is Opt-In

## Status

Proposed

## Context

`protect_branch` in `scripts/shiki_github.py` derived branch protection's
`require_code_owner_reviews` from the approving-review count:

```python
"require_code_owner_reviews": review_count > 0,
```

So asking for one approving review also demanded that an approval come from a
CODEOWNER. Those are two different constraints, and Shiki can only satisfy one
of them.

Shiki's design is that the CCA Review Bridge supplies the approving review
(SADR-0013): after a `complete` CCA verdict it POSTs an `APPROVE` review under
the GitHub Actions identity, and `mergegate_check.review_approved` accepts any
review whose state is `APPROVED` regardless of author. That satisfies
`required_approving_review_count: 1`. It cannot satisfy
`require_code_owner_reviews`, because a bot can never be listed as a CODEOWNER.

The installed `.github/CODEOWNERS` assigns `/.github/workflows/*`,
`/.shiki/config.yaml`, `/scripts/*`, `/CLAUDE.md` and more to the repository
owner. In the solo posture SADR-0013 explicitly targets, that owner is also the
author of every Goal Loop PR, and GitHub forbids self-approval. The result is
not a slow path but a closed one: **any PR touching a CODEOWNERS-governed path
can never be merged.** Observed live on `mizutani-140/shiki-atlas` PR #2, whose
only governed file was a newly added `.github/workflows/pages.yml` — an entirely
ordinary first slice.

The failure is also undiagnosable from inside the loop. A code-owner-blocked PR
reports `mergeStateStatus: BLOCKED`, which `shiki_loop.py` passes through to the
merge effector; `gh pr merge` then fails and the loop emits `stop_blocked` with
`"resolve manually (branch protection, conflicts, or auth)"` — three candidate
causes, none of them the real one.

The obvious workaround does not work. Setting `defaults.required_review: false`
turns the required `Validate Shiki mirror` check red, because
`scripts/test_shiki_control_plane.sh` asserts that MergeGate blocks a PR with no
approving review. The config flag and the contract test are jointly committed to
review being required.

Two further facts shaped the decision:

- **Nothing in Shiki consumes code-owner approval as evidence.** It appears
  nowhere in `mergegate_check.py`, `shiki_guardian.py`, `enforce_cca_verdict.py`
  or `.shiki/guardian-policy.json`. `validate_shiki.validate_codeowners_governance`
  checks only that the CODEOWNERS *file* covers the critical paths with the
  configured owner — never that an owner approved anything. It is the only
  merge-blocking condition in Shiki with no ledger entry, no MergeGate
  visibility, and no recorded identity, which `CONTEXT.md`'s **Authority** term
  otherwise requires.
- **For the paths that matter it duplicates a gate Shiki already models.**
  Contract Approval (SADR-0015) gives the Guardian a pre-implementation,
  lock-bound gate over exactly these paths, and workflow/CI changes trip the
  architecture gate into high risk anyway.

`docs/agents/decision-control.md` previously stated that branch protection *must*
require code owner reviews, and T-0032 (risk `critical`, done) shipped that
coupling deliberately. This SADR reverses that decision.

## Decision

`require_code_owner_reviews` becomes an independent, opt-in setting rather than a
value derived from the review count.

1. `.shiki/config.yaml` gains `defaults.required_code_owner_review`, read by
   `shiki_config.configured_required_code_owner_review`. It **defaults to
   `false`**, matching the solo/self-running posture SADR-0013 names as the
   default adopter.
2. The value is effective only when `defaults.required_review` is true.
   `required_review: false` forces it false, because GitHub still demands
   code-owner approval at `required_approving_review_count: 0` — a deadlock with
   no approving-review requirement behind it to justify the cost.
3. `required_approving_review_count` is unchanged. The CCA Review Bridge keeps
   satisfying it. Only the code-owner constraint is separable.
4. `protect_branch` takes the value as an explicit keyword argument and stays a
   pure payload builder; `shiki_bootstrap` resolves it from the target's config
   at both call sites.
5. `defaults.required_code_owner_review` joins `GOVERNANCE_CRITICAL_KEYS` for
   `.shiki/config.yaml`, and the key ships explicitly in the template. Both are
   required: `_governance_diff` compares template leaves against target leaves,
   so a key absent from the template produces no diff in a target that lacks it,
   and the installer summary would never name it — a silent governance downgrade
   at the next `--protect`.
6. `shiki doctor` compares the configured value against live protection in both
   directions, asymmetrically. Config-requires-but-GitHub-does-not is a `fail`.
   GitHub-requires-but-config-does-not is a `warn` naming the deadlock, because
   doctor cannot tell a deadlocked solo repository from a healthy
   multi-maintainer one: that turns on whether a code owner other than the PR
   author exists, which is not in the protection payload.

`.github/CODEOWNERS` is still installed, and `validate_shiki` still enforces its
coverage. What becomes optional is GitHub's *enforcement* of approval, not the
ownership record.

## Consequences

- A freshly bootstrapped solo-maintained target can merge its first vertical
  slice. That was the motivating failure.
- A multi-maintainer adopter who wants GitHub-enforced path ownership sets the
  key to `true`. Because `protect_branch` PUTs the full protection object and
  GitHub's PUT replaces protection wholesale, configuring it by hand in GitHub
  settings would be stomped at the next `shiki init --protect`; the config key is
  the only form that survives re-init. This is why the setting is a key rather
  than an instruction to configure it out of band.
- **Real governance is given up in the multi-maintainer case.** With the key
  false, a low-risk PR touching a CODEOWNERS path — a typo fix in `AGENTS.md` —
  merges on the Bridge's approval alone. In a solo repository that loss is zero,
  since the sole owner is the sole author and the sole merger. In a
  multi-maintainer repository it is real, and opting in is how it is restored.
  High and critical risk are unaffected: Contract Approval and the Guardian gate
  still apply.
- Existing targets are not silently downgraded, because the installer names the
  key in its end-of-run summary alongside its `.new` file.
- The platform repository's own live protection was already drifted from its
  config (`require_code_owner_reviews: false`, `required_approving_review_count:
  0`). Before this change, reconciling that drift by running `shiki init
  --protect` would have set code-owner review on and deadlocked the platform
  repository itself. That latent trap is removed.
- `require_code_owner_reviews` was not the only protection setting MergeGate
  cannot observe — `required_conversation_resolution` and `enforce_admins` are
  equally invisible. This SADR does not rule on them; it records the observation
  so a future goal can decide whether unobservable merge gates are acceptable in
  general.

## Alternatives Considered

**Keep the coupling and add a second code owner.** Rejected. It fixes the
deadlock only by putting a human in every AFK loop, which defeats the loop.

**Model code-owner approval so MergeGate can see it.** Rejected as a much larger
change that still deadlocks solo repositories until a second owner exists. The
gate is being made optional precisely because Shiki does not model it.

**Widen `required_review` into an enum (`none | any | code_owner`).** Rejected.
It makes the incoherent combination unrepresentable, but changes the type of a
key consumed by `mergegate_check.enforce_review_policy` — a CODEOWNERS-critical
file — and conflates a MergeGate concern with a branch-protection-only one.

**Default the new key to `true` and make solo maintainers opt out.** Rejected.
It preserves the current posture for existing repositories, but every new adopter
hits the wall on their first slice, which is the failure this SADR exists to
remove. SADR-0013 already names solo/self-running as the default posture.

**Derive the value by inspecting CODEOWNERS against the operator identity.**
Rejected. The inference is fragile and hides a governance choice inside bootstrap
logic instead of stating it in config.
