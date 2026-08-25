---
name: shiki-external-ai-guardian-bridge
description: Bridges Shiki Guardian-gated PRs between Claude Code CLI, Codex App side-panel terminal, GitHub evidence, and ChatGPT Pro/GPT-5.5 Pro external AI review. Use when Shiki PRs need external_ai_guardian_review, GPT Pro chat-based review, cross-platform context relay, codex-plugin-cc/Claude Code handoff, side-panel terminal handoff, parallel ChatGPT PR review requests, or CCA needs_guardian closeout.
---

# Shiki External AI Guardian Bridge

## Purpose
Use Codex App as the bridge for Shiki Guardian gates where Claude Code owns implementation and ChatGPT Pro/GPT-5.5 Pro provides external AI Guardian review. Codex observes, packages evidence, drives UI automation, verifies the returned artifact, and hands the result back to GitHub/Shiki.

## Authority Model

- Treat ChatGPT Pro/GPT-5.5 Pro as an external AI Guardian authority only through a structured `external_ai_guardian_review` artifact.
- A verified GPT Pro approval artifact is merge authorization for the external-AI Guardian path. It is not merely advisory evidence for another reviewer to reinterpret.
- Never convert GPT Pro output into human/operator approval.
- Do not add `guardian:approved`, do not write "Guardian approval granted", and do not impersonate a human reviewer.
- Use `guardian:external-ai-approved` for the UI/query label when the repository wants a label with Guardian-equivalent merge authority for this path. This label means "external AI Guardian approved"; it must never mean human Guardian approval.
- `guardian:external-ai-approved` is valid only with a current-head verified `external_ai_guardian_review` artifact. The label by itself is not approval evidence; stale, missing, or mismatched artifacts invalidate the label.
- The artifact must bind at least: `repo`, PR number, current head SHA, model, reviewer role such as `external_guardian_reviewer`, decision, evidence, and any blocking findings.
- If the artifact is missing, mismatched, non-final, or unverifiable, route to repair/evidence collection instead of treating it as approval.
- When the verifier reports `approved: true` with `route: autonomous_merge`, the correct next state is merge closeout for that PR, in dependency order. Do not turn later metadata-only check failures into implementation repair unless they show that the approval artifact is stale/missing/mismatched or that GPT identified an unresolved blocker.

## Context Relay Model

The bridge exists to preserve context across four platforms without making any side panel the source of truth:

- Claude Code CLI is the high-throughput implementer and repairer, often using `/effort Ultracode`.
- GitHub plus `.shiki/` is the durable authority for issue, PR, diff, checks, task, ledger, CCA, and repair state.
- Codex App is the orchestration and UI bridge that can read repository/GitHub evidence, read terminal output, operate Chrome/ChatGPT with Computer Use, and package context.
- ChatGPT Pro/GPT-5.5 Pro chat is the external review surface when API cost or availability makes chat the required route.

Use one active execution handoff path at a time. When Claude Code is the implementer, send the executable repair instruction through `codex-plugin-cc` if it is installed and documented for the environment, or through a Claude Code paste/CLI handoff if not. Mirror the same capsule to GitHub/Shiki as evidence and recovery state, not as a competing instruction route.

Every cross-platform handoff must be a head-bound context capsule containing: repo, PR number, current head SHA, branch, task/goal id, acceptance criteria, authoritative GitHub/check/CCA state, relevant `.shiki` evidence, scoped diff summary, Claude Code `/copy` or terminal transcript excerpt when available, GPT review/verifier result when available, next owner runtime, prohibited changes, verification commands, and stale-head invalidation rule.

Treat GitHub diff, checks, CCA, and `.shiki` evidence as authoritative. Treat Claude Code `/copy` output, terminal logs, and ChatGPT prose as supplemental runtime context unless verified and bound to the current head. If supplemental context conflicts with GitHub evidence, surface the conflict and stop before issuing repair instructions.

After a verified external-AI approval, distinguish authority from bookkeeping:

- Code/test/security/CCA failures with current-head evidence are real blockers and must become repair work.
- Missing, stale, mismatched, or unverifiable external-AI approval is a review blocker and must return to GPT review or evidence collection. Remove or ignore any `guardian:external-ai-approved` label while in this state.
- Deterministic `.shiki` mirror noise is not automatically an implementation blocker after GPT approval. Examples include old `expected_branch`, `expected_pr: null`, task status still `ready`, missing skill-ledger references, slice-stack ancestor files in the diff, lock lists that describe generated planning artifacts poorly, or stale setup/run ledgers. Treat these as merge-closeout bookkeeping unless they contradict the approved PR head or hide an unreviewed code change.
- Do not hand metadata-only red checks to Claude Code as a source repair packet. If branch protection blocks the merge on metadata-only checks, use the operator-approved Guardian/external-AI merge path: record the bypass rationale, merge in dependency order, and open or defer a separate metadata closure task after the stack lands.

Do not assume `codex-plugin-cc` exists or can move context into Codex App. Verify the plugin/tool docs and observed behavior before relying on it. A valid adapter must transfer the context capsule into a Codex App thread or Codex-compatible review turn, preserve the returned review capsule, and make that result available to Claude Code without dropping head SHA, PR number, or verifier status.

## Side-Panel Terminal Relay

Prefer the Codex App side-panel integrated terminal, or the operator's normal terminal, as the live Claude Code execution surface. Use this path when Claude Code needs `/login`, subscription-authenticated `/effort Ultracode`, long-running implementation, `/copy`, or local state under `~/.claude`.

Do not rely on Codex tool-sandbox `exec_command` as the primary Claude Code runtime when authentication or session continuity matters. It may not share the same writable home/session environment as the side-panel terminal, and can show API-billing mode or fail to write Claude session files even when the operator's Claude Code terminal is authenticated.

Codex App's role around the side-panel terminal is observation and capsule packaging:

1. Read the current terminal output when available.
2. Ask for or collect Claude Code `/copy` after implementation or repair.
3. Reconcile that runtime context against GitHub PR diff, checks, CCA, and `.shiki` evidence.
4. Build a head-bound context capsule for ChatGPT Pro/GPT-5.5 Pro review.
5. Feed the resulting repair capsule back to the same live Claude Code relay.

The side-panel terminal may be the fastest live path, but it is not durable truth. Mirror each capsule's key facts to the batch state file and GitHub/Shiki recovery surfaces: PR number, head SHA, Claude session/source, GPT review file, verifier file, next owner, and whether the handoff was delivered through `codex-plugin-cc`, side-panel paste, or PR comment fallback.

If Codex cannot directly type into the side-panel terminal, generate an exact paste-ready Claude Code prompt and ask the operator to paste it. Do not silently switch to PR-comment-first handoff while a live Claude Code terminal is the intended execution path.

## Start

1. Confirm the target repo, PR number, task/goal id if present, current PR head
   SHA, and whether Claude Code or Shiki loop is still running.
2. Read Shiki source-of-truth surfaces before acting: PR/issue/checks/comments,
   `.shiki/` evidence, `CONTEXT.md`, relevant ADRs, and guardian policy docs.
3. Freeze implementation scope when the only blocker is Guardian approval,
   metadata, CCA rerun, or closeout reconciliation. Do not edit code unless a
   verified finding requires a bounded repair.
4. Create or refresh the Guardian packet and prompt with repository evidence, not with a vague instruction to "read the repo".

Useful commands:

```bash
bin/shiki guardian packet --pr <PR> --out /tmp/pr-<PR>-guardian-packet.json
bin/shiki guardian prompt --packet /tmp/pr-<PR>-guardian-packet.json --out /tmp/pr-<PR>-gpt-pro-review-prompt.txt
bin/shiki guardian verify-response --response /tmp/pr-<PR>-gpt-pro-response.md --packet /tmp/pr-<PR>-guardian-packet.json --out /tmp/pr-<PR>-verify.json
```

## Batch PR Reviews

- When several independent PRs need the same external Guardian pass, prepare packets and prompts for every PR first, then submit the ChatGPT review requests in parallel instead of waiting for one PR to finish before starting the next.
- Keep each PR in its own ChatGPT conversation/tab and its own packet, prompt, raw response, verifier output, and state-file entry. Do not ask one ChatGPT message to approve or reject multiple PRs unless the task explicitly requires a cross-PR comparison.
- Treat parallelism as request scheduling only. Verification, PR comments, repair packets, and approval artifacts remain PR-scoped and must be processed against that PR's current head SHA.
- Track per-PR status in a compact batch state file such as `/tmp/shiki-guardian-bridge-state.json`: `queued`, `submitted`, `ready_to_copy`, `verified`, `commented`, `stale_head`, or `blocked`.
- After all PR-scoped artifacts verify, do not re-serialize unnecessary review work. Switch to dependency-order merge closeout: re-check each PR head, confirm its verified artifact is still head-bound, then merge the next dependency.
- If ChatGPT rate limits, tab instability, or attachment upload failures appear, reduce concurrency and continue as a bounded queue. Preserve already-submitted PR tabs and do not mix responses between PRs.

## UI Automation

- Use Computer Use/Chrome automation because this workflow is explicitly an autonomous UI bridge.
- Do not click ChatGPT's instant-answer button, including the Japanese UI label "今すぐ回答".
- Long pasted prompts may become text attachments; that is acceptable. Add a short visible message if needed to enable send.
- Wait for the executing LLM's final output. Advancing from partial streaming output is a hard failure mode.
- Use `/copy` or the UI copy control only after the final response is complete, then save the copied response to a local file.
- For batch reviews, open all PR conversations first, submit all prompts, and then revisit completed tabs as they become ready. Avoid holding the active browser on one streaming response when other PRs can already be running.
- If Ghostty background Return does not submit, foreground Ghostty and perform the minimum action required. Record the transport quirk for later skill/hook improvement.

## Poll Cadence

- Claude Code implementation or repair running: check every 3-5 minutes.
- ChatGPT Pro/GPT-5.5 Pro deep reasoning: prefer browser/OS notification or tab-ready signal, then inspect the completed tab. If notifications are unavailable, check every 3-5 minutes instead of watching continuously.
- Batch ChatGPT reviews: after submitting all tabs, leave them running in parallel and return on notification. If no notification arrives by the expected review window, do one summarized sweep across tabs, then back off again.
- Finalizing or streaming near completion: check every 30-60 seconds.
- GitHub Actions: poll summarized status every 2-3 minutes; avoid continuous
  watches unless finalizing.
- If there is no visible progress for about 20 minutes, inspect UI status and logs. Do not use instant-answer as a shortcut.
- After a notification or ready signal, copy and verify that PR immediately, then continue monitoring the remaining submitted PRs.

## Review Prompt Requirements

The ChatGPT prompt should include:

- Repository, PR number, current head SHA, base SHA, issue/task links, and risk level.
- PR summary, diff-focused implementation notes, changed files, public API and package/export changes, tests, docs, snapshots, CI status, and known risks.
- Claude Code `/copy` transcript or terminal summary only as supplemental implementation intent/runtime context, clearly marked lower authority than GitHub diff/checks.
- AI implementation risks: local-only fixes, responsibility-boundary breaks, duplicate utilities, unsafe casts, implementation-coupled tests, unjustified snapshots, public API drift, missing docs/examples, Node/browser/bundler gaps, and maintainability.
- Required output sections: PR Summary, AI-specific Risk Assessment, Blocking Issues, Non-blocking Suggestions, Missing Tests, Architecture Impact, Final Review Decision, and a GitHub-postable review comment.
- Explicit instruction to emit a structured `external_ai_guardian_review` artifact when the final decision is approval.

## Claude Code Handoff

When GPT/Codex produces repair findings for a Claude Code-owned PR, build a Claude Code handoff capsule instead of relying on a side-panel memory trail:

```text
/effort Ultracode

Use the attached Shiki context capsule for PR <PR> at head <SHA>.
Implement only the blocking repair items in this capsule.
Do not broaden scope, reorder stacked PRs, rewrite unrelated files, or treat GPT output as human Guardian approval.
Run the listed verification commands.
Return a /copy summary with changed files, commands/results, remaining blockers, and the new head SHA.
```

If `codex-plugin-cc` is available, use it to move this capsule into Claude Code and return Claude's `/copy` summary into the Codex review thread. If it is unavailable or unproven, use the paste/CLI handoff explicitly and record that the plugin path was skipped.

## Verification And Handoff

1. Save prompt, packet, raw response, verifier JSON, comment URL, and next action paths in a compact state file such as `/tmp/shiki-guardian-bridge-state.json`. For batch runs, store these fields per PR and include the ChatGPT tab/conversation locator when available.
2. Run `bin/shiki guardian verify-response`.
3. Accept only a verifier result whose artifact matches repo, PR, current head SHA, model, role, and approval route.
4. If and only if the verifier reports `approved: true` and `route: autonomous_merge`, post the verified artifact as a PR comment, add `guardian:external-ai-approved` when that label exists or can be created safely, and mark that PR `approved_for_merge` in the bridge state. The artifact plus this external-AI label is the Guardian-equivalent merge authority for the external-AI path.
5. If the verifier reports `request_changes`, `insufficient_evidence`, `rejected`, `blocked`, or no valid artifact, do not stop after local verification. Convert the result into a bounded repair/evidence capsule with repo, PR, current head SHA, verifier JSON summary, blocking findings, required repair, prohibited changes, and the next rerun instruction. Hand it back through the active Claude Code relay first (`codex-plugin-cc` when proven, otherwise Claude Code paste/CLI handoff) and mirror it to GitHub/Shiki as evidence or recovery state. If no live Claude Code runtime owns the work, use a PR comment addressed to `@claude` as the durable handoff. Do not post a Guardian approval artifact, do not add `guardian:approved`, and do not rerun CCA as if approval exists.
6. Rerun the required `pull_request` CCA/check-run only when the rerun can change merge readiness. If approval is already verified and the remaining failures are known metadata-only `.shiki` bookkeeping, do not spend time rerunning or repairing them; proceed to merge closeout or record the required bypass.
7. Let Claude Code/Shiki loop finish source repair when it owns the session. For verified external-AI approvals, Codex may own merge closeout if the operator asked to proceed to MergeGate/merge.
8. Wait for Claude Code's final report only when Claude Code is actively implementing or repairing. Do not wait on Claude Code for a pure external-AI-approved merge closeout.

## Merge Closeout After GPT Approval

Use this path when GPT Pro/GPT-5.5 Pro approval artifacts are verified for the current PR heads.

1. Re-read PR heads and dependency order from GitHub immediately before merging.
2. For each PR in dependency order, confirm the posted `external-ai-guardian-review` artifact matches the current head SHA and says `decision:"approve"` plus `merge_permission:"autonomous_merge_permitted"`. If `guardian:external-ai-approved` exists in the repository, add or confirm it for that same current head.
3. Confirm there are no current code/test/security failures or GPT blocking findings. Treat CCA success as useful evidence, but do not require deterministic `.shiki` metadata checks to become green when the only red items are bookkeeping noise already covered by the approved artifact.
4. If normal `gh pr merge` succeeds, continue to the next dependency.
5. If branch protection blocks only metadata-only `.shiki` checks, do not create an implementation repair packet. Record a short bypass rationale that names the verified GPT approval artifact, the current head SHA, and the metadata-only failure class, then use the operator-approved Guardian/external-AI merge path available for that repository. If no bypass mechanism is available in the current credentials, stop with exactly that blocker and the command that failed.
6. After the stack lands, create a separate metadata closure issue/PR only if the repository needs `.shiki` mirror cleanup on `main`. Do not mutate already approved implementation PRs solely to appease stale planning metadata.

Metadata-only failure signatures that should not trigger source repair after GPT approval:

- `expected_branch` or `expected_pr` stale relative to the actual PR.
- task status still `ready` even though the PR has verified external approval.
- `ledger_evidence` missing historical `tdd` or `code-review` entries when tests/review evidence is present in GitHub and GPT approved the current head.
- slice-stacked ancestor `.shiki/tasks`, `.shiki/ledger`, `.shiki/runs`, `.shiki/plans`, `.shiki/worktrees`, or lock files appearing in a descendant PR diff.
- generated Goal/Plan/DAG/bootstrap mirror files that are wider than the current slice but do not change runtime behavior.

Do not describe this path as "bypassing review." The review authority is the verified GPT approval artifact. The bypass, if needed, is only around stale deterministic bookkeeping that failed to model the approved stack state.

## External AI Approval Label

Use `guardian:external-ai-approved` when a GitHub label is useful for MergeGate, PR lists, dashboards, or human scanning.

- It is allowed to have Guardian-equivalent merge authority for external AI approvals.
- It must be bound to a valid current-head `external_ai_guardian_review` artifact posted on the PR.
- It must not be used as a replacement for the artifact, because labels do not carry reviewer model, role, PR number, head SHA, verdict, or `not_operator_approval`.
- It is distinct from `guardian:approved`. Never use `guardian:approved` for GPT Pro approval unless the operator separately gives human Guardian approval.
- If the PR head changes, remove/ignore `guardian:external-ai-approved` until a fresh current-head artifact verifies.
- If policy code treats labels as approval sources, it must validate `guardian:external-ai-approved` through the artifact parser, not through label presence alone.

## Closeout Notes

Record lessons learned while running the bridge: failed UI assumptions, cadence adjustments, verifier errors, prompt weaknesses, check-state ambiguity, and any manual fallback. These notes are the raw material for future hooks, skills, or runner automation.

## Failure Lessons

- If GPT Pro has already produced a verified current-head approval with autonomous merge permission, treating a later metadata-only MergeGate red as an implementation blocker is a routing bug. Classify the red check first. If it is stale `.shiki` bookkeeping, proceed to merge closeout or an operator-approved metadata bypass, not Claude repair.
- Before posting a repair packet, ask: "Would this change the PR code or only rewrite planning metadata to satisfy a checker?" If the answer is only planning metadata and GPT approval is current, do not post the repair packet.
- If an incorrect repair packet was posted for a GPT-approved PR, immediately supersede it on the PR with a correction that says it is withdrawn and that the PR should continue through GPT-approved merge closeout.
- If the repository expects a label for external-AI approvals, use `guardian:external-ai-approved`, not `guardian:approved`. Missing this label is a closeout/bookkeeping gap, not a reason to reinterpret GPT approval as human approval.
- A GPT Pro `request_changes` verdict is not a completed bridge task until the owning implementer/runtime has received the repair packet on a durable surface. Saving `/tmp` artifacts and reporting only to the operator is insufficient because Claude Code cannot act on it.
- When ChatGPT produces a non-approval verdict, keep the raw response and verifier JSON, then post a concise repair packet to the PR. Include enough detail for Claude Code to implement without reading the local `/tmp` files.
- A non-approval response may contain no fenced artifact, so `verify-response` can report `artifact_present: false` and no structured `blocking_issues` even when the raw response has actionable findings. In that case, treat the verifier as the authority for non-approval and extract the bounded repair details from the saved raw response before building the Claude Code repair capsule.
- If a non-approval finding depends on current upstream docs or vendor behavior, verify the cited primary docs before durable handoff, then include the precise doc boundary in the repair packet instead of only relaying the model's prose.
- After any repair commit changes the PR head, the previous external review packet and response are stale. Regenerate the packet and prompt for the new head before asking GPT Pro again.
- If Claude Code is the owning implementer, the bridge is not allowed to stop between verifier failure and Claude handoff. Deliver the repair capsule through the active Claude Code relay first when live, then mirror enough evidence to GitHub/Shiki for recovery and audit.
- If a Claude repair runner hangs after producing a scoped diff, inspect the registered worktree, run the required verification there, interrupt only the stuck runner, and either commit/push the verified bounded repair or report the blocker. Then regenerate the head-bound Guardian packet before rerunning GPT Pro.
