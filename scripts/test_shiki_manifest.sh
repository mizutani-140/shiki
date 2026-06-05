#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

import shiki
from shiki_manifest import (
    MANIFEST_PATH,
    load_manifest,
    manifest_create_directories,
    manifest_directories,
    manifest_required_files,
    render_manifest_layout,
)
from validate_shiki import ValidationError, validate_shiki_manifest


ROOT = Path.cwd()
BASE_MANIFEST = load_manifest(ROOT)


def run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_fixture(root: Path, manifest: dict[str, object] | None = None) -> None:
    manifest = copy.deepcopy(manifest or BASE_MANIFEST)
    write_json(root / MANIFEST_PATH, manifest)

    for relative in manifest_directories(manifest):
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        if manifest_directories(manifest)[relative].get("tracked") is True:
            (directory / ".gitkeep").write_text("", encoding="utf-8")

    for relative in manifest_required_files(manifest):
        path = root / relative
        if relative == MANIFEST_PATH:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == ".shiki/README.md":
            classes = "\n".join(f"- `{state_class}`" for state_class in manifest.get("state_classes", {}))
            path.write_text(
                "# .shiki Mirror\n\n"
                "## Layout\n\n"
                f"{render_manifest_layout(manifest)}\n\n"
                "## State Classes\n\n"
                f"{classes}\n",
                encoding="utf-8",
            )
        else:
            path.write_text("fixture\n", encoding="utf-8")

    docs = root / "docs" / "agents"
    docs.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "docs" / "agents").glob("*.md"):
        shutil.copy2(source, docs / source.name)

    run(["git", "init", "-b", "main"], root)


def expect_pass(name: str, callback) -> None:
    try:
        callback()
    except ValidationError as error:
        raise SystemExit(f"{name}: expected pass, got {error}") from error


def expect_fail(name: str, callback, expected: str) -> None:
    try:
        callback()
    except ValidationError as error:
        if expected not in str(error):
            raise SystemExit(f"{name}: expected {expected!r}, got {error}") from error
        return
    raise SystemExit(f"{name}: expected failure")


def with_fixture(callback, manifest: dict[str, object] | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="shiki-manifest-test-") as tmp:
        root = Path(tmp)
        write_fixture(root, manifest)
        callback(root)


with_fixture(lambda root: expect_pass("valid manifest", lambda: validate_shiki_manifest(root)))

with_fixture(
    lambda root: (
        (root / MANIFEST_PATH).unlink(),
        expect_fail("missing manifest", lambda: validate_shiki_manifest(root), "manifest is missing"),
    )
)

with_fixture(
    lambda root: (
        (root / MANIFEST_PATH).write_text("{not json", encoding="utf-8"),
        expect_fail("invalid JSON", lambda: validate_shiki_manifest(root), "invalid JSON"),
    )
)

outside_manifest = copy.deepcopy(BASE_MANIFEST)
outside_manifest["directories"]["docs/outside"] = {
    "kind": "bad",
    "tracked": True,
    "required": True,
    "description": "outside .shiki",
}
with_fixture(
    lambda root: expect_fail("outside directory", lambda: validate_shiki_manifest(root), "must start with .shiki/"),
    outside_manifest,
)

with_fixture(
    lambda root: (
        shutil.rmtree(root / ".shiki" / "goals"),
        expect_fail("missing required directory", lambda: validate_shiki_manifest(root), "required directory .shiki/goals is missing"),
    )
)

with_fixture(
    lambda root: (
        (root / ".shiki" / "gha" / "forged.json").write_text("{}", encoding="utf-8"),
        run(["git", "add", "--", ".shiki/gha/forged.json"], root),
        expect_fail("runtime committed", lambda: validate_shiki_manifest(root), "runtime-only directory .shiki/gha has tracked files"),
    )
)

with_fixture(
    lambda root: (
        (root / ".shiki" / "config.yaml").unlink(),
        expect_fail("missing required file", lambda: validate_shiki_manifest(root), "required file .shiki/config.yaml is missing"),
    )
)

with_fixture(
    lambda root: (
        (root / ".shiki" / "README.md").write_text(
            (root / ".shiki" / "README.md").read_text(encoding="utf-8").replace(".shiki/tasks", ".shiki/taskz"),
            encoding="utf-8",
        ),
        expect_fail("README missing manifest directory", lambda: validate_shiki_manifest(root), "manifest layout block is out of sync"),
    )
)

with_fixture(
    lambda root: (
        (root / ".shiki" / "README.md").write_text(
            (root / ".shiki" / "README.md").read_text(encoding="utf-8").replace(
                "<!-- SHIKI_MANIFEST_LAYOUT_END -->",
                "| `.shiki/unknown` | mirror | yes | yes | Unknown. |\n<!-- SHIKI_MANIFEST_LAYOUT_END -->",
            ),
            encoding="utf-8",
        ),
        expect_fail("README unknown directory", lambda: validate_shiki_manifest(root), "manifest layout block is out of sync"),
    )
)

drift_manifest = copy.deepcopy(BASE_MANIFEST)
drift_manifest["install"]["create_directories"] = [
    path for path in manifest_create_directories(drift_manifest) if path != ".shiki/tasks"
]
with_fixture(
    lambda root: expect_fail("TARGET_STATE_DIRECTORIES drift", lambda: validate_shiki_manifest(root), "TARGET_STATE_DIRECTORIES"),
    drift_manifest,
)

with tempfile.TemporaryDirectory(prefix="shiki-manifest-install-") as tmp:
    target = Path(tmp)
    (target / ".shiki").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / MANIFEST_PATH, target / MANIFEST_PATH)
    shiki.ensure_control_dirs(target)
    for relative in manifest_create_directories(BASE_MANIFEST):
        if not (target / relative).is_dir():
            raise SystemExit(f"ensure_control_dirs did not create {relative}")

stage_paths = shiki.manifest_stage_paths(ROOT)
if ".shiki/manifest.json" not in stage_paths:
    raise SystemExit("manifest_stage_paths must include .shiki/manifest.json")
if any(path.startswith(".shiki/gha") for path in stage_paths):
    raise SystemExit("manifest_stage_paths must exclude .shiki/gha runtime evidence")

print("Shiki manifest tests passed")
PY
