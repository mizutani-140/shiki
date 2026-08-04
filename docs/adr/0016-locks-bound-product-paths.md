# ADR 0016: Locks bound product paths; the Shiki mirror is bound by record identity

## Status

Proposed

- Date: 2026-07-28
- Deciders: operator (mizutani-140), Claude (planner)
- Related: ADR 0004 (GitHub-first with a `.shiki` mirror), ADR 0009 (Spec Freeze),
  ADR 0012 (autonomous loop closeout to main), ADR 0015 (Contract Approval before
  dispatch), remediation goal task R-02, the live PR #179 deadlock.

## Context

A full day of live operation produced a repo-wide stall: one task held an active
lock and no other task could be dispatched, including the task that would have
repaired the stuck one.

The mechanism was traced and confirmed. MergeGate resolves the task contract,
dependencies, locks and ledger evidence from the pull request head checkout
(`mergegate_check.py:162-193`, `--target` default `.`, workflows checking out
`pull_request.head.sha`). The Goal Loop therefore syncs mirror state onto the
task branch so MergeGate can see it (`shiki_loop._sync_state_to_branch`, whose
docstring names that reason). `files_outside_locks` applies to every changed
path including `.shiki/`, so those synced files must be covered by a declared
lock — and the lock authors reached for is `path:.shiki/**`. Because
`active_lock_conflicts` conflicts on lock-pattern overlap alone, before any
changed-file intersection, two tasks that declare it are mutually exclusive
regardless of what they actually touch.

Measurement rejects the obvious conclusion. Against the live lock holder, none
of the 22 tasks in the four spec-frozen plans becomes dispatchable when every
`.shiki` lock is removed: all 22 also declare `path:tests/**` and
`path:scripts/test_shiki_*.sh`, and identical globs always overlap. Removing the
`.shiki` locks *and* those two test globs makes 9 of 22 dispatchable. Pairwise
across the frozen backlog the conflict rate is 100% today, 100% after removing
`.shiki` locks, and 28.1% after removing the test globs as well.

The serialization is therefore over-determined by coarse plan-time globs, of
which the mirror lock is one instance and not the binding one. Three amplifiers
turned it into a deadlock rather than a queue: locks are held for the whole task
lifecycle and released only after the closeout PR merges; `_unblock_ready_tasks`
skips a conflicted task silently; and `active_lock_conflicts` emits one blocking
reason per (held lock × owner), so a collision reads as noise.

Tracing the chain also exposed a separate defect. For `.shiki/tasks`, `goals`,
`locks`, `ledger` and `repairs`, `enforce_untrusted_shiki_mutations` already
binds each changed file to the PR's own task or goal by path literal — a
strictly tighter rule than any lock. For the other eleven mirror directories
(`dag`, `handoffs`, `inbox`, `memories`, `plans`, `reports`, `runner`, `runs`,
`smoke`, `starts`, `worktrees`) there is no identity rule at all, and
`path:.shiki/**` is a blanket allow: an implementation PR declaring it may
rewrite another goal's DAG, another goal's completion report, distilled
memories, or a spec-frozen plan. The lock that causes the artificial
serialization is the same lock that authorizes the mutations nothing else gates.

`.shiki/manifest.json` already declares the mirror's `pr_mutation` policy as
`current-task-or-current-goal-only` — identity, not locks — and its
`trusted_authority` as `github-live-state`. `decision-control.md` and
`execution-control.md` both show canonical lock sets containing product paths
only. The code does not implement what the contracts state.

## Decision

We will separate the two duties that `locks` currently conflates.

**Locks bound product paths.** A task's declared `locks` express mutual
exclusion over contended repository files, and mutual exclusion only. They are
evaluated by pattern overlap as today. After the mirror-identity rule below is
in force, `files_outside_locks` no longer applies to paths under `.shiki/`, and
no task declares a `.shiki` lock. `LOOP_SHIKI_STATE_LOCK`,
`locks_cover_shiki_state` and the warn-only validator advisory that recommends
`path:.shiki/**` are removed, together with the three source comments and the
test docstring that describe a dispatch-time guarantee removed in `6dd7dda`.

**The mirror is bound by record identity.** Every tracked `.shiki` directory
gains an identity rule in `enforce_untrusted_shiki_mutations`: a pull request may
change only records owned by its own task or its own goal, may not delete a
tracked mirror record, and is blocked when ownership cannot be resolved. Task,
goal and lock records are owned by filename; ledger, repair, worktree, runner,
report, run, handoff and memory records are owned by their `task_id` / `goal_id`
field; a plan is owned by the goal whose `source_plan` names it, and a goal with
no `source_plan` blocks. These rules branch on path literals and record content,
never on the manifest, so they cannot be disabled by editing the manifest on the
PR head. The existing base-snapshot comparison is retained as an independent
second factor and extended to every tracked mirror directory.

**Lock granularity is a planning rule.** A declared lock must not be a directory
glob over a shared namespace when the files are nameable at plan time. Test
files, documentation files and named source modules are nameable; runtime-
allocated ledger, runner and report ids are not — and those live in `.shiki`,
which locks no longer govern. Existing frozen contracts are brought into line by
a one-time Spec Amendment per plan, which must land before ADR 0015's contract
immutability makes `locks` unwritable from the implementation PR.

**Lock state is resolved from the durable side.** MergeGate resolves the lock
registry, and ADR 0015's governance fields, from the base `.shiki` snapshot when
present, falling back to head only when absent. The `MergeGate metadata check`
job is given `--base-shiki`, which it does not receive today. Locks are not
derived from the live open-pull-request set: a pull request body is untrusted
text, third parties can open pull requests, and closing one fires no event on
the pull request it blocked.

**A blocked task is a named state.** `_unblock_ready_tasks` records a
blocked-on-lock stop naming the owning task instead of skipping silently, and
`active_lock_conflicts` emits one reason per conflicting owner.

Explicitly out of scope: moving MergeGate's contract resolution off the PR
checkout wholesale; making the mirror a projection of GitHub state; introducing
new lock kinds; moving ledger evidence out of the repository; and unwinding
ADR 0012's closeout pull request. Registration of multi-task goals is ADR 0015's
Contract PR and is not re-decided here.

## Consequences

Dispatch stops being globally exclusive: a stalled task holds only the product
paths it actually contends for, so an unrelated goal can proceed and a repair
for the stalled task is no longer blocked by the stall itself. On the current
frozen backlog, combined with the lock-narrowing amendment, 9 of 22 tasks become
dispatchable against the live holder and the greedy maximum concurrent set is 6.

Mirror authorization becomes stricter, not looser. Eleven directories that are
today authorized by a blanket glob become identity-scoped, closing the path by
which an implementation PR can rewrite a foreign DAG, report, memory or
spec-frozen plan. Every existing per-file identity rule and the base-snapshot
byte comparison are retained.

Risks accepted. First, goal-scoped identity is only as strong as `goal_id`,
which is resolved from the PR body and the head task file; the mitigation is to
add `goal_id` to ADR 0015's frozen governance field set, and until that lands a
task file can name a foreign goal. Second, base-when-present resolution is
unavailable on fork pull requests, where the head is contributor-controlled;
fork pull requests already cannot complete a Shiki merge, and this decision does
not change that posture. Third, mutual exclusion remains enforced by a single
coordinator process at dispatch; two coordinators can still race, and MergeGate's
base-resolved check is a backstop, not a distributed lock. Fourth, de-serializing
means sibling tasks of one goal can concurrently touch the goal record and the
goal DAG; those remain writable only by the registration path and the closeout
path, not by implementation pull requests.

New obligations: one Spec Amendment per frozen plan to rewrite `locks`, ordered
before ADR 0015 T1; extension of the adversarial suites in
`scripts/test_shiki_adversarial_state.sh` and
`scripts/test_shiki_governance_evidence.sh` to cover the eleven newly-scoped
directories; and updating `scripts/test_shiki_loop_lock_guard.sh`,
`tests/test_loop_lock_guard.py` and `tests/test_loop_lock_validator_hint.py`,
which pin the behavior being removed.

Follow-up, deliberately not decided here: whether purely additive files under a
locked directory should be expressible as a lock that does not conflict, and
whether the loop's transitive sync of ledger `evidence` references can be
deleted once the Contract PR carries its own source plan.

## Relationship to ADR 0004

This ADR **clarifies something ADR 0004 left unspecified**. ADR 0004 decides that
GitHub is the operational source of truth and that each target repository keeps a
`.shiki/` mirror. It says nothing about pull requests writing the mirror, does
not name MergeGate, and does not make the mirror an input to a gate. Nothing in
ADR 0004's Decision or Consequences is amended, superseded or weakened: the
mirror still exists, still records the same artifacts, and pull request
boundaries remain the coordination surface. What this ADR settles is the question
0004 never reached — by what authority a pull request may write a mirror record —
and it answers it the way `.shiki/manifest.json` already declares
(`pr_mutation: current-task-or-current-goal-only`).

It **amends ADR 0012**, whose Decision item 5 states that "Loop-executed tasks
declare the `path:.shiki/**` lock, which covers every `.shiki` file the closeout
stages ... so `files_outside_locks` is satisfied without per-file lock
bookkeeping." That clause is replaced: the closeout's mirror writes are
authorized by record identity, not by a declared lock. ADR 0012's substantive
decision — that completion reaches `main` through a closeout pull request rather
than local attestation — is preserved unchanged.

It is **complementary to ADR 0015**, not a substitute. ADR 0015 owns contract
immutability, the pre-dispatch gate, the Contract PR and the Scope Envelope. This
ADR owns what a lock means and who may write the mirror. The one ordering
constraint between them is that the lock-narrowing Spec Amendment must precede
ADR 0015 T1's freezing of the `locks` field.

## Alternatives Considered

**Move MergeGate's contract resolution to a default-branch store and forbid all
`.shiki` writes on implementation PRs.** Rejected on three grounds. It presupposes
its own output — the goal and plan must already be on the default branch before
the only writer that can register them may run. Migrating the Guardian signal to
that store makes every registration PR fail closed to `needs_guardian` at every
risk level, because the task file is by definition absent from the store, which
removes the low/medium autonomous path ADR 0008/0009 exists to provide. And it
leaves schemas, manifest, config, guardian policy, templates, migrations, plans,
worktrees, runner records, runs and handoffs with no writer at all in a
repository whose product is `.shiki`. Measured benefit on the frozen backlog:
zero, because the binding colliders are not `.shiki` locks.

**Type locks by contention semantics: exclusion locks versus non-conflicting
mirror channels.** Rejected. A lock that never conflicts silently defeats another
task's live exclusion lock over the same record. The ownership predicate it
depends on authorizes against a `goal_id` the pull request controls, and the
namespace is not in fact ownership-partitioned — 56 ledger entries carry no
`task_id`, and one goal DAG is writable by 35 registered tasks. The correct
expression of its insight is that locks do not govern the mirror at all, which is
what this ADR decides.

**Make the mirror a projection written only by a registration writer and a
post-merge projection job.** Rejected. It requires a bypass actor with write
access to the protected default branch; it makes its own master invariant
readable from a manifest that pull requests may edit; it deletes both reconcile
modes and leaves a corrupted mirror with no in-band repair path, contradicting
ADR 0004's consequence that mirror conflicts are repaired against GitHub state;
and its content-derived ids are rejected by `validate_shiki`'s currently-green id
pattern.

**Derive lock conflicts from the live open-pull-request set.** Rejected. Task
identity comes from free-text pull request bodies, so any account that can open a
pull request can turn a required check red on someone else's; closing the
hostile pull request fires no event on the victim; and it replaces an ordered
dispatch-time mutex with an unordered judgment-time observation in which two
overlapping pull requests deadlock symmetrically after both have been fully
implemented.

**Restore the dispatch-time `path:.shiki/**` injection removed in `6dd7dda`.**
Rejected as an inversion: it augments the lock record, while `files_outside_locks`
reads `locks` from the task file, so it would widen serialization while fixing
nothing. The independent review that removed it reached the same conclusion.

**Narrow the mirror lock to per-record paths and keep `files_outside_locks`
applying to `.shiki`.** Rejected against measurement: ledger, runner and report
ids are allocated at runtime into flat directories, so the finest lock a plan can
express is `path:.shiki/ledger/**`, which conflicts with itself. It also leaves
the eleven unscoped directories authorized by nothing.
```

---
