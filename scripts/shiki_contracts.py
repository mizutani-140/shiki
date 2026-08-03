"""Canonical Shiki contract constants.

Keep this module dependency-free. Bootstrap, validation, and MergeGate code all
import it before target repositories have project dependencies installed.
"""

from __future__ import annotations

import json
from pathlib import Path

from shiki_runtime_registry import runtime_names


CANONICAL_CCA_VERDICT_SCHEMA_PATH = ".shiki/schemas/cca-verdict.schema.json"
OBSOLETE_CCA_VERDICT_SCHEMA_PATH = ".shiki/templates/cca-verdict.schema.json"
CANONICAL_REPAIR_PACKET_SCHEMA_PATH = ".shiki/schemas/repair-packet.schema.json"
OBSOLETE_REPAIR_PACKET_SCHEMA_PATH = ".shiki/templates/repair-packet.schema.json"
CANONICAL_EXTERNAL_AI_GUARDIAN_REVIEW_PACKET_SCHEMA_PATH = ".shiki/schemas/external-ai-guardian-review-packet.schema.json"

DEFAULT_REQUIRED_CHECKS = (
    "Validate Shiki mirror",
    "CCA verdict",
    "MergeGate metadata check",
    "MergeGate policy check",
)

CODEOWNERS_PATH = ".github/CODEOWNERS"
# Default required CODEOWNERS owner for this repository. Kept as the fallback so
# this platform repo's behaviour is unchanged; a target resolves its own owner
# from ``.shiki/repo.json`` via ``codeowners_required_owner`` so a repository
# owned by anyone else can satisfy its own CODEOWNERS check.
CODEOWNERS_REQUIRED_OWNER = "@mizutani-140"


def codeowners_required_owner(target: Path | str = ".") -> str:
    """Resolve the required CODEOWNERS owner for ``target``.

    A target records its GitHub repository as ``OWNER/NAME`` in
    ``.shiki/repo.json``; the required owner is ``@OWNER``. When no repo config is
    present (as in this platform repository), fall back to the documented default
    so existing behaviour is unchanged.
    """
    repo_json = Path(target) / ".shiki" / "repo.json"
    try:
        data = json.loads(repo_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CODEOWNERS_REQUIRED_OWNER
    repo = data.get("repo") if isinstance(data, dict) else None
    if isinstance(repo, str) and "/" in repo:
        owner = repo.split("/", 1)[0].strip()
        if owner:
            return owner if owner.startswith("@") else f"@{owner}"
    return CODEOWNERS_REQUIRED_OWNER
CODEOWNERS_CRITICAL_PATHS = (
    "/.shiki/config.yaml",
    "/.shiki/guardian-policy.json",
    "/.shiki/policy.example.yaml",
    "/.github/CODEOWNERS",
    "/.github/pull_request_template.md",
    "/.github/PULL_REQUEST_TEMPLATE/*",
    "/.github/prompts/*",
    "/.github/workflows/*",
    "/scripts/shiki.py",
    "/scripts/mergegate_check.py",
    "/scripts/enforce_cca_verdict.py",
    "/scripts/validate_shiki.py",
    "/scripts/shiki_contracts.py",
    "/scripts/shiki_guardian.py",
    "/AGENTS.md",
    "/SYSTEM_PROMPT.md",
    "/CLAUDE.md",
)

RUNTIME_NAMES = runtime_names()

TARGET_STATE_DIRECTORIES = (
    ".shiki/goals",
    ".shiki/plans",
    ".shiki/tasks",
    ".shiki/dag",
    ".shiki/ledger",
    ".shiki/migrations",
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
    ".shiki/memories",
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
