"""Canonical Shiki contract constants.

Keep this module dependency-free. Bootstrap, validation, and MergeGate code all
import it before target repositories have project dependencies installed.
"""

from __future__ import annotations


CANONICAL_CCA_VERDICT_SCHEMA_PATH = ".shiki/schemas/cca-verdict.schema.json"
OBSOLETE_CCA_VERDICT_SCHEMA_PATH = ".shiki/templates/cca-verdict.schema.json"

DEFAULT_REQUIRED_CHECKS = (
    "Validate Shiki mirror",
    "CCA verdict",
    "MergeGate metadata check",
    "MergeGate policy check",
)

RUNTIME_NAMES = (
    "codex",
    "codex-front",
    "claude-code",
    "claude-code-action",
    "github-cca",
    "github-actions",
    "hermes-runner",
    "human",
    "other",
)

TARGET_STATE_DIRECTORIES = (
    ".shiki/goals",
    ".shiki/plans",
    ".shiki/tasks",
    ".shiki/dag",
    ".shiki/ledger",
    ".shiki/locks",
    ".shiki/worktrees",
    ".shiki/repairs",
    ".shiki/reports",
    ".shiki/runs",
    ".shiki/inbox",
    ".shiki/handoffs",
    ".shiki/runner",
    ".shiki/smoke",
    ".shiki/starts",
)

SOURCE_OF_TRUTH_MARKER_START = "<!-- shiki-source-of-truth:start -->"
SOURCE_OF_TRUTH_MARKER_END = "<!-- shiki-source-of-truth:end -->"
CANONICAL_SOURCE_OF_TRUTH_ORDER = (
    "GitHub Issues, Pull Requests, Checks, Reviews, comments, and merge evidence are the operational source of truth.",
    "The repository-local `.shiki/` mirror records Goals, PRDs, plans, Task DAGs, contracts, locks, ledger entries, CCA verdicts, repair packets, reports, and handoffs.",
    "`CONTEXT.md` defines Shiki domain language and glossary decisions.",
    "`docs/adr/` records hard-to-reverse platform decisions.",
    "Runtime-specific wrappers such as `CLAUDE.md`, `.codex/`, `.claude/`, `.github/prompts/`, and hooks may add stricter instructions but must not weaken the shared constitution.",
)

CONTRACT_SOURCE_OF_TRUTH_FILES = (
    "AGENTS.md",
    "SYSTEM_PROMPT.md",
    "CLAUDE.md",
    ".codex/skills/shiki/SKILL.md",
    ".claude/commands/shiki.md",
    ".github/prompts/cca-completion-check.md",
    "docs/agents/implementation-policy.md",
)

CONTRACT_SCHEMA_SCAN_PATHS = (
    "AGENTS.md",
    "SYSTEM_PROMPT.md",
    "CLAUDE.md",
    ".codex",
    ".claude",
    ".github",
    "docs",
    "prompts",
    "skills/engineering",
)


def canonical_source_of_truth_markdown() -> str:
    lines = [SOURCE_OF_TRUTH_MARKER_START]
    lines.extend(f"{index}. {item}" for index, item in enumerate(CANONICAL_SOURCE_OF_TRUTH_ORDER, start=1))
    lines.append(SOURCE_OF_TRUTH_MARKER_END)
    return "\n".join(lines)
