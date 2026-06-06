#!/usr/bin/env python3
"""Dependency-free Shiki config parsing helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

def parse_config_scalar(value: str) -> Any:
    value = value.strip().strip("\"'")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def load_shiki_config(target: Path) -> dict[str, dict[str, Any]]:
    """Read the small .shiki/config.yaml subset bootstrap owns."""
    config_path = target / ".shiki" / "config.yaml"
    if not config_path.exists():
        return {}

    config: dict[str, dict[str, Any]] = {}
    section: str | None = None
    key: str | None = None
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if indent == 0:
            section = stripped[:-1] if stripped.endswith(":") else None
            key = None
            if section:
                config.setdefault(section, {})
            continue
        if section is None:
            continue
        if indent == 2:
            if stripped.endswith(":"):
                key = stripped[:-1]
                config[section].setdefault(key, [])
                continue
            if ":" in stripped:
                item_key, value = stripped.split(":", 1)
                config[section][item_key.strip()] = parse_config_scalar(value)
                key = None
                continue
        if indent >= 4 and key and stripped.startswith("- "):
            values = config[section].setdefault(key, [])
            if isinstance(values, list):
                values.append(parse_config_scalar(stripped[2:]))
    return config


def configured_required_review(target: Path) -> bool:
    value = load_shiki_config(target).get("defaults", {}).get("required_review")
    if isinstance(value, bool):
        return value
    return True


def branch_protection_review_count(target: Path) -> int:
    return 1 if configured_required_review(target) else 0


def configured_required_checks(target: Path, default: "list[str] | tuple[str, ...]") -> list[str]:
    """Required status-check contexts derived from .shiki/config.yaml.

    Canonical, config-first source for branch-protection setup. ``default`` is the
    documented fallback (DEFAULT_REQUIRED_CHECKS), used only when the target has no
    ``mergegate.required_checks`` entries (e.g. before config is installed).
    """
    mergegate = load_shiki_config(target).get("mergegate", {})
    raw = mergegate.get("required_checks") if isinstance(mergegate, dict) else None
    checks = [str(check) for check in raw or [] if str(check).strip()]
    return checks or list(default)
