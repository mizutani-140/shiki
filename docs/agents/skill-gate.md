# Skill Gate

Skill Gate is the rule that Shiki work must invoke the relevant engineering skill before execution when the trigger applies.

Skills are execution controls, not writing style preferences. Missing a required skill is a MergeGate risk and may block readiness.

## Required Skills

| Skill | Trigger | Expected output |
| --- | --- | --- |
| `setup-matt-pocock-skills` | First configuring a repo for these skills, or when issue tracker, triage labels, or domain docs are missing. | `docs/agents/` configuration and an `Agent skills` block in `AGENTS.md` or `CLAUDE.md`. |
| `grill-with-docs` | Ambiguous plan, unclear terminology, boundary decision, tradeoff, or ADR-worthy design choice. | Clarified decision tree, recommended answers, updates to `CONTEXT.md` or ADRs when decisions crystallize. |
| `zoom-out` | Agent lacks a higher-level architectural map. | Map of relevant modules, callers, seams, and domain concepts. |
| `to-prd` | Settled context should become a PRD. | PRD published to the issue tracker with problem, solution, user stories, decisions, testing, and out-of-scope notes. |
| `to-issues` | Goal, PRD, or plan must become implementation tickets. | Vertical-slice issues with acceptance criteria, dependencies, and AFK/HITL classification. |
| `triage` | Issue readiness, labels, workflow state, or AFK-agent preparation is needed. | Triage recommendation, labels, notes, or agent brief. |
| `tdd` | Feature work or bug fix has observable behavior that can be specified with tests. | Red-green-refactor loop through public interfaces, one vertical slice at a time. |
| `code-review` | The TDD loop is green and the implementer is about to create or update a PR. Default for every implementation task. | Pre-PR self-review of the task diff with fixes applied on the task branch; findings and resolutions recorded as a ledger entry naming the skill and a `## Pre-PR code review` PR body section. |
| `diagnose` | Hard bug, failing check, regression, flaky behavior, or performance problem. | Reproduction loop, ranked hypotheses, instrumentation, fix, regression test, post-mortem. |
| `improve-codebase-architecture` | Structural friction, testability, AI-navigability, deep-module opportunity, or refactor request. | Architecture candidates and deepening recommendations informed by `CONTEXT.md` and ADRs. |
| `prototype` | A throwaway logic or UI prototype is needed to answer a design question. | Clearly marked disposable prototype and captured decision. |

## MergeGate Interaction

A task is not MergeGate-ready when a required skill was skipped without explanation.

When a skill applies, record:

- Skill name.
- Why it applies.
- What artifact or decision it produced.
- Where the evidence is stored.
- Any unresolved question or follow-up task.

## Default Skill Selection

Use this quick map:

- "What are we building?" -> `grill-with-docs`, then `to-prd` if settled.
- "Break this down" -> `to-issues`.
- "Can an agent pick this up?" -> `triage`.
- "I do not understand this code area" -> `zoom-out`.
- "Build/fix behavior" -> `tdd`.
- "Ready to open the PR" -> `code-review` first.
- "Something is failing" -> `diagnose`.
- "This architecture is hard to work with" -> `improve-codebase-architecture`.
- "Let me try a few options" -> `prototype`.
- "The skills do not know the tracker/docs/labels" -> `setup-matt-pocock-skills`.
