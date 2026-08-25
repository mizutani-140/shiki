# External AI Guardian UI Adapter Experiment - 2026-06-22

## Purpose

Record the first Codex App-driven experiment for monitoring Claude Code CLI
work, building review context from the Shiki repository, sending that context
to ChatGPT Pro, retrieving the response, and turning the observed process into
future skill/plugin instructions.

## Fixed Design Decisions

- Codex App is the External AI Guardian UI Adapter.
- Claude Code remains implementer/repairer and must not self-drive ChatGPT Pro
  Guardian review for its own PR.
- GPT Pro is the external Guardian Authority through
  `external_ai_guardian_review`.
- Review Packets are runtime review inputs, not approval evidence.
- GitHub connector use inside ChatGPT Pro is optional corroboration, not the
  primary context path.
- Only a validated fenced `external-ai-guardian-review` JSON artifact can be
  relayed to GitHub as approval evidence.

## Experiment Log

- 2026-06-22: GitHub issue #175 created for the Shiki-side deterministic
  contract: Packet schema, Prompt Builder, PR-type classifier, response
  verifier, CLI surface, docs, and tests.
- 2026-06-22: CuaDriver daemon was not running at experiment start. Started it
  with background launch and verified Accessibility and Screen Recording grants.
- 2026-06-22: Running apps detected: Codex, Ghostty, Google Chrome, Claude
  desktop. Candidate Ghostty Claude Code windows and a Chrome ChatGPT window
  were detected through CuaDriver window listing.

## Observations

- Ghostty window `GitHubリポジトリのPR内容を確認` is the active Claude Code
  implementation session for issue #175.
- Claude Code correctly restated the core boundary: do not implement a path
  where Claude Code self-drives ChatGPT Pro Guardian approval for its own PR;
  Codex App is the UI adapter; GPT Pro is the external Guardian Authority;
  GitHub carries the live artifact; MergeGate verifies.
- Claude Code classified the task as high-risk and broad: packet schema,
  prompt builder, classifier, response extraction/verification, CLI, docs, ADR
  0014/glossary, and tests.
- Claude Code attempted to move from a dirty/probe state to a clean
  `origin/main`-based branch for issue #175.

## Failures / Friction

- First observed friction: Claude Code attempted a cleanup/status shell command
  including `git clean` preview/cleanup for scattered `.shiki` files, but the
  command was denied by Claude Code auto-mode permissions. This is a useful
  adapter signal: Codex monitoring should distinguish implementation failure
  from CLI permission denial / local dirty tree setup failure.

## Improvements For Skillization

- The monitor should capture both screenshots and structured observations:
  current task, active branch, blocked command, whether the block is permission,
  auth, dirty tree, check failure, or Guardian/evidence failure.
- The monitor should avoid writing experiment notes into the same checkout that
  Claude Code is using for implementation unless the notes are explicitly part
  of the task; otherwise it can contaminate dirty-tree detection.

## Candidate Skill Steps

- Start CuaDriver and verify Accessibility / Screen Recording grants.
- Identify the Claude Code terminal window by title and screenshot content.
- Identify the ChatGPT Pro window separately.
- Poll the Claude Code window until it reaches a stable report or permission
  blocker.
- If blocked by local permissions/destructive cleanup, record the exact command
  class and stop for operator decision instead of approving blindly.
