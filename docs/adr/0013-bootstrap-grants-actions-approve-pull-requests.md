# ADR 0013: Bootstrap Grants GitHub Actions Permission To Create And Approve Pull Requests

## Status

Proposed

## Context

Shiki's solo/self-running operation relies on the **CCA Review Bridge**
(`docs/agents/decision-control.md`): after CCA returns `complete` and Guardian
evidence is present when required, an automated GitHub PR review approval is
created so a repository with `defaults.required_review: true` can still satisfy
its required-approving-review branch-protection rule without a second human. The
bridge submits that approval as the `github-actions` identity using the
workflow's `GITHUB_TOKEN`.

GitHub gates this behavior behind a repository setting — *"Allow GitHub Actions
to create and approve pull requests"* (the REST field
`can_approve_pull_request_reviews` on `repos/{repo}/actions/permissions/workflow`).
It defaults to **off** on new repositories. `shiki doctor` already *diagnoses*
the gap (`doctor.github.workflow_permissions` fails when
`default_workflow_permissions != "read"` or `can_approve_pull_request_reviews`
is not `true`), but nothing in `shiki init` / `bootstrap-platform` ever *set*
it. The result was a silent MergeGate dead-end discovered only after a PR was
ready: branch protection was configured, but the bridge could not create the
approval it requires, so `required_review` could never be satisfied
autonomously. This relates to the G-0012 closeout-to-main work and the
Review-Bridge wiring tracked in closed #106.

Granting Actions the ability to approve pull requests is a real, hard-to-reverse
security tradeoff: it lets repository automation satisfy a human-review gate. In
a multi-maintainer repo that could let a compromised or buggy workflow
self-approve changes. So whether bootstrap turns it on by default — and with
what default token scope — is an ADR-worthy decision, not an implementation
detail.

## Decision

`shiki` bootstrap (`shiki init` and `bootstrap-platform`) will configure
repository Actions workflow permissions **immediately after branch protection,
inside the same `--protect` block**, via a new
`configure_workflow_permissions(repo, *, can_approve_pull_requests=True,
default_permissions="read", provider_config=None)` in `scripts/shiki_github.py`.
It `PUT`s `repos/{repo}/actions/permissions/workflow` with
`{default_workflow_permissions: "read", can_approve_pull_request_reviews: true}`,
mirroring `protect_branch`'s `gh api ... -X PUT --input -` pattern.

Specifically:

- **`can_approve_pull_request_reviews` defaults to `true`.** Shiki's default
  posture is solo/AFK autonomous operation (ADR 0008/0009/0012), where the CCA
  Review Bridge is the only path that can satisfy `required_review: true` after
  the CCA and Guardian gates have already passed. The default makes a
  freshly-bootstrapped repo able to run the autonomous loop end to end without a
  hidden manual settings step. The bridge's own guards remain the real
  protection: it only approves after a `complete` CCA verdict, it refuses to
  approve when the authenticated identity is the PR author, and CODEOWNERS plus
  Guardian evidence still govern critical paths.
- **`default_workflow_permissions` is `read`.** Least-privilege default for the
  workflow `GITHUB_TOKEN`; the approve-PR capability is granted explicitly and
  narrowly rather than by handing workflows broad write by default. This matches
  exactly what `doctor.github.workflow_permissions` expects, so bootstrap and
  doctor agree.
- **Failure warns, it does not raise.** Branch protection is the hard gate and
  keeps raising on failure. Workflow permissions require Actions-admin scope
  that a given token may lack, and the same setting can be applied by hand in
  *Settings → Actions → General*, so a failure emits a `warn(...)` with
  remediation text and lets an otherwise-complete bootstrap finish. This is the
  one deliberate asymmetry with `protect_branch`.
- It is gated by `--protect`. Under `--no-protect` the operator has opted out of
  Shiki branch governance, so the dry-run records a matching
  `workflow-permissions: skipped by --no-protect` note and nothing is set.

## Consequences

- A repo bootstrapped with default flags can run the autonomous loop to an
  auto-merge without a manual Actions-settings step; `shiki doctor`'s workflow
  permissions check passes on a fresh bootstrap instead of flagging a gap.
- Repositories accept that GitHub Actions may create and approve pull requests.
  This is safe for solo/self-running Shiki because the Review Bridge approves
  only post-`complete`-CCA, never as the PR author, and never in place of
  Guardian or CODEOWNERS governance — but a multi-maintainer adopter who does
  not want Actions self-approval should bootstrap with `--no-protect` (or set
  `can_approve_pull_request_reviews=false` out of band) and rely on human
  reviewers. Adopters should record that posture for their own repo.
- The setter warns rather than raises, so an operator whose token lacks
  Actions-admin scope still completes bootstrap and can apply the setting
  manually; the remediation text and `shiki doctor` point the way.

## Alternatives Considered

- **Default `can_approve_pull_request_reviews=false` and require an explicit
  opt-in flag.** Rejected as the default: it reintroduces the silent dead-end
  for the primary solo/AFK use case — branch protection would demand a review
  the bridge is forbidden to create — and contradicts `shiki doctor`, which
  treats `false` as a failure. The keyword-only parameter still allows an
  explicit `false` for callers that want it.
- **Grant `default_workflow_permissions=write`.** Rejected: broader than needed.
  Only the approve-PR capability is required; least-privilege keeps the token
  read by default.
- **Raise on failure like `protect_branch`.** Rejected: the capability can be
  set manually in repository settings and is not the hard merge gate, so a
  missing Actions-admin scope should not abort an otherwise-successful
  bootstrap. Warn-with-remediation preserves progress and is reconciled later by
  `shiki doctor`.
- **Leave it to `shiki doctor` to flag and a human to fix.** Rejected: that is
  the status quo that produced the dead-end; diagnosing a gap that bootstrap
  could close is exactly the kind of hidden manual step autonomous operation is
  meant to remove.
