# ADR 0011: Autonomous Quality-Gate Evidence — Deterministic Facts And An Independent Verifier

## Status

Proposed

## Context

The Shiki 0.2.0 autonomous post-freeze loop (ADR 0008/0009, PRD #119 G-D) must
take a ready task to an auto-merged PR with no human action. A live run on
2026-06-17 proved the loop self-implements (a headless `claude -p` runner wrote
a correct, passing test) and the Memory Loop capture fires, but it cannot reach
auto-merge: MergeGate and CCA require evidence the autonomous path never
produces — `required_skill` evidence for `tdd`/`code-review`, the CCA PR-12
pre-PR code-review section, and a deterministic verification record.

The naive fix is to let the headless implementer self-attest: instruct it to
"do TDD, run a code review, and write the evidence." That collides with the
platform maxim — **"LLM outputs may vary. State transitions must not vary."** —
and with the finding (Anthropic engineering blog; Fable-5 prompting guidance)
that models self-critique poorly: grading in an *independent context window*
outperforms self-review. A runner that writes its own "code review found 0
findings" ledger is a varying, self-graded, forgeable state transition.

The CCA already embodies the right pattern (an independent judge in a separate
GitHub Actions context). The pre-PR quality gates must follow the same shape.

## Decision

Autonomous quality-gate evidence is produced by the **loop**, from deterministic
observable facts and an independent verifier — never by the implementer runtime
self-attesting.

- **TDD / verification evidence is a machine-observed fact.** After the
  implementer runs, the loop executes the task's tests in the worktree and
  records a `check` ledger of the command and its green result. The evidence is
  "tests are present and pass (loop-observed)", not "the runner says it followed
  red-green". Red-first ordering is the implementer runtime's concern and is not
  gated, because it cannot be observed deterministically without trusting the
  runner.
- **Code-review evidence comes from an independent verifier.** Before opening
  the PR, the loop dispatches a **read-only** `claude -p` reviewer in a separate
  context (`--allowedTools` restricted to read tools; no edit tools) with a
  `--json-schema` structured-findings contract. The loop parses the verdict
  deterministically and records it as the `code-review` ledger plus the
  `## Pre-PR code review` PR-body section (PR-12). A dispatch or parse failure is
  **fail-closed** (review-not-done → block; never silently pass). Same model as
  the implementer, separate context — the independence is the context boundary,
  exactly as for CCA.
- **Blocking review findings feed the existing repair loop** (the verdict
  becomes a repair packet; the implementer fixes; the reviewer re-runs), bounded
  by the standard 3-attempt repair limit. The reviewer is never asked to "make
  the gate pass"; it only judges.
- The implementer runtime is never instructed to write its own skill-evidence
  ledgers or PR-12 section. Those are loop-owned state transitions.

This keeps every evidence-producing transition deterministic and auditable while
honoring the independent-verifier principle.

## Consequences

- The loop gains a code-review-verifier dispatch step (a read-only reviewer
  invocation) and a deterministic test-run + evidence-recording step, both
  before `create_pr`. `shiki_loop.py` records the `tdd` and `code-review`
  ledgers and writes the PR-12 section; the runner adapter set gains a read-only
  reviewer invocation distinct from the bypass-permissions implementer.
- Autonomous PRs now satisfy MergeGate `required_skill` and CCA PR-12 without
  manual pre-clearing, closing the gap that kept the loop from auto-merging.
- Evidence integrity rests on: the loop (not the LLM) writing the transitions,
  the reviewer's read-only confinement, the structured fail-closed verdict, and
  the audit trail — not on trusting a self-graded runner.
- CCA independence is unchanged: the pre-PR reviewer is an additional,
  independent gate, not a replacement for or input to the CCA verdict.

## Alternatives Considered

- **Runner self-attestation** (the implementer writes its own tdd/code-review
  evidence). Rejected: varying, self-graded, forgeable — violates the maxim and
  the independent-verifier finding.
- **Drop the gates for low/medium-risk autonomous tasks.** Rejected: weakens the
  quality bar and diverges from how human-driven tasks (and T1/T2/T3 of the
  Memory Loop) are judged; the gates exist precisely for unattended work.
- **Deterministic red→green verification** (run the test without the impl to
  confirm it fails, then with it to confirm it passes). Deferred as a future
  enhancement: rigorous but requires clean test/impl separation and does not
  apply to test-only or refactor tasks. The loop-observed-green record is the
  baseline; this can strengthen it later without changing the model.
