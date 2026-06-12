# ADR 0008: Use Claude Code As The Default Implementer Runtime

## Status

Proposed (supersedes ADR 0005 for the implementer default; keeps its auth model)

## Context

ADR 0005 made Codex Front the default implementation runtime, with Claude Code
restricted to planner and reviewer roles by the runtime registry, and the only
autonomous implementation adapter was `shiki runner codex`.

Operational reality diverged from this contract:

- Direct `codex exec` dispatch is rejected by the operator's harness, so
  automated repair dispatch never worked in practice. The practical repair path
  became Guardian-approved direct Claude assignment.
- The platform's planning, review, and CCA layers already run on Claude Code,
  and Claude Code's native capabilities (the built-in `/code-review` skill and
  the Workflow multi-agent orchestration tool) have no Codex equivalent.
- The Goal lifecycle redesign (ADR 0009) requires an autonomous post-freeze
  loop driven from the operator's Claude Code session, including bounded
  repair dispatch, which must not depend on a runtime that cannot be invoked
  unattended.

The constitution claims Shiki is runtime-agnostic ("not a Claude-only
workflow", ADR 0002). Any change to the implementer default must preserve the
registry-based runtime model rather than hardcoding one vendor.

## Decision

We will make `claude-code` the default implementer runtime:

- Grant `claude-code` the `implementer` and `runner` roles in the runtime
  registry, and set it as the default `implementer` in `.shiki/config.yaml`.
- Add a `shiki runner claude` adapter, symmetric to `shiki runner codex`: it
  materializes the registered worktree, pipes the task or repair handoff into
  a headless `claude -p` session, records EXEC and ledger evidence, and moves
  the task to review or repair-needed.
- Keep Codex registered as an optional implementer runtime. `shiki runner
  codex`, the Codex handoff templates, and the ChatGPT OAuth auth model from
  ADR 0005 remain supported for tasks explicitly assigned to `codex`.
- Commit to mandating two Claude-native capabilities as loop contract in
  follow-up Goals (status: the code-review gate was delivered by Goal G-B —
  skill registry, checklists item PR-12, default required_skills — while the
  Context & Impact sweep remains pending for Goal G-C):
  - the implementer will run the `code-review` skill as a pre-PR self-review
    gate (TDD, then code review with fixes applied, then PR), recorded in the
    ledger and the PR body and judged by a new CCA checklist item — to be
    introduced together with the skill-gate registry entry (Goal G-B of the
    0.2.0 series, PRD issue #119);
  - Context & Impact for non-trivial Goals will be produced by a Workflow-tool
    parallel exploration sweep, with the run recorded as evidence (Goal G-C).
  The constitution will state these as required capabilities whose default
  implementation is Claude Code native tooling; a future runtime may satisfy
  them with equivalent evidence.
- File-mutating subagent worktrees are allowed only inside the task's
  registered worktree. Read-only fan-out is unrestricted. Unregistered
  worktree creation remains a non-negotiable block.

Out of scope: removing Codex support, changing the CCA runtime (Claude Code
Action with `CLAUDE_CODE_OAUTH_TOKEN` stays), and API-key based automation
(still requires its own ADR per ADR 0005).

## Consequences

- Autonomous implementation and repair dispatch become operational on the
  operator's existing Claude subscription; the broken `codex exec` automated
  path stops being load-bearing.
- The runtime registry, `.shiki/config.yaml`, `validate_shiki.py` role
  validation, AGENTS.md default assignment, CLAUDE.md direct-edit rules,
  runtime-auth-model.md, and the skill gate registry must change together.
- CLAUDE.md's restriction that Claude edits product source only under explicit
  assignment now has a standing satisfier: tasks dispatched through
  `shiki runner claude` carry that assignment and its evidence.
- One vendor now powers planning, implementation, review, and CCA. The
  role-separation maxim still holds at the process level (separate sessions,
  separate evidence, CCA never implements), but model-diversity in judgment is
  reduced. Codex remains available as an independent implementation lens.
- Implementer-run pre-PR `code-review --fix` is compatible with the rule that
  reviewers do not mutate the implementation branch, because it runs in the
  Implementer role before review; the Reviewer and CCA roles remain
  mutation-free.
- The headless dispatch runs `claude -p --permission-mode bypassPermissions`:
  a non-interactive session needs standing permissions, so Claude Code's
  per-action permission prompts are disabled inside the dispatched session.
  This is an accepted risk, bounded by: dispatch only through the runner for a
  registered, MergeGate-gated task; a hard refusal to execute when the
  registered worktree resolves to the target checkout itself; Guardian
  approval for high/critical risk; and MergeGate remaining the only authority
  for state transitions ("LLM outputs may vary. State transitions must not
  vary."). A tighter posture (settings-based deny rules and hooks shipped into
  the dispatch worktree) was deferred past the 0.2.0 series: Goal G-D shipped
  the autonomous loop without it, and the bounding work remains an open,
  recorded follow-up for the next series (tracked in PRD issue #119).

## Alternatives Considered

- **Keep Codex as default implementer and fix dispatch**: rejected; the
  harness rejection of direct `codex exec` is outside Shiki's control, and the
  plan's mandatory native capabilities cannot run on Codex at all.
- **Remove Codex entirely**: rejected; it abandons the runtime-agnostic
  constitution (ADR 0002) for no operational gain and forecloses
  cross-runtime adversarial review.
- **GitHub Actions cloud implementation via claude-code-action**: rejected as
  default; durable evidence is automatic but token cost, Node 24 workflow
  constraints, and divergence from the operator-local Goal experience are too
  high. It remains a possible future extension.
