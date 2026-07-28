# ADR 0015: Approve The Task Contract Before Dispatch, Not The Diff After It

## Status

Proposed

## Context

Guardian approval for high/critical risk is expressible only on a pull request.
Every approval source in `guardian-policy.json` — GitHub review, the
`guardian:approved` label, the `Guardian approval granted` comment, and the
`external_ai_guardian_review` artifact (ADR 0010/0014) — is bound to a PR, and
three of the four require the current head SHA. Approval therefore cannot exist
before an implementation PR exists.

The consequence is structural, not accidental: a high/critical task is
implemented in full, opens a PR, and only then is stopped by CCA with
`needs_guardian`. In this repository 43 of 80 registered tasks are high or
critical, so the Guardian stop is the normal path rather than the exception, and
each one burns a complete implementation cycle before the first governance
decision is made.

Two supporting defects make the current state worse than intended:

- The documented pre-dispatch gate is not enforced. `shiki dispatch check` marks
  high/critical work as blocked with no way to satisfy it, while the path the
  Goal Loop actually uses (`dispatchable_task_ids`, `decide_task_action`) never
  reads `risk_level` at all. `decision-control.md` already requires that a
  worktree be created only when "risk level does not require unresolved Guardian
  approval"; the code does not implement it.
- The task contract is mutable by the implementation that it governs. MergeGate
  resolves `risk_level` and `locks` from the PR head, PRs are permitted to change
  their own task file, and base-state comparison for `.shiki/tasks/` checks only
  deletion. An implementation PR can therefore lower its own `risk_level` and
  disengage the Guardian gate, or widen its own `locks` and escape lock scope.
  The frozen-contract binding (`_FROZEN_TASK_MATCH_FIELDS`) exists but applies
  only to `goal_reconcile` PRs.

Most of the machinery needed to fix this already exists. `goal_reconcile` is
already a deny-by-default PR mode that registers spec-frozen task contracts,
matches each against its frozen plan definition, and forces Guardian evaluation
from the Goal's risk level. MergeGate already enforces that changed files fall
inside the task's declared locks. The Goal Loop already delegates the merge
decision for high risk to the `MergeGate policy check`, and CCA already reads the
deterministic guardian-approval result rather than interpreting comments itself.
What is missing is a normal-flow moment at which a Guardian approves the
contract, and a rule that keeps the contract fixed afterwards.

## Decision

We will move Guardian approval for high/critical risk from the implementation
diff to the task contract, before dispatch.

**Contract PR.** A spec-frozen Goal whose risk is high or critical registers its
task contracts to the default branch through a Contract PR carrying only Goal,
task, and DAG registration — no implementation. The Guardian approves that PR
through the existing approval sources, unchanged. Because the default branch
requires the `MergeGate policy check`, the presence of the contract on the
default branch is itself the durable proof that Contract Approval was granted.

**Pre-dispatch gate.** A high/critical task is dispatchable only when its
contract exists on the remote default branch and agrees with local state on its
governance fields. The comparison is over governance fields only, never the
whole file: `try_acquire_locks` flips `status` to `ready` and appends a lock
ledger id before dispatch, while a contract registered to the default branch is
still `status: planned`, so a whole-file comparison could never succeed and
would leave the gate permanently closed. The gate fails closed on verification
failure: when it cannot check, it stops instead of dispatching. Low and medium
risk keep dispatching from local state as today.

**Scope Envelope.** A Contract Approval authorizes work bounded by the task's
declared locks plus a size ceiling (net added lines and changed file count). The
ceiling is a single default in `guardian-policy.json` — a governance-policy file
already protected by CODEOWNERS — and is not overridable per contract: a
per-contract override would have to be both registered (the task registration
field whitelist) and frozen (the governance-field set), and once frozen, any plan
declaring one registers a task without it and the Contract PR blocks itself. The
ceiling is a tripwire for scope explosion inside already-locked paths, not a work
budget: the default is set well above observed PR sizes so it rarely fires.

An envelope breach must surface only on the non-repairable policy gate, never on
the repairable `MergeGate metadata check`. Otherwise the Goal Loop routes it to
`dispatch_repair` — instructing a runner to make the breach go away — which is
exactly what this ADR forbids.

**Contract immutability.** After registration, an implementation PR must not
change the governance fields of its own task contract (`scope`, `non_goals`,
`required_skills`, `risk_level`, `locks`, `acceptance_checks`, `test_command`).
Only lifecycle fields (`status`, `expected_pr`, `ledger_evidence`) may change.
This closes the risk-downgrade and lock-widening paths and is required
independently of Contract Approval.

Resolution is base-when-present, not base-only. An implementation PR *adds* its
own task file: today the Goal Loop registers a task's contract on the task branch
rather than on the default branch first, so at the merge base the contract does
not exist. Resolving governance fields from the base snapshot alone would read
empty locks and an absent risk level for every first-registration PR — blocking
every implementation in the repository while simultaneously disengaging the
Guardian gate. The rule is therefore: the base snapshot is authority when the
contract exists in it; otherwise head is authority; and risk may never resolve
weaker than base.

**Contract Amendment.** When implementation proves the approved contract wrong or
too narrow, the loop stops rather than repairing. A new Contract PR re-approves
the corrected contract, and implementation resumes. An envelope breach is never
an auto-repair target.

**Evaluation placement.** Contract Approval is evaluated by a new, separately
tested function, and MergeGate accepts *either* live PR approval *or* Contract
Approval. `evaluate_guardian_approval` is not modified. A defect in the new path
therefore degrades to "Contract Approval does not apply" — the current behavior —
rather than weakening the existing approval path.

The same OR-merge must reach the deterministic CCA Guardian signal, not only
MergeGate. The `mergegate` job depends on the `cca` job, `enforce_cca_verdict`
fails any verdict that is not `complete`, and the signal the CCA prompt reads is
computed from `evaluate_guardian_approval` alone. Without the OR-merge at the
signal, a contract-approved PR still returns `needs_guardian`, the CCA job fails,
and the MergeGate policy check never runs — the gate this ADR changes would never
be reached. The CCA *prompt* needs no change: a `contract_approval` entry in the
signal's existing `sources` array satisfies its current wording.

Explicitly out of scope: deriving risk from the diff, mechanically detecting
architecture-gate triggers such as auth or schema changes, and reducing how many
tasks are classified high risk. Those are separate decisions.

## Consequences

A high/critical Goal reaches its first governance decision before any code is
written, and an approved contract lets its implementation PRs merge through the
same automatic path as low/medium risk. The wasted implement-then-block cycle
disappears. Guardian review shifts from reading diffs to reading contracts, which
is both earlier and smaller; the external AI Guardian path (ADR 0010/0014) works
on contracts with no change.

We accept that the Guardian authorizes a class of change without seeing its
implementation. This is a real reduction in what Guardian review inspects. It is
bounded by three things: the envelope is enforced deterministically at merge, the
contract cannot be edited by the work it governs, and CCA plus the `code-review`
gate still judge whether the authorized work was implemented correctly. The
residual risk is dangerous code written *inside* approved paths and under the
size ceiling; that risk is carried by CCA and review, not by the Guardian gate.

New obligations: one extra PR per high/critical Goal; a stop-and-re-approve path
that the operator must service when a contract turns out to be wrong; and
adversarial tests for the new approval path equivalent in rigor to
`test_shiki_governance_evidence.sh`.

Bootstrap costs, which apply only while this ADR is being implemented. The tasks
that build Contract Approval merge under the existing rules, so the ones
dispatched after the pre-dispatch gate lands but before the approval path lands
need a Guardian-approved Contract PR *and* a live Guardian approval on their
implementation PR — double approval, because the second one is precisely what
the last task removes. Separately, a Contract PR keeps the maintainer-applied
label as its second factor; the CLI that opens one must not apply that label to
itself, or the two-factor property collapses to one.

Follow-up work, deliberately not decided here: making the architecture gate
deterministic, and reducing the 43/80 high-risk ratio through finer risk
classification at plan time.

## Alternatives Considered

**Approve dispatch only, keep PR approval for merge.** Safe and small, but it
adds a step without removing the CCA stop — the implementation still waits for a
diff-time Guardian decision. It does not address the stated problem.

**Record pre-approval in a committed `.shiki/` file.** Rejected on existing
grounds: `CONTEXT.md` already lists committed approval files as forgeable. The
implementation runtime has write access to the repository and could author its
own approval.

**Bind approval to a contract digest in a Goal Issue comment.** Avoids the extra
PR, but requires modifying `evaluate_guardian_approval` — the most
security-critical function in the repository — and rebuilding the adversarial
test suite that constrains it, so that a defect in the new feature could weaken
the existing approval path.

**Reuse the architecture-gate thresholds (100 net lines, 3 new files) as the
envelope ceiling.** Rejected against measurement: the median merged PR in this
repository is ~110 net added lines across 7 files, so these thresholds would fire
on nearly every PR and make fallback the normal path. Those numbers classify
risk; they do not bound authorized work.

**Auto-escalate risk on envelope breach instead of stopping.** Returns the
workflow to a diff-time Guardian decision — the exact behavior this ADR removes.

**Resolve governance fields from the base snapshot only.** Rejected against the
registration flow: implementation PRs add their own contract, so the base has no
task file at first registration. A base-only rule blocks every implementation PR
in the repository and turns the Guardian gate off in the same state.

**Let a contract raise its own envelope ceiling.** Rejected as self-deadlocking:
the override must survive task registration (a hard-coded field whitelist) and
must be frozen to be meaningful, but once frozen, a plan declaring an override
registers a task that does not carry it, and the frozen-definition match rejects
the registration. A single CODEOWNERS-protected default in the policy file keeps
raising the ceiling a Guardian decision without the deadlock.

**Detect architecture-gate triggers in the diff as a third envelope axis.**
Deferred. A deterministic auth/schema/secret detector would make false negatives
a silent hole in the gate, and building it is a separate goal.
