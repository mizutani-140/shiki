# ADR 0017: A Bookkeeping Closeout Does Not Inherit Its Task's Risk

## Status

Proposed

## Context

ADR 0012 made the goal loop push a task's completion to the default branch through a
closeout pull request: after the implementation PR merges, the loop opens a second PR
carrying `task.status = done`, `lock.state = released`, and — when the task completes
its goal — `goal.status = complete` plus one scorecard report. Nothing else. The
closeout exists so completion is durable on `main` rather than local-only.

MergeGate derives the Guardian requirement from the task's `risk_level`
(`_guardian_risk_labels` feeding `_builtin_guardian_risk_required`), and a closeout
PR is judged against the same task. A high or critical task therefore requires a
Guardian approval for a PR that contains no implementation — the work it records was
already approved and merged one PR earlier.

The cost is measured, not hypothetical. Across the 31 pull requests merged on
2026-07-29/30 while driving the audit-remediation backlog, 21 Guardian approval
actions were performed. Seven of them were closeout bookkeeping with a zero-line code
diff. That is a third of all operator approvals spent re-approving work the operator
had already approved, and it scales linearly: every high/critical task in every future
goal costs two approvals instead of one.

Two adjacent costs were pure defects and have been fixed rather than accepted — the
CCA workflow's shallow base fetch, which invalidated every other open PR's approval on
each merge (PR #206), and the model-transcribed head SHA (same PR). What remains is
not a defect. It is a policy: Shiki currently asks the Guardian to look at a class of
change that carries no code.

## Decision

A **Bookkeeping Closeout** does not inherit its task's risk for the Guardian gate.
MergeGate evaluates the Guardian requirement for such a PR as if the task were low
risk; every other gate applies unchanged.

A PR qualifies as a Bookkeeping Closeout only when MergeGate can prove all of the
following **from the diff and from base state**, never from the PR body, a label, or
any assertion the PR makes about itself:

1. Every changed path is under `.shiki/`. A single byte outside `.shiki/` disqualifies
   the PR.
2. No path is deleted. The mode is additive and status-mutating only.
3. Every changed path falls inside the task's declared locks unioned with its derived
   id-scoped mirror locks — the existing `files_outside_locks` rule, unchanged.
4. The task file's governance fields (`scope`, `non_goals`, `required_skills`,
   `risk_level`, `locks`, `acceptance_checks`, `test_command`) are byte-identical to
   the base snapshot. Only `status`, `expected_pr`, `closeout_pr`, `ledger_evidence`
   and `expected_branch` may differ — the ADR 0015 contract-immutability rule,
   unchanged.

   `expected_branch` is in that set because a closeout is *by construction* on a
   different branch from the implementation it closes, and the loop rewrites the
   field accordingly. It is safe to let it differ because MergeGate independently
   requires `expected_branch` to equal the PR's actual head ref, so a PR cannot set
   it to anything other than the branch it is really on. The first draft of this ADR
   omitted it, which made the exemption unreachable: every real closeout failed
   condition 4 on a field the loop always changes. See PR #209.
5. The state transitions are exactly the terminal set: task `review` → `done`, lock
   `active` → `released`, and, only when this task completes its goal, goal → `complete`
   with exactly one added scorecard report. Any other status value, any other
   transition, or a second report disqualifies the PR.
6. The task's implementation PR is merged. The approved work must already be on the
   default branch; a closeout may not precede the thing it closes out.

Failing any condition, the PR is judged exactly as today and inherits the task's risk.
The classification fails closed: when MergeGate cannot establish a condition — a
missing base snapshot, an unreadable task file, an unresolvable implementation PR — the
PR does not qualify.

The classification is computed by MergeGate itself, in the same place that decides
`requires_guardian`. There is no label, marker, or PR-body claim that can make a PR a
Bookkeeping Closeout, because the entire value of the rule depends on the operator not
having to trust the PR's self-description.

## Consequences

A high or critical task costs one Guardian approval instead of two. On the remaining
audit-remediation backlog that is roughly seven approvals; across future goals it
removes a per-task tax that has no corresponding decision.

We accept that a task's completion record can reach `main` without a Guardian looking
at it. What the Guardian gives up is narrow: the implementation was approved and merged
under the full gate, and conditions 1-6 mean the closeout can only carry the terminal
state of that same task. What the Guardian keeps is everything that decides whether the
work was correct — CCA judgment on the implementation PR, the `code-review` gate, the
required checks, contract immutability, and lock scope.

The residual risk is that a closeout marks a task `done` and releases its lock without a
human confirming that completion is warranted. That risk is carried by the CCA verdict
on the implementation PR, which is where completion is actually judged; the closeout only
transcribes it. Condition 6 prevents the transcription from running ahead of the thing it
transcribes.

A new obligation: the classifier is now security-relevant surface. It needs adversarial
tests of the same rigour as `test_shiki_governance_evidence.sh` — in particular that a PR
smuggling one non-`.shiki` byte, a governance-field edit, a deletion, an unexpected status
value, or an unmerged implementation PR is rejected and falls back to the risk-inheriting
path.

## Alternatives Considered

**Keep the current behaviour.** Safe, and it is what we have been doing. Rejected on
measurement: a third of operator approvals buy no information, and the ratio does not
improve with scale.

**Let the closeout PR declare itself with a maintainer-applied label**, as contract mode
does. Rejected: the label would be applied by the same operator whose approval it
removes, so it is not an independent second factor — it is the approval, renamed. The
classification must be derived, not asserted.

**Fold the closeout into the implementation PR** so no second PR exists. Rejected under
ADR 0012, which recorded why: a task file that is already `done` on the implementation
branch short-circuits the loop's review → CCA → merge → done ordering into a local
goal-completion before any merge happens.

**Have the loop auto-merge closeouts with elevated credentials.** Rejected outright: that
is a runtime authoring its own approval, which ADR 0010 and the constitution's
approval-record prohibition forbid. It would also be indistinguishable, in the evidence,
from the forgery those rules exist to prevent.

**Exempt `.shiki`-only PRs generally, rather than closeouts specifically.** Rejected as
too wide. A `.shiki`-only PR can register contracts, freeze specs, or mutate a goal's risk
level; contract mode and `goal_reconcile` exist precisely because those need *more*
scrutiny, not less. The exemption is bound to the terminal-state transition, not to the
directory.
