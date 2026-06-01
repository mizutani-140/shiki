---
name: shiki
description: Use when editing Shiki control-plane artifacts, task contracts, ledger evidence, CCA, MergeGate, runtime orchestration, or repository-local .shiki mirror state.
---

Read `.shiki/config.yaml`, the relevant Goal, Task, DAG, ledger entries, and applicable ADRs before editing.

Preserve GitHub-first source-of-truth semantics. Keep edits within the assigned task contract. Do not broaden locks, alter historical evidence without a repair ledger entry, or bypass CCA/MergeGate.
