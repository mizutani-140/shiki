# Shiki

Shiki is an agentic engineering control plane. It turns a Goal into planned, grilled, PRD-backed, issue-decomposed, TDD-implemented, GitHub-judged, repairable, mergeable work while preserving evidence for recovery and governance.

## Language

**Shiki**:
The platform that drives agentic engineering from Goal to Merge through planning, `grill-with-docs`, PRDs, Task DAGs, branch execution, review, validation, CCA judgment, repair loops, and evidence.
_Avoid_: prompt collection, Claude-only workflow, Codex-only workflow

**Shiki Template**:
The reusable repository structure, workflow set, command rules, schemas, and default documents installed into a Target Repository.
_Avoid_: one-off project setup

**Target Repository**:
A product or project repository that adopts Shiki by installing the Shiki Template and running Goals through GitHub and the local `.shiki/` mirror.
_Avoid_: centralizing every product inside the Shiki repo

**Goal**:
A user-approved target outcome with completion conditions, scope boundaries, and success signals. A Goal is grilled and decomposed before implementation starts.
_Avoid_: vague prompt, todo, single task

**Goal Seek**:
The clarification process that turns a user request into a Goal with outcome, non-goals, risks, completion criteria, and evidence requirements.
_Avoid_: jumping directly into code

**grill-with-docs**:
A planning skill that challenges a plan against domain language, ADRs, code reality, and concrete edge scenarios. It resolves design-tree questions before PRD/issues. It runs inside Requirements Definition.
_Avoid_: generic brainstorming, silent assumptions

**Requirements Definition**:
The single interactive phase that combines Goal Seek, grill-with-docs, Context & Impact review, and PRD drafting into one continuous operator dialogue. It ends with Spec Freeze. After Requirements Definition, no operator input is required on the happy path.
_Avoid_: separate grill and plan sessions, requirements emerging during implementation

**Spec Freeze**:
The state transition where the operator approves the PRD and the requirements become fixed. Task decomposition and implementation proceed autonomously against the frozen spec. Scope changes after Spec Freeze require an explicit, recorded Spec Amendment, never silent drift.
_Avoid_: implicit approval, re-negotiating scope mid-implementation, freezing before open design questions are resolved

**Goal Loop**:
The autonomous post-freeze driver that takes a frozen Goal's Task DAG through dispatch, completion judgment, risk-gated merge, bounded repair, and dependent unblocking without operator input. It stops only for repair-limit exhaustion, Guardian-gated risk, blocked evidence, operator-initiated Spec Amendment, or Goal completion.
_Avoid_: unbounded automation, merging without MergeGate evidence, treating the loop as the merge authority

**Spec Amendment**:
A bounded re-opening of a frozen spec when implementation reveals the spec is wrong or incomplete. It pauses affected tasks, runs a scoped re-grill of only the contested decisions with the operator, and re-stamps Spec Freeze with the amendment recorded as evidence. Only the operator approves amendments.
_Avoid_: full re-planning, unrecorded scope changes, automated self-approval, treating every discovery as an amendment

**Assumption Log**:
The recorded list of implementation-level interpretations that do not move scope boundaries, made after Spec Freeze without pausing work. Each assumption is durable evidence and is challengeable by CCA and review.
_Avoid_: hiding scope changes as assumptions, unrecorded interpretation, blocking on trivia

**Context & Impact**:
The planning intelligence that identifies relevant documents, code areas, symbols, dependencies, risks, lock candidates, and likely verification surfaces before execution.
_Avoid_: generic repo scan, unstructured research

**PRD**:
A durable product and engineering intent document created after enough Goal context has settled. It records problem, solution, user stories, implementation decisions, testing decisions, and out-of-scope boundaries.
_Avoid_: implementation scratch pad, volatile file list

**Vertical Slice**:
A narrow but complete end-to-end task that cuts through relevant layers and is independently verifiable.
_Avoid_: horizontal layer-only ticket

**Task DAG**:
A dependency graph of executable tasks derived from a Goal or PRD. Only tasks whose dependencies and locks are satisfied may run.
_Avoid_: unordered checklist, parallel execution without dependency proof

**MergeGate**:
The execution governance layer that decides whether a task, branch, or pull request can proceed, based on dependency state, file locks, required checks, CCA verdict, review status, risk level, and evidence completeness.
_Avoid_: simple CI status, human-only merge habit

**CCA**:
The GitHub-side Completion Check Agent. CCA judges whether a PR actually satisfies its task contract by evaluating acceptance criteria, diff scope, TDD evidence, checks, review state, risk, locks, and ledger evidence. CCA returns `complete`, `repair_required`, `blocked`, `needs_guardian`, or `insufficient_evidence`.
_Avoid_: implementer, casual reviewer, green-check proxy

**Ledger**:
The durable evidence record for Goals, PRDs, plans, task state, locks, branch and PR links, check results, reviews, CCA verdicts, repair packets, and merge decisions.
_Avoid_: chat memory, transient agent state

**Repair Packet**:
A bounded handoff generated when CCA, review, CI, or MergeGate rejects completion. It tells Codex exactly what failed, what to change, what not to change, and how to verify the repair.
_Avoid_: vague “fix this” request

**Repair Loop**:
The controlled retry cycle that diagnoses failed checks, CCA findings, review findings, missing evidence, or blocked dependencies, then creates a bounded follow-up task or commit.
_Avoid_: infinite retry, silent fix, broad rewrite

**Skill Gate**:
The rule that certain engineering work must invoke the relevant skills before execution, such as `grill-with-docs`, `to-prd`, `to-issues`, `triage`, `tdd`, `code-review`, `diagnose`, `zoom-out`, `improve-codebase-architecture`, or `prototype`.
_Avoid_: optional prompt style, undocumented best effort

**Agent Runtime**:
An implementation, review, judgment, or orchestration engine used by Shiki, such as Codex, Claude Code, GitHub CCA, Hermes Runner, GitHub Actions, or a future coding agent.
_Avoid_: assuming one model provider owns the platform

**Guardian**:
A human or explicitly authorized governance role for high-risk decisions and exceptions. The approving Authority may be a human or an external AI guardian reviewer; approval is judged by authority kind, scope, head SHA, evidence, and audit trail.
_Avoid_: letting automation approve critical changes silently, forging human approval

**Authority**:
The kind of approver that can grant a controlled transition: operator, repository maintainer, external AI guardian reviewer, policy engine, or verifier ensemble. Shiki is authority-in-the-loop, not human-in-the-loop. Each authority's approval is recorded under its own identity.
_Avoid_: treating "human pressed the button" as the only valid authority, recording one authority's approval under another's identity

**External AI Guardian Review**:
A first-class Guardian approval authority (ADR 0010) by which an external AI reviewer authorizes autonomous merge of a high/critical-risk PR through a head-SHA-bound `external_ai_guardian_review` artifact, recorded with the AI reviewer's own model identity (reviewer_type=external_ai_model).
_Avoid_: transforming an AI review into an operator approval, committed (forgeable) approval files, head-unbound approval

## Example Dialogue

Operator: "Create a Goal for the new intake workflow."

Shiki: "I will run `grill-with-docs` first to challenge terminology and decisions, then create a PRD and vertical-slice issues. Tasks with clean dependencies can run in parallel, but MergeGate will block tasks with unresolved locks, missing CCA evidence, or failed checks."

Operator: "Can Codex implement and Claude judge completion?"

Shiki: "Yes. Codex is the default Agent Runtime for TDD implementation and bounded repair. GitHub CCA is the default completion judge, and the Ledger records branch, PR, check, review, CCA, repair, and merge evidence."
