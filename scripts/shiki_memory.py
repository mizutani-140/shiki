#!/usr/bin/env python3
"""Memory Loop state machine, validation rules, and CLI commands (proposal 0001 v2).

Memory entries under .shiki/memories are current-state documents; the audit
trail of every status transition lives in memory_transition ledger events.
Promotion is fail-closed: raw -> investigated -> verified -> distilled with no
skipping, verified requires local evidence, distilled requires operator
approval recorded in the ledger.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MEMORY_DIR = ".shiki/memories"
MEMORY_SCHEMA_PATH = ".shiki/schemas/memory-entry.schema.json"
MEMORY_LEDGER_TYPE = "memory-transition"
MEMORY_SCHEMA_VERSION = 1

# Set by autonomous execution surfaces (shiki runner / shiki loop). When this
# environment variable is present, distill/revoke/supersede are refused (B4).
AUTONOMOUS_CONTEXT_ENV = "SHIKI_AUTONOMOUS_EXECUTION"

MEMORY_STATUSES = ("raw", "investigated", "verified", "distilled")

MEMORY_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "raw": ("investigated",),
    "investigated": ("verified",),
    "verified": ("distilled",),
    "distilled": (),
}

MEMORY_AREAS = (
    "mergegate",
    "cca",
    "locks",
    "runner",
    "loop",
    "planning",
    "memory",
    "contracts",
    "manifest",
    "migrations",
    "handoff",
    "validator",
    "docs",
    "other",
)

MEMORY_SOURCE_KINDS = ("repair", "loop_stop", "cca_fail", "runner_fail", "manual")
MEMORY_EVIDENCE_KINDS = ("ledger", "report", "exec", "pr_check")
LOCAL_EVIDENCE_KINDS = ("ledger", "report", "exec")
# A local evidence kind must point at the matching state directory so that
# "local evidence >= 1" cannot be satisfied by an arbitrary existing .shiki path (B3).
LOCAL_EVIDENCE_PREFIX = {
    "ledger": ".shiki/ledger/L-",
    "report": ".shiki/reports/R-",
    "exec": ".shiki/runner/EXEC-",
}
# redaction.status on a PERSISTED entry must be clean or redacted; "skipped"
# is a capture-time signal that the entry is not written at all (B4).
REDACTION_STATUSES = ("clean", "redacted", "skipped")
STORED_REDACTION_STATUSES = ("clean", "redacted")

_ID_SUFFIX = r"(?:[0-9]{4,}|[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8})"
MEMORY_ID_RE = re.compile(rf"^MEM-{_ID_SUFFIX}$")
GOAL_ID_RE = re.compile(rf"^G-{_ID_SUFFIX}$")
TASK_ID_RE = re.compile(rf"^T-{_ID_SUFFIX}$")
LEDGER_PATH_RE = re.compile(rf"^\.shiki/ledger/L-{_ID_SUFFIX}\.json$")

# Status-specific required/prohibited top-level fields (B1). Nested
# requirements (investigation/verification members) are enforced separately.
_RAW_REQUIRED = (
    "id",
    "schema_version",
    "status",
    "area",
    "claim",
    "source",
    "created_at",
    "updated_at",
    "redaction",
)
MEMORY_STATUS_REQUIRED: dict[str, tuple[str, ...]] = {
    "raw": _RAW_REQUIRED,
    "investigated": _RAW_REQUIRED + ("investigation",),
    "verified": _RAW_REQUIRED + ("investigation", "verification", "last_verified"),
    "distilled": _RAW_REQUIRED
    + (
        "investigation",
        "verification",
        "last_verified",
        "rule",
        "approved_by",
        "approved_at",
        "approval_ledger",
        "active",
    ),
}
# Fields that belong only to a higher status. A lower status must not carry a
# higher status's blocks, so an unpromoted memory cannot look half-investigated,
# half-verified, or carry distilled-only lifecycle fields (B1).
_INVESTIGATION_FIELDS = ("investigation",)
_VERIFICATION_FIELDS = ("verification", "last_verified")
_DISTILLED_FIELDS = (
    "rule", "approved_by", "approved_at", "approval_ledger",
    "active", "supersedes", "superseded_by",
    "revoked_at", "revoked_by", "revocation_ledger",
)
MEMORY_STATUS_PROHIBITED: dict[str, tuple[str, ...]] = {
    "raw": _INVESTIGATION_FIELDS + _VERIFICATION_FIELDS + _DISTILLED_FIELDS,
    "investigated": _VERIFICATION_FIELDS + _DISTILLED_FIELDS,
    "verified": _DISTILLED_FIELDS,
    "distilled": (),
}


def memory_transition_errors(from_status: str, to_status: str) -> list[str]:
    """Return fail-closed errors for a requested status transition (B2)."""
    errors: list[str] = []
    if from_status not in MEMORY_TRANSITIONS:
        errors.append(f"unknown memory status {from_status!r}")
    if to_status not in MEMORY_STATUSES:
        errors.append(f"unknown memory status {to_status!r}")
    if errors:
        return errors
    if to_status not in MEMORY_TRANSITIONS[from_status]:
        errors.append(
            f"memory transition {from_status} -> {to_status} is not allowed; "
            "promotion must follow raw -> investigated -> verified -> distilled with no skipping"
        )
    return errors


def _is_set(data: dict[str, Any], key: str) -> bool:
    return data.get(key) is not None


def _non_empty_string(data: dict[str, Any], key: str, errors: list[str], *, label: str | None = None) -> None:
    label = label or key
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")


def _memory_evidence_errors(
    items: Any,
    *,
    label: str,
    root: Path | None,
) -> tuple[list[str], int]:
    """Validate structured evidence refs; return (errors, local evidence count) (B3)."""
    errors: list[str] = []
    local_count = 0
    if not isinstance(items, list):
        return [f"{label} must be a list of evidence objects"], 0
    for index, item in enumerate(items):
        item_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{item_label} must be an object")
            continue
        kind = item.get("kind")
        if kind not in MEMORY_EVIDENCE_KINDS:
            errors.append(f"{item_label}.kind must be one of {sorted(MEMORY_EVIDENCE_KINDS)}")
            continue
        if kind in LOCAL_EVIDENCE_KINDS:
            path_value = item.get("path")
            prefix = LOCAL_EVIDENCE_PREFIX[kind]
            if not isinstance(path_value, str) or not path_value.startswith(prefix):
                errors.append(f"{item_label}.path for kind {kind} must be under {prefix}")
                continue
            if root is not None and not (root / path_value).is_file():
                errors.append(f"{item_label}.path {path_value} does not exist")
                continue
            local_count += 1
        else:
            if not isinstance(item.get("pr"), int) or isinstance(item.get("pr"), bool) or item["pr"] < 1:
                errors.append(f"{item_label}.pr must be a positive integer")
            if not isinstance(item.get("check"), str) or not item["check"].strip():
                errors.append(f"{item_label}.check must be a non-empty string")
    return errors, local_count


def _ledger_ref_errors(value: Any, *, label: str, root: Path | None) -> list[str]:
    if not isinstance(value, str) or not LEDGER_PATH_RE.match(value):
        return [f"{label} must reference a .shiki/ledger/L-*.json entry"]
    if root is not None and not (root / value).is_file():
        return [f"{label} {value} does not exist"]
    return []


def memory_entry_errors(data: dict[str, Any], *, root: Path | None = None) -> list[str]:
    """Fail-closed status-specific validation for one memory entry (B1/B3/B6).

    A prohibited field counts as present only when it is set to a non-null
    value; memory files are current-state documents and may carry explicit
    nulls. When root is provided, local evidence paths and ledger references
    are checked for existence on disk.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["memory entry must be a JSON object"]

    status = data.get("status")
    if status not in MEMORY_STATUSES:
        return [f"status must be one of {sorted(MEMORY_STATUSES)}"]

    memory_id = data.get("id")
    if not isinstance(memory_id, str) or not MEMORY_ID_RE.match(memory_id):
        errors.append("id must match MEM-0001 or MEM-YYYYMMDDTHHMMSSffffffZ-<8 hex>")
    if data.get("schema_version") != MEMORY_SCHEMA_VERSION:
        errors.append(f"schema_version must be {MEMORY_SCHEMA_VERSION}")

    for key in MEMORY_STATUS_REQUIRED[status]:
        if not _is_set(data, key):
            errors.append(f"status {status} requires {key}")
    for key in MEMORY_STATUS_PROHIBITED[status]:
        if _is_set(data, key):
            errors.append(f"status {status} prohibits {key}")

    if data.get("area") not in MEMORY_AREAS:
        errors.append(f"area must be one of {sorted(MEMORY_AREAS)}")
    _non_empty_string(data, "claim", errors)
    _non_empty_string(data, "created_at", errors)
    _non_empty_string(data, "updated_at", errors)

    for key in ("applies_to", "tags"):
        value = data.get(key)
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            errors.append(f"{key} must be a list of non-empty strings")

    source = data.get("source")
    if not isinstance(source, dict) or source.get("kind") not in MEMORY_SOURCE_KINDS:
        errors.append(f"source.kind must be one of {sorted(MEMORY_SOURCE_KINDS)}")
    # Every memory must be anchored to a real Goal: source.goal_id is required and
    # must be a well-formed G-* id. Existence against an actual goal file is
    # cross-checked by the repository validator. This makes a committed entry
    # without a goal anchor fail closed at the engine boundary.
    if isinstance(source, dict):
        source_goal_id = source.get("goal_id")
        if not source_goal_id or not GOAL_ID_RE.match(str(source_goal_id)):
            errors.append("source.goal_id is required and must match ^G-<id>")

    redaction = data.get("redaction")
    if not isinstance(redaction, dict) or redaction.get("status") not in STORED_REDACTION_STATUSES:
        errors.append(f"redaction.status on a stored entry must be one of {sorted(STORED_REDACTION_STATUSES)}")

    evidence_errors, _ = _memory_evidence_errors(data.get("evidence", []), label="evidence", root=root)
    errors.extend(evidence_errors)

    if status in ("investigated", "verified", "distilled"):
        investigation = data.get("investigation")
        if not isinstance(investigation, dict):
            errors.append("investigation must be an object with summary and refs")
        else:
            if not isinstance(investigation.get("summary"), str) or not investigation["summary"].strip():
                errors.append("investigation.summary must be a non-empty string")
            if not isinstance(investigation.get("refs"), list):
                errors.append("investigation.refs must be a list")

    if status in ("verified", "distilled"):
        _non_empty_string(data, "last_verified", errors)
        verification = data.get("verification")
        if not isinstance(verification, dict):
            errors.append("verification must be an object with verified_at and evidence")
        else:
            if not isinstance(verification.get("verified_at"), str) or not verification["verified_at"].strip():
                errors.append("verification.verified_at must be a non-empty string")
            verification_errors, local_count = _memory_evidence_errors(
                verification.get("evidence"), label="verification.evidence", root=root
            )
            errors.extend(verification_errors)
            if not verification_errors and local_count < 1:
                errors.append(
                    "verified promotion requires at least one local evidence "
                    f"(kind in {sorted(LOCAL_EVIDENCE_KINDS)}); remote pr_check evidence alone is not sufficient"
                )

    if status == "distilled":
        _non_empty_string(data, "rule", errors)
        _non_empty_string(data, "approved_by", errors)
        _non_empty_string(data, "approved_at", errors)
        if _is_set(data, "approval_ledger"):
            errors.extend(_ledger_ref_errors(data["approval_ledger"], label="approval_ledger", root=root))
        active = data.get("active")
        if not isinstance(active, bool):
            errors.append("active must be a boolean")

        superseded_by = data.get("superseded_by")
        if superseded_by is not None and (
            not isinstance(superseded_by, str) or not MEMORY_ID_RE.match(superseded_by)
        ):
            errors.append("superseded_by must be a MEM id or null")
        supersedes = data.get("supersedes")
        if supersedes is not None and (
            not isinstance(supersedes, list)
            or not all(isinstance(item, str) and MEMORY_ID_RE.match(item) for item in supersedes)
        ):
            errors.append("supersedes must be a list of MEM ids")

        revoked_at = data.get("revoked_at")
        if active is True and (revoked_at is not None or superseded_by is not None):
            errors.append("active distilled rules must have revoked_at=null and superseded_by=null")
        if revoked_at is not None:
            if active is not False:
                errors.append("revoked distilled rules must set active=false")
            if not _is_set(data, "revoked_by"):
                errors.append("revoked distilled rules require revoked_by")
            if not _is_set(data, "revocation_ledger"):
                errors.append("revoked distilled rules require revocation_ledger")
            else:
                errors.extend(_ledger_ref_errors(data["revocation_ledger"], label="revocation_ledger", root=root))

    return errors



# --- effectors and CLI -------------------------------------------------------
#
# Pure effectors (capture_memory/.../supersede_memory) hold the logic and are
# unit-tested directly; the cmd_* functions are thin argparse wrappers. Imports
# are kept below the pure validation block so validate_shiki.py can import the
# validation helpers without pulling in the control-plane modules.

from shiki_process import (  # noqa: E402
    ShikiError,
    ensure_control_dirs,
    print_json,
    read_json,
    shiki_path,
    target_path,
    utc_now,
    write_json,
)
from shiki_state import new_control_id  # noqa: E402
from shiki_tasks import append_ledger, require_github_first_target  # noqa: E402


def _memory_path(target: Path, memory_id: str) -> Path:
    return shiki_path(target, "memories", f"{memory_id}.json")


def load_memory(target: Path, memory_id: str) -> dict[str, Any]:
    return read_json(_memory_path(target, memory_id))


def load_all_memories(target: Path) -> list[dict[str, Any]]:
    """Read every memory entry from .shiki/memories (pure; never mutates)."""
    mem_dir = shiki_path(target, "memories")
    if not mem_dir.exists():
        return []
    return [read_json(path) for path in sorted(mem_dir.glob("MEM-*.json"))]


# --- Consult: deterministic distilled-rule selection (proposal 0001 v2 §3.5) ---
#
# CI-08 assumption (operator-approved): the frozen spec selects rules on
# task.area / goal.area / applies_to / tags, but neither the task nor goal schema
# defines area/tags and no record carries them. Rather than amend those schemas,
# T3 derives a non-persisted "consult context" from existing task/goal metadata:
# task locks (path -> area) and required_skills (-> tags). A distilled rule is
# selected when it is active/non-revoked/non-superseded, carries at least one
# selector (area/applies_to/tags), and any selector OR-overlaps the derived
# context. Reading never mutates state.

# Substring (in a lock path, lowercased) -> coarse MEMORY_AREAS value. Order is
# irrelevant: every match is unioned into a set, so the derivation is stable.
_LOCK_AREA_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("mergegate", "mergegate"),
    ("shiki_loop", "loop"),
    ("shiki_runtime", "runner"),
    ("runner", "runner"),
    ("shiki_memory", "memory"),
    ("shiki_contracts", "contracts"),
    ("shiki_migrations", "migrations"),
    ("validate_shiki", "validator"),
    ("shiki_tasks", "planning"),
    ("handoff", "handoff"),
    ("locks", "locks"),
    ("cca", "cca"),
    ("manifest", "manifest"),
    ("docs/", "docs"),
    ("context.md", "docs"),
    ("claude.md", "docs"),
)


def derive_consult_context(task: dict[str, Any], goal: dict[str, Any] | None) -> dict[str, set]:
    """Derive the non-persisted consult context (areas, tags) for a task/goal.

    Areas come from the task's lock paths; tags come from task and goal
    required_skills (lowercased). Identifiers/titles are descriptive only and
    are never used as match input.
    """
    areas: set[str] = set()
    for lock in task.get("locks") or []:
        path = str(lock).lower()
        for needle, area in _LOCK_AREA_SUBSTRINGS:
            if needle in path:
                areas.add(area)
    tags: set[str] = set()
    for skill in (task.get("required_skills") or []):
        tags.add(str(skill).strip().lower())
    for skill in ((goal or {}).get("required_skills") or []):
        tags.add(str(skill).strip().lower())
    return {"areas": areas, "tags": tags}


def _rule_is_eligible(memory: dict[str, Any]) -> bool:
    """Active, non-revoked, non-superseded distilled rule with >=1 selector."""
    if memory.get("status") != "distilled":
        return False
    if memory.get("active") is not True:
        return False
    if memory.get("superseded_by") is not None:
        return False
    if memory.get("revoked_at") is not None:
        return False
    has_selector = bool(memory.get("area") or memory.get("applies_to") or memory.get("tags"))
    return has_selector


def select_distilled_rules(
    task: dict[str, Any],
    goal: dict[str, Any] | None,
    memories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pure, deterministic selection of distilled rules for a handoff.

    Returns active/non-revoked/non-superseded distilled rules whose area /
    applies_to (matched against the derived areas) or tags (matched against the
    derived tags) OR-overlap the consult context, stable-sorted by last_verified
    descending then MEM id ascending (entries missing last_verified sort last).
    Never mutates ``memories`` or any input.
    """
    context = derive_consult_context(task, goal)
    ctx_areas, ctx_tags = context["areas"], context["tags"]
    matched: list[dict[str, Any]] = []
    for memory in memories:
        if not _rule_is_eligible(memory):
            continue
        # Normalize rule selectors symmetrically with the derived context
        # (which is lowercased/stripped) so a case-variant area/applies_to/tag
        # is never silently dropped.
        rule_areas = {str(memory["area"]).strip().lower()} if memory.get("area") else set()
        rule_areas |= {str(a).strip().lower() for a in (memory.get("applies_to") or [])}
        rule_tags = {str(t).strip().lower() for t in (memory.get("tags") or [])}
        if (rule_areas & ctx_areas) or (rule_tags & ctx_tags):
            matched.append(memory)
    # Stable sort: id ascending first, then last_verified descending. Python's
    # sort is stable, so equal last_verified keeps id-ascending order; an empty
    # last_verified is the smallest key, so reverse=True places it last.
    matched = sorted(matched, key=lambda m: str(m.get("id") or ""))
    matched = sorted(matched, key=lambda m: str(m.get("last_verified") or ""), reverse=True)
    return matched


def render_distilled_rules_section(rules: list[dict[str, Any]]) -> list[str]:
    """Render the always-present ``## Distilled Rules`` handoff section."""
    lines = ["## Distilled Rules", ""]
    if not rules:
        lines.append("none applicable")
        return lines
    for rule in rules:
        lines.append(f"- {str(rule.get('rule', '')).strip()} ({rule.get('id')})")
    return lines


def _save(target: Path, memory: dict[str, Any]) -> None:
    write_json(_memory_path(target, memory["id"]), memory)


def in_autonomous_context() -> bool:
    import os
    return bool(os.environ.get(AUTONOMOUS_CONTEXT_ENV))


def _require_operator(action: str) -> None:
    if in_autonomous_context():
        raise ShikiError(
            f"`shiki memory {action}` is operator-only and is refused in an autonomous "
            f"execution context ({AUTONOMOUS_CONTEXT_ENV} is set). Run it from an "
            "interactive operator session."
        )


def memory_source_errors(target: Path, goal_id: str | None, task_id: str | None) -> list[str]:
    """A memory's source goal/task anchor the ledger events it emits; those
    events must satisfy validate_ledger (goal_id ^G-, task_id ^T- or null, and
    an existing goal file). Validated here so capture fails open before it
    writes a tree-invalidating ledger entry."""
    errors: list[str] = []
    if not goal_id or not GOAL_ID_RE.match(str(goal_id)):
        errors.append("source goal_id is required and must match ^G-<id>")
    elif not (target / ".shiki" / "goals" / f"{goal_id}.json").is_file():
        errors.append(f"source goal_id {goal_id} has no matching .shiki/goals file")
    if task_id is not None and not TASK_ID_RE.match(str(task_id)):
        errors.append("source task_id must match ^T-<id> or be omitted")
    return errors


def _record_transition(target: Path, memory: dict[str, Any], summary: str, *, ledger_type: str = MEMORY_LEDGER_TYPE) -> str:
    source = memory.get("source") or {}
    return append_ledger(
        target,
        goal_id=str(source.get("goal_id")),
        task_id=source.get("task_id"),
        ledger_type=ledger_type,
        summary=summary,
        evidence=[f".shiki/memories/{memory['id']}.json"],
    )


def capture_memory(
    target: Path,
    *,
    area: str,
    claim: str,
    source_kind: str,
    goal_id: str | None = None,
    task_id: str | None = None,
    applies_to: list[str] | None = None,
    tags: list[str] | None = None,
    evidence: list[str] | None = None,
    redaction: str = "clean",
    redaction_notes: str = "",
) -> dict[str, Any]:
    """Capture a raw memory. Fail-open: an invalid entry is never written (M5)."""
    ensure_control_dirs(target)
    source_errors = memory_source_errors(target, goal_id, task_id)
    if source_errors:
        return {"memory_id": None, "written": False, "warnings": source_errors}
    if redaction == "skipped":
        # redact-unable capture writes nothing; the lesson is not persisted (B4).
        return {"memory_id": None, "written": False, "warnings": ["redaction skipped: capture writes no memory entry"]}
    now = utc_now()
    structured: list[dict[str, Any]] = []
    for ref in evidence or []:
        kind = "ledger" if ref.startswith(".shiki/ledger/") else "report" if ref.startswith(".shiki/reports/") else "exec"
        structured.append({"kind": kind, "path": ref})
    memory = {
        "id": new_control_id("MEM"),
        "schema_version": MEMORY_SCHEMA_VERSION,
        "status": "raw",
        "area": area,
        "applies_to": applies_to or [],
        "tags": tags or [],
        "claim": claim,
        "evidence": structured,
        "source": {"kind": source_kind, "goal_id": goal_id, "task_id": task_id},
        "created_at": now,
        "updated_at": now,
        "redaction": {"status": redaction, "notes": redaction_notes or ""},
    }
    errors = memory_entry_errors(memory, root=target)
    if errors:
        return {"memory_id": memory["id"], "written": False, "warnings": errors}
    _save(target, memory)
    ledger_id = _record_transition(target, memory, f"Memory {memory['id']} captured (raw, area={area}, source={source_kind})")
    return {"memory_id": memory["id"], "status": "raw", "written": True, "ledger_id": ledger_id}


# Secret-like patterns that must never reach a committed memory file (B10). The
# auto-capture claim is a short, structured summary; these are a defense layer in
# case a reason/diagnostic string embeds a credential.
_SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                    # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),                 # GitHub fine-grained PAT
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                             # AWS access key id
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),                 # Slack token
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),                          # OpenAI-style key
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"),  # JWT
    re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----", re.DOTALL),
    re.compile(
        r"(?i)\b[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIAL)[A-Za-z0-9_]*\s*[=:]\s*\S+"
    ),
)


def redact_text(text: str) -> tuple[str, bool]:
    """Replace secret-like tokens with ``[REDACTED]``.

    Returns ``(redacted_text, found)`` where ``found`` is True when any secret
    pattern matched. Pure and side-effect free.
    """
    redacted = str(text or "")
    found = False
    for pattern in _SECRET_PATTERNS:
        new_text = pattern.sub("[REDACTED]", redacted)
        if new_text != redacted:
            found = True
            redacted = new_text
    return redacted, found


@dataclass(frozen=True)
class CaptureResult:
    """The single contract every auto-capture hook receives.

    Hooks call ``capture_failure`` and inspect this result; they must NOT add
    their own exception handling or file checks — that would re-open the
    fail-open boundary. ``written`` is True only when a raw memory was persisted;
    ``skipped_reason`` explains a deliberate no-write; ``warnings`` carries any
    non-fatal diagnostics.
    """

    written: bool
    memory_id: str | None = None
    skipped_reason: str | None = None
    warnings: tuple[str, ...] = ()


def capture_failure(
    target: Path,
    *,
    source_kind: str,
    area: str,
    claim: str,
    goal_id: str | None,
    task_id: str | None = None,
    evidence_refs: list[str] | None = None,
    applies_to: list[str] | None = None,
    tags: list[str] | None = None,
) -> CaptureResult:
    """Fail-open auto-capture of a failure as a raw memory (proposal 3.3).

    Contract:
      - NEVER raises into the caller — a capture failure must not stop the loop (M5);
      - requires an existing ``source.goal_id`` (no sentinel fallback);
      - writes only a raw memory;
      - never stores stdout/stderr bodies — only the structured evidence refs
        passed in plus a short redacted claim;
      - redacts secret-like content and records redaction.status;
      - returns a skip (writes nothing) when the goal anchor is missing, the
        evidence is invalid, or no safe claim remains.
    """
    try:
        redacted, found = redact_text(claim)
        # Unsalvageable: the input was essentially only a secret, leaving no
        # meaningful claim after redaction — write nothing (B10).
        if not redacted.replace("[REDACTED]", "").strip():
            reason = "redaction left no safe claim"
            return CaptureResult(written=False, skipped_reason=reason, warnings=(reason + "; capture writes no memory entry",))
        outcome = capture_memory(
            target,
            area=area,
            claim=redacted,
            source_kind=source_kind,
            goal_id=goal_id,
            task_id=task_id,
            applies_to=applies_to,
            tags=tags,
            evidence=list(evidence_refs or []),
            redaction="redacted" if found else "clean",
        )
        if outcome.get("written"):
            return CaptureResult(written=True, memory_id=outcome.get("memory_id"))
        warnings = tuple(outcome.get("warnings") or ())
        return CaptureResult(
            written=False,
            memory_id=outcome.get("memory_id"),
            skipped_reason=warnings[0] if warnings else "capture not written",
            warnings=warnings,
        )
    except Exception as error:  # noqa: BLE001 - fail-open is mandated (M5); never break the loop
        reason = f"capture failed: {error}"
        return CaptureResult(written=False, skipped_reason=reason, warnings=(reason,))


def _safe_json(path: Path) -> Any:
    """Load JSON, returning None on any read/parse error (scorecard is failure
    tolerant and must never raise on a malformed state file)."""
    import json as _json
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse_iso(value: Any) -> Any:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def compute_scorecard(target: Path, goal_id: str, *, tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Goal-completion scorecard (proposal 3.6).

    Counts come ONLY from ledger events and task state — never from raw memories
    (that would create a failure -> memory -> scorecard -> suggestion -> memory
    cycle). Ledger events are deduplicated by their event id. Uncomputable
    categories are 0 (never null) with a warning. Failure tolerant: any error
    degrades to a minimal scorecard plus a warning rather than raising.
    """
    warnings: list[str] = []
    tasks = tasks or []
    repairs_total = 0
    lock_amendments = 0
    cca_reruns = 0
    window_from = window_to = None
    try:
        ledger_dir = target / ".shiki" / "ledger"
        entries: dict[str, dict[str, Any]] = {}
        if ledger_dir.is_dir():
            for path in ledger_dir.glob("L-*.json"):
                data = _safe_json(path)
                if isinstance(data, dict) and data.get("goal_id") == goal_id and data.get("id"):
                    entries[str(data["id"])] = data  # dedup by ledger event id
        for entry in entries.values():
            etype = entry.get("type")
            if etype == "repair":
                repairs_total += 1
            elif etype == "lock":
                lock_amendments += 1
            stamp = _parse_iso(entry.get("timestamp"))
            if stamp is not None:
                window_from = stamp if window_from is None or stamp < window_from else window_from
                window_to = stamp if window_to is None or stamp > window_to else window_to
        cca_reruns = sum(int(task.get("cca_rerun_count") or 0) for task in tasks)
    except Exception as error:  # noqa: BLE001 - scorecard generation is failure tolerant (3.6)
        warnings.append(f"scorecard ledger aggregation degraded: {error}")

    completed = sum(1 for task in tasks if task.get("status") == "done")
    failed = sum(1 for task in tasks if task.get("status") == "repair-needed")
    # Loop stops are captured as memories, not ledger events; counting them from
    # raw memory would be the forbidden circular source, so they are reported as
    # 0 with a warning rather than guessed.
    warnings.append("loop_stops are captured as raw memories and are not counted by the ledger-only scorecard")
    if repairs_total:
        warnings.append("repairs.by_area is not derivable from ledger events; reported empty")

    duration_ms = 0
    if window_from is not None and window_to is not None:
        duration_ms = int((window_to - window_from).total_seconds() * 1000)

    return {
        "goal_id": goal_id,
        "generated_at": utc_now(),
        "window": {
            "from": window_from.isoformat() if window_from is not None else None,
            "to": window_to.isoformat() if window_to is not None else None,
        },
        "tasks": {"total": len(tasks), "completed": completed, "failed": failed},
        "repairs": {"total": repairs_total, "by_area": {}},
        "cca_reruns": {"total": cca_reruns},
        "loop_stops": {"total": 0, "by_reason": {}},
        "lock_amendments": {"total": lock_amendments},
        "duration_ms": duration_ms,
        "warnings": warnings,
        "suggestions": distillation_suggestions(target, goal_id),
    }


def distillation_suggestions(target: Path, goal_id: str, *, min_group: int = 2) -> list[dict[str, Any]]:
    """Operator-facing distillation suggestions (proposal 3.6, B5).

    A suggestion is advisory only: it NEVER changes a memory's status and NEVER
    creates a distilled rule. Adoption still requires the normal
    investigate -> promote -> distill flow. Recurring failures in the same area
    (>= min_group raw/verified memories anchored to this goal) become one
    suggestion that names the source MEM ids.
    """
    suggestions: list[dict[str, Any]] = []
    try:
        mem_dir = target / ".shiki" / "memories"
        if not mem_dir.is_dir():
            return suggestions
        by_area: dict[str, list[str]] = {}
        for path in sorted(mem_dir.glob("MEM-*.json")):
            data = _safe_json(path)
            if not isinstance(data, dict):
                continue
            source = data.get("source") or {}
            if source.get("goal_id") != goal_id:
                continue
            if data.get("status") not in ("raw", "investigated", "verified"):
                continue
            by_area.setdefault(str(data.get("area") or "other"), []).append(str(data.get("id")))
        for area, ids in sorted(by_area.items()):
            if len(ids) >= min_group:
                suggestions.append({
                    "from_memories": ids,
                    "proposed_rule": f"Recurring {area} failures in this goal suggest a generalizable rule.",
                    "note": "採用には verified 経由の distill が必要（suggestion は memory status を変えない）",
                })
    except Exception:  # noqa: BLE001 - suggestions are advisory and failure tolerant
        return suggestions
    return suggestions


def investigate_memory(target: Path, memory_id: str, *, summary: str, refs: list[str] | None = None) -> dict[str, Any]:
    memory = load_memory(target, memory_id)
    errors = memory_transition_errors(memory.get("status", ""), "investigated")
    if errors:
        raise ShikiError("; ".join(errors))
    memory["status"] = "investigated"
    memory["investigation"] = {"summary": summary, "refs": refs or []}
    memory["updated_at"] = utc_now()
    entry_errors = memory_entry_errors(memory, root=target)
    if entry_errors:
        raise ShikiError("; ".join(entry_errors))
    _save(target, memory)
    ledger_id = _record_transition(target, memory, f"Memory {memory_id} raw -> investigated")
    return {"memory_id": memory_id, "status": "investigated", "ledger_id": ledger_id}


def promote_memory(target: Path, memory_id: str, *, local_evidence: list[tuple[str, str]]) -> dict[str, Any]:
    memory = load_memory(target, memory_id)
    errors = memory_transition_errors(memory.get("status", ""), "verified")
    if errors:
        raise ShikiError("; ".join(errors))
    now = utc_now()
    structured = [{"kind": kind, "path": path} for kind, path in (local_evidence or [])]
    memory["status"] = "verified"
    memory["evidence"] = (memory.get("evidence") or []) + structured
    memory["verification"] = {"verified_at": now, "validator": "validate_memory", "evidence": structured}
    memory["last_verified"] = now
    memory["updated_at"] = now
    entry_errors = memory_entry_errors(memory, root=target)
    if entry_errors:
        raise ShikiError("; ".join(entry_errors))
    _save(target, memory)
    ledger_id = _record_transition(target, memory, f"Memory {memory_id} investigated -> verified")
    return {"memory_id": memory_id, "status": "verified", "ledger_id": ledger_id}


def distill_memory(
    target: Path,
    memory_id: str,
    *,
    rule: str,
    approved_by: str,
    approve: bool,
    supersede: list[str] | None = None,
) -> dict[str, Any]:
    # Audit + atomicity (B2): all validation that can fail happens BEFORE any
    # side effect, so an operator-approval ledger is never written for a
    # mutation that then fails to persist. The placeholder lets the distilled
    # entry pass structural validation before its real approval ledger exists.
    _PLACEHOLDER_LEDGER = ".shiki/ledger/L-0000.json"
    _require_operator("distill")
    if not approve:
        raise ShikiError("distill requires explicit operator approval: pass --approve")
    memory = load_memory(target, memory_id)
    errors = memory_transition_errors(memory.get("status", ""), "distilled")
    if errors:
        raise ShikiError("; ".join(errors))
    now = utc_now()
    candidate = {
        **memory,
        "status": "distilled",
        "rule": rule,
        "approved_by": approved_by,
        "approved_at": now,
        "approval_ledger": _PLACEHOLDER_LEDGER,
        "active": True,
        "supersedes": supersede or [],
        "superseded_by": None,
        "revoked_at": None,
        "revoked_by": None,
        "revocation_ledger": None,
        "updated_at": now,
    }
    pre_errors = memory_entry_errors(candidate, root=None)
    if pre_errors:
        raise ShikiError("; ".join(pre_errors))
    # Supersede targets must exist and be distilled before any write.
    prior_memories = []
    for prior in supersede or []:
        prior_memory = load_memory(target, prior)
        if prior_memory.get("status") != "distilled":
            raise ShikiError(f"supersede target {prior} is not a distilled rule")
        prior_memories.append(prior_memory)
    approval_ledger = _record_transition(
        target, memory, f"Operator {approved_by} approved distilling memory {memory_id}", ledger_type="review"
    )
    candidate["approval_ledger"] = f".shiki/ledger/{approval_ledger}.json"
    full_errors = memory_entry_errors(candidate, root=target)
    if full_errors:
        raise ShikiError("; ".join(full_errors))
    _save(target, candidate)
    for prior in supersede or []:
        supersede_memory(target, prior, superseded_by=memory_id, approved_by=approved_by)
    ledger_id = _record_transition(target, candidate, f"Memory {memory_id} verified -> distilled (active rule)")
    return {"memory_id": memory_id, "status": "distilled", "approval_ledger": approval_ledger, "ledger_id": ledger_id}


def supersede_memory(target: Path, memory_id: str, *, superseded_by: str, approved_by: str = "operator") -> dict[str, Any]:
    _require_operator("supersede")
    memory = load_memory(target, memory_id)
    if memory.get("status") != "distilled":
        raise ShikiError("only distilled rules can be superseded")
    now = utc_now()
    candidate = {**memory, "active": False, "superseded_by": superseded_by, "updated_at": now}
    pre_errors = memory_entry_errors(candidate, root=None)
    if pre_errors:
        raise ShikiError("; ".join(pre_errors))
    approval_ledger = _record_transition(
        target, memory, f"Operator {approved_by} approved superseding memory {memory_id} with {superseded_by}", ledger_type="review"
    )
    _save(target, candidate)
    transition_ledger = _record_transition(target, candidate, f"Memory {memory_id} superseded by {superseded_by}")
    return {"memory_id": memory_id, "active": False, "superseded_by": superseded_by, "approval_ledger": approval_ledger, "ledger_id": transition_ledger}


def revoke_memory(target: Path, memory_id: str, *, revoked_by: str, reason: str) -> dict[str, Any]:
    _require_operator("revoke")
    memory = load_memory(target, memory_id)
    if memory.get("status") != "distilled":
        raise ShikiError("only distilled rules can be revoked")
    _PLACEHOLDER_LEDGER = ".shiki/ledger/L-0000.json"
    now = utc_now()
    candidate = {
        **memory,
        "active": False,
        "revoked_at": now,
        "revoked_by": revoked_by,
        "revocation_ledger": _PLACEHOLDER_LEDGER,
        "updated_at": now,
    }
    pre_errors = memory_entry_errors(candidate, root=None)
    if pre_errors:
        raise ShikiError("; ".join(pre_errors))
    revoke_ledger = _record_transition(
        target, memory, f"Operator {revoked_by} revoked memory {memory_id}: {reason}", ledger_type="review"
    )
    candidate["revocation_ledger"] = f".shiki/ledger/{revoke_ledger}.json"
    full_errors = memory_entry_errors(candidate, root=target)
    if full_errors:
        raise ShikiError("; ".join(full_errors))
    _save(target, candidate)
    transition_ledger = _record_transition(target, candidate, f"Memory {memory_id} distilled -> revoked")
    return {"memory_id": memory_id, "active": False, "revocation_ledger": revoke_ledger, "ledger_id": transition_ledger}


def cmd_memory_capture(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(capture_memory(
        target, area=args.area, claim=args.claim, source_kind=args.source_kind,
        goal_id=args.goal_id, task_id=args.task_id, applies_to=args.applies_to,
        tags=args.tag, evidence=args.evidence, redaction=args.redaction, redaction_notes=args.redaction_notes,
    ))
    return 0


def cmd_memory_list(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    directory = shiki_path(target, "memories")
    rows = []
    if directory.exists():
        for path in sorted(directory.glob("MEM-*.json")):
            data = read_json(path)
            if args.status and data.get("status") != args.status:
                continue
            if args.area and data.get("area") != args.area:
                continue
            rows.append({k: data.get(k) for k in ("id", "status", "area", "active", "claim")})
    print_json({"memories": rows, "count": len(rows)})
    return 0


def cmd_memory_investigate(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(investigate_memory(target, args.memory_id, summary=args.summary, refs=args.ref))
    return 0


def cmd_memory_promote(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(promote_memory(target, args.memory_id, local_evidence=[tuple(e) for e in args.local_evidence]))
    return 0


def cmd_memory_distill(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(distill_memory(target, args.memory_id, rule=args.rule, approved_by=args.approved_by, approve=args.approve, supersede=args.supersede))
    return 0


def cmd_memory_revoke(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(revoke_memory(target, args.memory_id, revoked_by=args.revoked_by, reason=args.reason))
    return 0


def cmd_memory_supersede(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(supersede_memory(target, args.memory_id, superseded_by=args.superseded_by))
    return 0
