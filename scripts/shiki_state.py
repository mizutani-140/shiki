#!/usr/bin/env python3
"""Atomic state helpers for Shiki control-plane JSON records."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_control_id(prefix: str) -> str:
    """Return a sortable, filename-safe, collision-resistant Shiki state ID."""
    if not prefix or not all(part.isalnum() and part.isupper() for part in prefix.split("-")):
        raise ValueError(f"invalid Shiki ID prefix: {prefix!r}")
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}Z"
    return f"{prefix}-{timestamp}-{secrets.token_hex(4)}"


def json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _write_temp_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json_text(payload))
            handle.flush()
            os.fsync(handle.fileno())
        return temp_path
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_create_json(path: Path, payload: dict[str, Any]) -> None:
    """Create a JSON file only if it does not exist. Never overwrite."""
    temp_path = _write_temp_json(path, payload)
    try:
        os.link(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_replace_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON through a temp file and atomically replace the destination."""
    temp_path = _write_temp_json(path, payload)
    try:
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def append_ledger_entry(
    target: Path,
    payload_factory: Callable[[str], dict[str, Any]],
    *,
    retries: int = 10,
) -> str:
    """Append a ledger entry using no-overwrite creation and collision retry."""
    ledger_dir = target / ".shiki" / "ledger"
    for _ in range(retries):
        ledger_id = new_control_id("L")
        payload = payload_factory(ledger_id)
        payload["id"] = ledger_id
        try:
            atomic_create_json(ledger_dir / f"{ledger_id}.json", payload)
            return ledger_id
        except FileExistsError:
            continue
    raise FileExistsError(f"could not allocate a unique ledger id after {retries} attempts")
