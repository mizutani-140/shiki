#!/usr/bin/env python3
"""Dependency-free .shiki state migration framework."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Literal

from shiki_process import print_json

MigrationStatus = Literal["applied", "pending", "failed", "skipped"]

MIGRATION_STATE_PATH = ".shiki/migrations/state.json"
MIGRATION_STATE_VERSION = 1
BASELINE_MIGRATION_ID = "M-20260604-0001-baseline"
MIGRATION_ID_RE = re.compile(r"^M-[0-9]{8}-[0-9]{4}-[a-z0-9][a-z0-9-]*$")
MIGRATION_SOURCE_OF_TRUTH = "Repository-local Shiki migration state. GitHub operational state remains authoritative."


class MigrationError(Exception):
    """Raised when migration state, registry, or apply policy is invalid."""


@dataclass(frozen=True)
class Migration:
    id: str
    title: str
    description: str
    introduced_in: str
    requires: tuple[str, ...] = ()
    affected_paths: tuple[str, ...] = ()
    destructive: bool = False
    apply: Callable[[Path, bool], dict[str, Any]] | None = None


@dataclass(frozen=True)
class MigrationRecord:
    id: str
    status: str
    applied_at: str
    actor: str
    summary: str
    evidence: tuple[str, ...] = field(default_factory=tuple)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _baseline_apply(root: Path, dry_run: bool) -> dict[str, Any]:
    required = [
        ".shiki/manifest.json",
        ".shiki/tasks",
        ".shiki/goals",
        ".shiki/ledger",
        ".shiki/goals/G-0012.json",
    ]
    missing = [relative for relative in required if not (root / relative).exists()]
    if missing:
        raise MigrationError(f"{BASELINE_MIGRATION_ID}: baseline prerequisites are missing: {', '.join(missing)}")
    return {
        "summary": "Accepted existing post-P1.3.5 Shiki layout as the migration baseline.",
        "evidence": [
            ".shiki/manifest.json exists.",
            ".shiki/tasks, .shiki/goals, and .shiki/ledger exist.",
            "G-0012 exists.",
            "Known prior state is accepted as the baseline without rewriting historical records.",
        ],
        "dry_run": dry_run,
    }


def migration_registry() -> tuple[Migration, ...]:
    return (
        Migration(
            id=BASELINE_MIGRATION_ID,
            title="Baseline current Shiki mirror layout",
            description="Record the existing post-P1.3.5 repository-local .shiki layout as the migration baseline without rewriting historical state.",
            introduced_in="T-0045",
            affected_paths=(
                ".shiki/manifest.json",
                ".shiki/tasks",
                ".shiki/goals",
                ".shiki/ledger",
                MIGRATION_STATE_PATH,
            ),
            destructive=False,
            apply=_baseline_apply,
        ),
    )


def migration_ids() -> tuple[str, ...]:
    return tuple(migration.id for migration in migration_registry())


def validate_migration_registry(registry: tuple[Migration, ...] | None = None) -> list[str]:
    registry = migration_registry() if registry is None else registry
    errors: list[str] = []
    ids = [migration.id for migration in registry]
    if ids != sorted(ids):
        errors.append("migration registry IDs must be sorted deterministically")
    seen: set[str] = set()
    for migration in registry:
        if not MIGRATION_ID_RE.match(migration.id):
            errors.append(f"{migration.id}: invalid migration id format")
        if migration.id in seen:
            errors.append(f"{migration.id}: duplicate migration id")
        seen.add(migration.id)
        for required in migration.requires:
            if required not in ids:
                errors.append(f"{migration.id}: unknown dependency {required}")
            elif ids.index(required) > ids.index(migration.id):
                errors.append(f"{migration.id}: dependency {required} must appear before dependent migration")
    return errors


def _state_path(root: Path) -> Path:
    return root / MIGRATION_STATE_PATH


def _default_state() -> dict[str, Any]:
    return {
        "version": MIGRATION_STATE_VERSION,
        "source_of_truth": MIGRATION_SOURCE_OF_TRUTH,
        "applied": [],
    }


def load_migration_state(root: Path) -> dict[str, Any]:
    path = _state_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise MigrationError(f"{MIGRATION_STATE_PATH}: migration state file is missing") from error
    except json.JSONDecodeError as error:
        raise MigrationError(f"{MIGRATION_STATE_PATH}: invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise MigrationError(f"{MIGRATION_STATE_PATH}: migration state must be a JSON object")
    return data


def _validate_record(index: int, record: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return [f"applied[{index}] must be an object"]
    for field_name in ("id", "status", "applied_at", "actor", "summary"):
        if not isinstance(record.get(field_name), str) or not record[field_name]:
            errors.append(f"applied[{index}].{field_name} must be a non-empty string")
    if "evidence" in record:
        evidence = record["evidence"]
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            errors.append(f"applied[{index}].evidence must be an array of strings")
    if isinstance(record.get("id"), str) and not MIGRATION_ID_RE.match(record["id"]):
        errors.append(f"applied[{index}].id has invalid migration id format: {record['id']}")
    if record.get("status") != "applied":
        errors.append(f"applied[{index}].status must be applied")
    return errors


def validate_migration_state_data(data: dict[str, Any], registry: tuple[Migration, ...] | None = None) -> list[str]:
    registry = migration_registry() if registry is None else registry
    errors = validate_migration_registry(registry)
    known_ids = {migration.id for migration in registry}
    if data.get("version") != MIGRATION_STATE_VERSION:
        errors.append(f"{MIGRATION_STATE_PATH}: version must be {MIGRATION_STATE_VERSION}")
    if data.get("source_of_truth") != MIGRATION_SOURCE_OF_TRUTH:
        errors.append(f"{MIGRATION_STATE_PATH}: source_of_truth must match canonical text")
    applied = data.get("applied")
    if not isinstance(applied, list):
        errors.append(f"{MIGRATION_STATE_PATH}: applied must be an array")
        return errors
    seen: set[str] = set()
    for index, record in enumerate(applied):
        errors.extend(_validate_record(index, record))
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            continue
        migration_id = record["id"]
        if migration_id in seen:
            errors.append(f"{MIGRATION_STATE_PATH}: duplicate applied migration {migration_id}")
        seen.add(migration_id)
        if migration_id not in known_ids:
            errors.append(f"{MIGRATION_STATE_PATH}: unknown applied migration {migration_id}")
    if BASELINE_MIGRATION_ID not in seen:
        errors.append(f"{MIGRATION_STATE_PATH}: baseline migration {BASELINE_MIGRATION_ID} must be applied")
    return errors


def _applied_ids(data: dict[str, Any]) -> set[str]:
    applied = data.get("applied")
    if not isinstance(applied, list):
        return set()
    return {record["id"] for record in applied if isinstance(record, dict) and isinstance(record.get("id"), str)}


def migration_status(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    registry = migration_registry()
    registry_ids = [migration.id for migration in registry]
    errors: list[str] = validate_migration_registry(registry)
    warnings: list[str] = []
    try:
        state = load_migration_state(root)
    except MigrationError as error:
        state = _default_state()
        errors.append(str(error))
        state_exists = False
    else:
        state_exists = True
        errors.extend(validate_migration_state_data(state, registry))
    applied_ids = _applied_ids(state)
    unknown_applied = sorted(applied_ids - set(registry_ids))
    pending = [migration.id for migration in registry if migration.id not in applied_ids]
    if unknown_applied:
        warnings.append(f"Unknown applied migrations: {', '.join(unknown_applied)}")
    return {
        "version": MIGRATION_STATE_VERSION,
        "target": str(root),
        "state_path": MIGRATION_STATE_PATH,
        "state_exists": state_exists,
        "source_of_truth": MIGRATION_SOURCE_OF_TRUTH,
        "registry_ids": registry_ids,
        "applied": [record for record in state.get("applied", []) if isinstance(record, dict)],
        "applied_count": len(applied_ids & set(registry_ids)),
        "pending": pending,
        "pending_count": len(pending),
        "unknown_applied": unknown_applied,
        "warnings": warnings,
        "errors": errors,
        "valid": not errors,
    }


def pending_migrations(root: Path) -> list[Migration]:
    status = migration_status(root)
    pending_ids = set(status["pending"])
    return [migration for migration in migration_registry() if migration.id in pending_ids]


def _select_migrations(registry: tuple[Migration, ...], requested: list[str] | None) -> list[Migration]:
    by_id = {migration.id: migration for migration in registry}
    if requested:
        unknown = [migration_id for migration_id in requested if migration_id not in by_id]
        if unknown:
            raise MigrationError(f"Unknown migration id: {', '.join(unknown)}")
        selected: list[Migration] = []
        visiting: set[str] = set()

        def visit(migration_id: str) -> None:
            if migration_id in visiting:
                raise MigrationError(f"Migration dependency cycle includes {migration_id}")
            if any(migration.id == migration_id for migration in selected):
                return
            visiting.add(migration_id)
            migration = by_id[migration_id]
            for required in migration.requires:
                visit(required)
            visiting.remove(migration_id)
            selected.append(migration)

        for migration_id in requested:
            visit(migration_id)
        return selected
    return list(registry)


def _write_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(encoded)
        temporary = Path(handle.name)
    temporary.replace(path)


def apply_migrations(
    root: Path,
    *,
    dry_run: bool = True,
    migration_ids: list[str] | None = None,
    i_understand: bool = False,
    actor: str = "shiki-cli",
    registry: tuple[Migration, ...] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    registry = migration_registry() if registry is None else registry
    registry_errors = validate_migration_registry(registry)
    if registry_errors:
        raise MigrationError("; ".join(registry_errors))
    selected = _select_migrations(registry, migration_ids)
    destructive = [migration.id for migration in selected if migration.destructive]
    if destructive and not i_understand:
        raise MigrationError(f"Destructive migrations require --i-understand: {', '.join(destructive)}")
    try:
        state = load_migration_state(root)
    except MigrationError:
        state = _default_state()
    state_errors = [error for error in validate_migration_state_data(state, registry) if "baseline migration" not in error]
    if state_errors:
        raise MigrationError("; ".join(state_errors))
    applied_ids = _applied_ids(state)
    to_apply = [migration for migration in selected if migration.id not in applied_ids]
    intended_writes = [MIGRATION_STATE_PATH] if to_apply or not _state_path(root).exists() else []
    planned: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for migration in to_apply:
        result = migration.apply(root, dry_run) if migration.apply else {"summary": migration.description, "evidence": []}
        evidence = tuple(str(item) for item in result.get("evidence", []))
        summary = str(result.get("summary", migration.description))
        planned.append(
            {
                "id": migration.id,
                "title": migration.title,
                "destructive": migration.destructive,
                "affected_paths": list(migration.affected_paths),
                "summary": summary,
                "evidence": list(evidence),
            }
        )
        records.append(
            asdict(
                MigrationRecord(
                    id=migration.id,
                    status="applied",
                    applied_at=utc_now(),
                    actor=actor,
                    summary=summary,
                    evidence=evidence,
                )
            )
        )
    if not dry_run and records:
        applied = list(state.get("applied", []))
        applied.extend(records)
        state = {
            "version": MIGRATION_STATE_VERSION,
            "source_of_truth": MIGRATION_SOURCE_OF_TRUTH,
            "applied": applied,
        }
        _write_state(_state_path(root), state)
    return {
        "dry_run": dry_run,
        "target": str(root),
        "state_path": MIGRATION_STATE_PATH,
        "intended_writes": intended_writes,
        "planned": planned,
        "applied": [] if dry_run else [record["id"] for record in records],
        "pending_after": migration_status(root)["pending"] if not dry_run else [migration.id for migration in to_apply],
        "message": "dry-run: no migration state was mutated" if dry_run else "migrations applied",
    }


def _print_status(status: dict[str, Any]) -> None:
    print("Shiki migrations")
    print(f"target: {status['target']}")
    print(f"state: {status['state_path']} ({'present' if status['state_exists'] else 'missing'})")
    print(f"applied: {status['applied_count']}")
    print(f"pending: {status['pending_count']}")
    for migration_id in status["pending"]:
        print(f"- pending: {migration_id}")
    for warning in status["warnings"]:
        print(f"warning: {warning}")
    for error in status["errors"]:
        print(f"error: {error}")


def cmd_migrate(args: argparse.Namespace) -> int:
    target = Path(getattr(args, "target", ".")).expanduser().resolve()
    command = getattr(args, "migrate_command", None)
    if command == "status":
        status = migration_status(target)
        if args.json:
            print_json(status)
        else:
            _print_status(status)
        return 0 if status["valid"] else 1
    if command == "plan":
        result = apply_migrations(target, dry_run=True, migration_ids=getattr(args, "migration", None), i_understand=True)
        print_json(result) if args.json else _print_plan(result)
        return 0
    if command == "apply":
        execute = bool(args.execute or args.i_understand)
        result = apply_migrations(
            target,
            dry_run=not execute,
            migration_ids=getattr(args, "migration", None),
            i_understand=bool(args.i_understand),
        )
        print_json(result) if args.json else _print_plan(result)
        return 0
    raise MigrationError(f"unknown migrate command: {command}")


def _print_plan(result: dict[str, Any]) -> None:
    print("Shiki migration plan")
    print(f"target: {result['target']}")
    print(f"mode: {'dry-run' if result['dry_run'] else 'execute'}")
    print(f"message: {result['message']}")
    if result["intended_writes"]:
        print("intended writes:")
        for relative in result["intended_writes"]:
            print(f"- {relative}")
    if result["planned"]:
        print("migrations:")
        for item in result["planned"]:
            print(f"- {item['id']}: {item['summary']}")
    else:
        print("migrations: none")
