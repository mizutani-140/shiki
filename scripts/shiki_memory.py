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
REDACTION_STATUSES = ("clean", "redacted", "skipped")

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
MEMORY_STATUS_PROHIBITED: dict[str, tuple[str, ...]] = {
    "raw": ("rule", "approved_by", "approved_at", "approval_ledger", "last_verified"),
    "investigated": ("rule", "approved_by", "approved_at", "approval_ledger"),
    "verified": ("rule", "approved_by", "approved_at", "approval_ledger"),
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
            if not isinstance(path_value, str) or not path_value.startswith(".shiki/"):
                errors.append(f"{item_label}.path must be a repository-relative .shiki/ path")
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

    redaction = data.get("redaction")
    if not isinstance(redaction, dict) or redaction.get("status") not in REDACTION_STATUSES:
        errors.append(f"redaction.status must be one of {sorted(REDACTION_STATUSES)}")

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
    _require_operator("distill")
    if not approve:
        raise ShikiError("distill requires explicit operator approval: pass --approve")
    memory = load_memory(target, memory_id)
    errors = memory_transition_errors(memory.get("status", ""), "distilled")
    if errors:
        raise ShikiError("; ".join(errors))
    approval_ledger = _record_transition(
        target, memory, f"Operator {approved_by} approved distilling memory {memory_id}", ledger_type="review"
    )
    now = utc_now()
    memory["status"] = "distilled"
    memory["rule"] = rule
    memory["approved_by"] = approved_by
    memory["approved_at"] = now
    memory["approval_ledger"] = f".shiki/ledger/{approval_ledger}.json"
    memory["active"] = True
    memory["supersedes"] = supersede or []
    memory["superseded_by"] = None
    memory["revoked_at"] = None
    memory["updated_at"] = now
    entry_errors = memory_entry_errors(memory, root=target)
    if entry_errors:
        raise ShikiError("; ".join(entry_errors))
    _save(target, memory)
    for prior in supersede or []:
        supersede_memory(target, prior, superseded_by=memory_id)
    ledger_id = _record_transition(target, memory, f"Memory {memory_id} verified -> distilled (active rule)")
    return {"memory_id": memory_id, "status": "distilled", "approval_ledger": approval_ledger, "ledger_id": ledger_id}


def supersede_memory(target: Path, memory_id: str, *, superseded_by: str) -> dict[str, Any]:
    _require_operator("supersede")
    memory = load_memory(target, memory_id)
    if memory.get("status") != "distilled":
        raise ShikiError("only distilled rules can be superseded")
    now = utc_now()
    memory["active"] = False
    memory["superseded_by"] = superseded_by
    memory["updated_at"] = now
    entry_errors = memory_entry_errors(memory, root=target)
    if entry_errors:
        raise ShikiError("; ".join(entry_errors))
    _save(target, memory)
    _record_transition(target, memory, f"Memory {memory_id} superseded by {superseded_by}")
    return {"memory_id": memory_id, "active": False, "superseded_by": superseded_by}


def revoke_memory(target: Path, memory_id: str, *, revoked_by: str, reason: str) -> dict[str, Any]:
    _require_operator("revoke")
    memory = load_memory(target, memory_id)
    if memory.get("status") != "distilled":
        raise ShikiError("only distilled rules can be revoked")
    revoke_ledger = _record_transition(
        target, memory, f"Operator {revoked_by} revoked memory {memory_id}: {reason}", ledger_type="review"
    )
    now = utc_now()
    memory["active"] = False
    memory["revoked_at"] = now
    memory["revoked_by"] = revoked_by
    memory["revocation_ledger"] = f".shiki/ledger/{revoke_ledger}.json"
    memory["updated_at"] = now
    entry_errors = memory_entry_errors(memory, root=target)
    if entry_errors:
        raise ShikiError("; ".join(entry_errors))
    _save(target, memory)
    return {"memory_id": memory_id, "active": False, "revocation_ledger": revoke_ledger}


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
