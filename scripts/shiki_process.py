#!/usr/bin/env python3
"""Shared process, path, JSON, and console helpers for Shiki.

This module is intentionally standard-library only and has no import-time side
effects beyond constant definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from shiki_manifest import load_manifest, manifest_create_directories, manifest_directories
from shiki_state import atomic_replace_json

ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG = Path.home() / ".shiki" / "config.json"
DEFAULT_ENGINEERING_SKILLS_DIRS = [
    "~/Documents/lead-os/skills/engineering",
    "~/skills/skills/engineering",
]

@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class ShikiError(Exception):
    pass


def run(
    args: list[str],
    *,
    cwd: Path = ROOT,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> CommandResult:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    process = subprocess.run(
        args,
        cwd=str(cwd),
        input=input_text,
        text=True,
        capture_output=True,
        env=process_env,
        check=False,
    )
    result = CommandResult(args, process.returncode, process.stdout, process.stderr)
    if check and process.returncode != 0:
        command = " ".join(args)
        raise ShikiError(f"{command} failed\n{process.stderr.strip()}")
    return result


def info(message: str) -> None:
    print(f"[shiki] {message}")


def warn(message: str) -> None:
    print(f"[shiki] warning: {message}", file=sys.stderr)


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise ShikiError(f"required tool not found: {name}")


def validate_local_shiki() -> None:
    run(["python3", "scripts/validate_shiki.py"], cwd=ROOT)
    info("local Shiki validation passed")


def validate_target_shiki(target: Path) -> None:
    run(["python3", "scripts/validate_shiki.py"], cwd=target)
    info("target Shiki validation passed")


def save_default_config(repo: str, branch: str) -> None:
    LOCAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "repo": repo,
        "default_branch": branch,
        "shiki_root": str(ROOT),
    }
    LOCAL_CONFIG.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    info(f"saved defaults to {LOCAL_CONFIG}")


def load_default_config() -> dict[str, str]:
    if not LOCAL_CONFIG.exists():
        return {}
    return json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def target_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def shiki_path(target: Path, *parts: str) -> Path:
    return target / ".shiki" / Path(*parts)


def ensure_control_dirs(target: Path) -> None:
    manifest = load_manifest(target) if (target / ".shiki" / "manifest.json").exists() else load_manifest(ROOT)
    directories = manifest_directories(manifest)
    for relative in manifest_create_directories(manifest):
        directory = target / relative
        directory.mkdir(parents=True, exist_ok=True)
        metadata = directories.get(relative, {})
        if metadata.get("tracked") is True and metadata.get("required") is True and not any(directory.iterdir()):
            (directory / ".gitkeep").write_text("", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ShikiError(f"missing file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ShikiError(f"expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_replace_json(path, data)


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def prompt_value(label: str, current: str | None = None, *, required: bool = True) -> str:
    if current:
        return current
    if not sys.stdin.isatty():
        if required:
            raise ShikiError(f"missing {label}; pass it as an option or use --answers-file")
        return ""
    while True:
        value = input(f"{label}: ").strip()
        if value or not required:
            return value


def prompt_default(label: str, default: str) -> str:
    if not sys.stdin.isatty():
        return default
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def prompt_list(label: str, current: list[str] | None = None) -> list[str]:
    if current:
        return current
    if not sys.stdin.isatty():
        return []
    print(f"{label}: enter one item per line, then an empty line.")
    values: list[str] = []
    while True:
        value = input("> ").strip()
        if not value:
            break
        values.append(value)
    return values


def default_engineering_skills_dir() -> str:
    configured = os.environ.get("SHIKI_ENGINEERING_SKILLS_DIR")
    if configured:
        return configured
    for candidate in DEFAULT_ENGINEERING_SKILLS_DIRS:
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return DEFAULT_ENGINEERING_SKILLS_DIRS[0]


def resolve_engineering_skills_dir(value: str | None) -> str:
    skills_dir = value or default_engineering_skills_dir()
    if value and not Path(value).expanduser().exists():
        raise ShikiError(f"engineering skills directory does not exist: {value}")
    return skills_dir


def start_target_value(args: argparse.Namespace) -> str:
    positional = getattr(args, "target_positional", None)
    option = getattr(args, "target", ".")
    if positional and option != "." and Path(positional).expanduser().resolve() != Path(option).expanduser().resolve():
        raise ShikiError("pass the target repository either positionally or with --target, not both")
    return positional or option


def first_line(value: str) -> str:
    return value.strip().splitlines()[0] if value.strip() else ""
