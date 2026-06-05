"""Canonical Shiki mirror manifest helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MANIFEST_PATH = ".shiki/manifest.json"
README_LAYOUT_START = "<!-- SHIKI_MANIFEST_LAYOUT_START -->"
README_LAYOUT_END = "<!-- SHIKI_MANIFEST_LAYOUT_END -->"


class ManifestError(Exception):
    """Raised when the Shiki manifest cannot be loaded."""


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError(f"{path}: manifest is missing") from error
    except json.JSONDecodeError as error:
        raise ManifestError(f"{path}: invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: manifest must be a JSON object")
    return data


def _mapping(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    value = manifest.get(key, {})
    return value if isinstance(value, dict) else {}


def _string_list(manifest: dict[str, Any], *keys: str) -> list[str]:
    value: Any = manifest
    for key in keys:
        if not isinstance(value, dict):
            return []
        value = value.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def manifest_directories(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    directories: dict[str, dict[str, Any]] = {}
    for section in ("directories", "runtime_directories"):
        for path, metadata in _mapping(manifest, section).items():
            if isinstance(path, str) and isinstance(metadata, dict):
                directories[path] = metadata
    return directories


def manifest_tracked_directories(manifest: dict[str, Any]) -> list[str]:
    return [
        path
        for path, metadata in manifest_directories(manifest).items()
        if metadata.get("tracked") is True
    ]


def manifest_runtime_directories(manifest: dict[str, Any]) -> list[str]:
    return sorted(_mapping(manifest, "runtime_directories"))


def manifest_required_directories(manifest: dict[str, Any]) -> list[str]:
    return [
        path
        for path, metadata in manifest_directories(manifest).items()
        if metadata.get("required") is True
    ]


def manifest_required_files(manifest: dict[str, Any]) -> list[str]:
    return [
        path
        for path, metadata in _mapping(manifest, "files").items()
        if isinstance(metadata, dict) and metadata.get("required") is True
    ]


def manifest_create_directories(manifest: dict[str, Any]) -> list[str]:
    return _string_list(manifest, "install", "create_directories")


def manifest_install_include(manifest: dict[str, Any]) -> list[str]:
    return _string_list(manifest, "install", "include")


def manifest_exclude_from_commit(manifest: dict[str, Any]) -> list[str]:
    return _string_list(manifest, "install", "exclude_from_commit")


def render_manifest_layout(manifest: dict[str, Any]) -> str:
    rows = [
        README_LAYOUT_START,
        "| Path | Kind | State Class | Tracked | Required | Description |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for path, metadata in sorted(manifest_directories(manifest).items()):
        kind = str(metadata.get("kind", "unknown"))
        state_class = str(metadata.get("state_class", "unknown"))
        tracked = "yes" if metadata.get("tracked") is True else "no"
        required = "yes" if metadata.get("required") is True else "no"
        description = str(metadata.get("description", "")).replace("\n", " ")
        rows.append(f"| `{path}` | {kind} | {state_class} | {tracked} | {required} | {description} |")
    rows.append(README_LAYOUT_END)
    return "\n".join(rows)
