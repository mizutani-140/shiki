"""State class helpers for repository-local Shiki state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

STATE_CLASS_FIELD = "state_class"
UNKNOWN_STATE_CLASS = "unknown"


def normalize_shiki_path(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/") if normalized != ".shiki" else normalized


def _mapping(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    value = manifest.get(key)
    return value if isinstance(value, dict) else {}


def manifest_state_classes(manifest: dict[str, Any]) -> dict[str, Any]:
    return _mapping(manifest, "state_classes")


def manifest_state_class_policies(manifest: dict[str, Any]) -> dict[str, Any]:
    return _mapping(manifest, "state_class_policies")


def _manifest_entries(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    directories: dict[str, Any] = {}
    for section in ("directories", "runtime_directories"):
        for path, metadata in _mapping(manifest, section).items():
            if isinstance(path, str) and isinstance(metadata, dict):
                directories[normalize_shiki_path(path)] = metadata
    files: dict[str, Any] = {}
    for path, metadata in _mapping(manifest, "files").items():
        if isinstance(path, str) and isinstance(metadata, dict):
            files[normalize_shiki_path(path)] = metadata
    return directories, files


def classify_shiki_path(path: str, manifest: dict[str, Any]) -> str:
    normalized = normalize_shiki_path(path)
    if not normalized.startswith(".shiki/"):
        return UNKNOWN_STATE_CLASS

    directories, files = _manifest_entries(manifest)
    file_metadata = files.get(normalized)
    if file_metadata is not None:
        return str(file_metadata.get(STATE_CLASS_FIELD) or UNKNOWN_STATE_CLASS)

    best_path = ""
    best_metadata: dict[str, Any] | None = None
    for directory, metadata in directories.items():
        if normalized == directory or normalized.startswith(f"{directory}/"):
            if len(directory) > len(best_path):
                best_path = directory
                best_metadata = metadata
    if best_metadata is None:
        return UNKNOWN_STATE_CLASS
    return str(best_metadata.get(STATE_CLASS_FIELD) or UNKNOWN_STATE_CLASS)


def class_policy(state_class: str, manifest: dict[str, Any]) -> dict[str, Any]:
    policy = manifest_state_class_policies(manifest).get(state_class)
    return policy if isinstance(policy, dict) else {}


def _is_class(path: str, manifest: dict[str, Any], state_class: str) -> bool:
    return classify_shiki_path(path, manifest) == state_class


def is_runtime_only(path: str, manifest: dict[str, Any]) -> bool:
    state_class = classify_shiki_path(path, manifest)
    policy = class_policy(state_class, manifest)
    return state_class in {"workflow-runtime-evidence", "cache", "local-only"} or policy.get("tracked") is False


def is_append_only_evidence(path: str, manifest: dict[str, Any]) -> bool:
    return _is_class(path, manifest, "append-only-evidence")


def is_governance_policy(path: str, manifest: dict[str, Any]) -> bool:
    return _is_class(path, manifest, "governance-policy")


def is_mirror(path: str, manifest: dict[str, Any]) -> bool:
    return _is_class(path, manifest, "mirror")


def state_class_summary(manifest: dict[str, Any]) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {state_class: [] for state_class in manifest_state_classes(manifest)}
    directories, files = _manifest_entries(manifest)
    for path, metadata in {**directories, **files}.items():
        state_class = str(metadata.get(STATE_CLASS_FIELD) or UNKNOWN_STATE_CLASS)
        summary.setdefault(state_class, []).append(path)
    return {state_class: sorted(paths) for state_class, paths in sorted(summary.items())}


def unknown_tracked_shiki_paths(root: Path, manifest: dict[str, Any], tracked_paths: list[str]) -> list[str]:
    unknown: list[str] = []
    for path in tracked_paths:
        normalized = normalize_shiki_path(path)
        if normalized.startswith(".shiki/") and classify_shiki_path(normalized, manifest) == UNKNOWN_STATE_CLASS:
            unknown.append(normalized)
    return sorted(set(unknown))
