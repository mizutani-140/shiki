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
A planning skill that challenges a plan against domain language, ADRs, code reality, and concrete edge scenarios. It resolves design-tree questions before PRD/issues.
_Avoid_: generic brainstorming, silent assumptions

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
The rule that certain engineering work must invoke the relevant skills before execution, such as `grill-with-docs`, `to-prd`, `to-issues`, `triage`, `tdd`, `diagnose`, `zoom-out`, `improve-codebase-architecture`, or `prototype`.
_Avoid_: optional prompt style, undocumented best effort

**Agent Runtime**:
An implementation, review, judgment, or orchestration engine used by Shiki, such as Codex, Claude Code, GitHub CCA, Hermes Runner, GitHub Actions, or a future coding agent.
_Avoid_: assuming one model provider owns the platform

**Guardian**:
A human or explicitly authorized governance role for high-risk decisions and exceptions.
_Avoid_: letting automation approve critical changes silently

## Example Dialogue

Operator: "Create a Goal for the new intake workflow."

Shiki: "I will run `grill-with-docs` first to challenge terminology and decisions, then create a PRD and vertical-slice issues. Tasks with clean dependencies can run in parallel, but MergeGate will block tasks with unresolved locks, missing CCA evidence, or failed checks."

Operator: "Can Codex implement and Claude judge completion?"

Shiki: "Yes. Codex is the default Agent Runtime for TDD implementation and bounded repair. GitHub CCA is the default completion judge, and the Ledger records branch, PR, check, review, CCA, repair, and merge evidence."
