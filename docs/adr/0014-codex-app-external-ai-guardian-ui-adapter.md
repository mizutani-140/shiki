# ADR 0014: Use Codex App As The External AI Guardian UI Adapter

## Status

Proposed

## Context

ADR 0010 admits `external_ai_guardian_review` as a first-class Guardian
approval authority. The next design problem is how to obtain that external AI
Guardian review when the reviewer is reached through a ChatGPT Pro UI rather
than a deterministic GitHub Action or API.

The external reviewer must receive enough context to judge a high-risk PR:
task contract, PR diff, checks, implementer report, relevant Shiki docs, and
current head SHA. Asking the reviewer to discover the repository from scratch
through the GitHub connector is useful as an independent verification path, but
it is not stable enough to be the primary input path. Connector search indexing,
tool availability, and model tool-use choices can vary.

Claude Code is the default implementer and repairer runtime. Letting the same
runtime operate the external Guardian UI would blur the boundary between
implementation and approval routing. Shiki needs an autonomous path, but it
also needs identity honesty and deterministic state transitions.

## Decision

We will make the Codex App-side coordinator the External AI Guardian UI Adapter.

The adapter will:

- monitor Claude Code implementer output and `/copy` Full Response material;
- inspect GitHub and/or the local repository for PR, diff, check, task, and
  `.shiki` evidence;
- generate an External AI Guardian Review Packet as runtime input evidence;
- generate a PR-specific prompt with implementation-sensitive review focus
  areas;
- drive the ChatGPT Pro review UI with Computer Use / Browser Use;
- retrieve the full reviewer response;
- parse `approve`, `request_changes`, or `insufficient_evidence`;
- validate any fenced `external-ai-guardian-review` JSON artifact against repo,
  PR, head SHA, reviewer model/role, verdict, merge permission, and
  `not_operator_approval:true`;
- relay only the validated approval artifact to GitHub as the live PR comment
  consumed by Guardian policy; and
- route non-approval verdicts back into Shiki as bounded repair or evidence
  packets.

The External AI Guardian Review Packet is not itself an approval artifact. It
must not be committed by the PR being reviewed. If Shiki records packet
evidence, it records provenance such as source list, PR number, head SHA, and a
packet digest, not the packet as trusted approval.

The ChatGPT Pro reviewer is the approval Authority. The Codex App adapter is a
transport and validation runtime. Claude Code remains the implementer or
repairer and must not operate this Guardian UI path for its own implementation
work.

Human Guardian approval paths remain available as fallback. The external AI
path remains distinct from human approval and must never be recorded as operator
approval.

Out of scope: replacing MergeGate, treating ChatGPT UI output as trusted without
artifact validation, requiring the GitHub connector as the primary context path,
committing review packets as approval evidence, or giving Claude Code
implementer sessions the authority to self-route Guardian UI approval.

## Consequences

- External AI Guardian review can run end-to-end through the operator's Codex
  App and ChatGPT Pro session without forcing human approval into the critical
  path.
- Review inputs become reproducible: Codex builds the packet from durable PR,
  check, task, and repository evidence before the external model judges it.
- The GitHub connector remains useful for reviewer-side challenge and
  corroboration, but connector instability cannot decide whether the reviewer
  received adequate baseline context.
- The implementation/approval boundary is clearer: Claude Code implements,
  Codex App transports and validates, GPT Pro judges, GitHub carries the live
  artifact, and MergeGate verifies.
- The adapter must treat ChatGPT UI automation as an environment-specific
  runtime integration. UI transcripts and copied responses are not source of
  truth until parsed into a valid head-bound artifact or a bounded
  repair/evidence packet.
- Future work should add schemas and CLI support for packet generation,
  prompt building, verdict extraction, artifact validation, and adapter
  evidence digests before broad target-repository rollout.

## Alternatives Considered

- **Let Claude Code operate the ChatGPT Pro UI**: rejected because Claude Code
  is the default implementer and repairer. Letting it drive its own external
  Guardian path weakens role separation.
- **Ask GPT Pro to read the repository only through the GitHub connector**:
  rejected as the primary path because connector search/index/tool-use behavior
  can vary. It remains acceptable as a secondary verification path.
- **Keep the process manual**: rejected because the framework goal is to test
  how far autonomous implementation and governance can proceed without human
  intervention.
- **Commit the review packet in the PR**: rejected because PR-authored review
  input can be shaped by the implementation branch and must not become trusted
  approval evidence.
