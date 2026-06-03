"""Shared Shiki lock matching and conflict detection."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any


SHIKI_SEMANTIC_LOCKS: dict[str, list[str]] = {
    "shiki:state": [
        "path:.shiki/goals/**",
        "path:.shiki/tasks/**",
        "path:.shiki/ledger/**",
        "path:.shiki/locks/**",
        "path:.shiki/dag/**",
        "path:.shiki/reports/**",
    ],
    "shiki:governance": [
        "path:.shiki/config.yaml",
        "path:.shiki/policy.example.yaml",
        "path:.github/CODEOWNERS",
        "path:.github/workflows/**",
        "path:scripts/mergegate_check.py",
        "path:scripts/enforce_cca_verdict.py",
        "path:scripts/validate_shiki.py",
        "path:scripts/shiki_contracts.py",
    ],
    "shiki:workflows": [
        "path:.github/workflows/**",
    ],
    "shiki:contracts": [
        "path:AGENTS.md",
        "path:SYSTEM_PROMPT.md",
        "path:CLAUDE.md",
        "path:.codex/**",
        "path:.claude/**",
        "path:.github/prompts/**",
        "path:docs/agents/**",
        "path:scripts/shiki_contracts.py",
    ],
}


def normalize_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def split_lock(lock: str) -> tuple[str, str] | None:
    kind, separator, value = lock.partition(":")
    if not separator:
        return None
    kind = kind.strip().lower()
    value = value.strip()
    if not kind or not value:
        return None
    if kind == "path":
        value = normalize_path(value)
    elif kind == "shiki":
        value = value.lower()
    return kind, value


def normalize_lock(lock: str) -> str:
    parsed = split_lock(lock)
    if parsed is None:
        return lock.strip()
    return f"{parsed[0]}:{parsed[1]}"


def expand_lock(lock: str) -> list[str]:
    normalized = normalize_lock(lock)
    return SHIKI_SEMANTIC_LOCKS.get(normalized, [normalized])


def known_shiki_semantic_locks() -> set[str]:
    return set(SHIKI_SEMANTIC_LOCKS)


def is_glob_pattern(value: str) -> bool:
    return any(character in value for character in "*?[")


def path_lock_patterns(lock: str) -> list[str]:
    parsed = split_lock(lock)
    if parsed is None or parsed[0] != "path":
        return []
    pattern = parsed[1]
    patterns = [pattern]
    if pattern.endswith("/"):
        patterns.append(pattern + "**")
    elif pattern.endswith("/*"):
        patterns.append(pattern[:-1] + "**")
    return list(dict.fromkeys(patterns))


def path_matches_lock(path: str, lock: str) -> bool:
    normalized = normalize_path(path)
    for expanded_lock in expand_lock(lock):
        for pattern in path_lock_patterns(expanded_lock):
            if normalized == pattern or fnmatch.fnmatch(normalized, pattern):
                return True
    return False


def literal_prefix(value: str) -> str:
    indexes = [index for index in (value.find("*"), value.find("?"), value.find("[")) if index >= 0]
    return value if not indexes else value[: min(indexes)]


def path_patterns_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    left_prefix = literal_prefix(left).rstrip("*")
    right_prefix = literal_prefix(right).rstrip("*")
    left_directory = left if left.endswith("/") else left.rsplit("/", 1)[0] + "/" if "/" in left else left
    right_directory = right if right.endswith("/") else right.rsplit("/", 1)[0] + "/" if "/" in right else right
    candidates = {
        left,
        right,
        left_prefix.rstrip("/") or left,
        right_prefix.rstrip("/") or right,
        left_prefix + "sample.txt" if left_prefix.endswith("/") else left_prefix,
        right_prefix + "sample.txt" if right_prefix.endswith("/") else right_prefix,
        left_directory + "sample.txt" if left_directory.endswith("/") else left_directory,
        right_directory + "sample.txt" if right_directory.endswith("/") else right_directory,
    }
    for candidate in {normalize_path(value) for value in candidates if value}:
        if fnmatch.fnmatch(candidate, left) and fnmatch.fnmatch(candidate, right):
            return True
    if left_prefix and right_prefix and (left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)):
        return True
    if not is_glob_pattern(left) and not is_glob_pattern(right):
        return left.startswith(right.rstrip("/") + "/") or right.startswith(left.rstrip("/") + "/")
    return False


def semantic_values_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    if fnmatch.fnmatch(left, right) or fnmatch.fnmatch(right, left):
        return True
    left_prefix = literal_prefix(left)
    right_prefix = literal_prefix(right)
    return bool(left_prefix and right_prefix and (left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)))


def primitive_locks_overlap(left_lock: str, right_lock: str) -> bool:
    if left_lock == right_lock:
        return True
    left = split_lock(left_lock)
    right = split_lock(right_lock)
    if left is None or right is None or left[0] != right[0]:
        return False
    if left[0] == "path":
        return any(
            path_patterns_overlap(left_pattern, right_pattern)
            for left_pattern in path_lock_patterns(left_lock)
            for right_pattern in path_lock_patterns(right_lock)
        )
    return semantic_values_overlap(left[1], right[1])


def locks_overlap(left_lock: str, right_lock: str) -> bool:
    return any(
        primitive_locks_overlap(left, right)
        for left in expand_lock(left_lock)
        for right in expand_lock(right_lock)
    )


def load_lock_record(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def active_lock_conflicts(
    target: Path,
    task_id: str,
    locks: list[str],
    files: list[str] | None = None,
) -> list[str]:
    conflicts: list[str] = []
    directory = target / ".shiki" / "locks"
    if not directory.exists():
        return conflicts
    changed_files = [normalize_path(path) for path in files or []]
    for path in sorted(directory.glob("*.json")):
        record = load_lock_record(path)
        if not record or record.get("state") != "active" or record.get("task_id") == task_id:
            continue
        owner_task = str(record.get("task_id") or path.stem)
        for held_lock in [str(lock) for lock in record.get("locks") or []]:
            matching_files = [
                changed_file
                for changed_file in changed_files
                if path_matches_lock(changed_file, held_lock)
                and any(path_matches_lock(changed_file, requested_lock) for requested_lock in locks)
            ]
            if any(locks_overlap(held_lock, requested_lock) for requested_lock in locks):
                conflicts.append(f"Lock conflict: {held_lock} held by {owner_task}")
            elif matching_files:
                conflicts.append(f"Lock conflict: {held_lock} held by {owner_task} overlaps {matching_files[0]}")
    return conflicts


def files_outside_locks(files: list[str], locks: list[str]) -> list[str]:
    return [path for path in files if not any(path_matches_lock(path, lock) for lock in locks)]
