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

from mergegate_check import ChangedFile, enforce_untrusted_shiki_mutations
from shiki_locks import files_outside_locks
from shiki_manifest import MANIFEST_PATH, load_manifest, manifest_directories, manifest_required_files, render_manifest_layout
from shiki_migrations import STATE_CLASSES_MIGRATION_ID, migration_status
from shiki_state_classes import classify_shiki_path
from shiki_doctor import _state_class_findings
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
    for relative, metadata in manifest_directories(manifest).items():
        directory = root / relative
        directory.mkdir(parents=True, exist_ok=True)
        if metadata.get("tracked") is True:
            (directory / ".gitkeep").write_text("", encoding="utf-8")
    for relative in manifest_required_files(manifest):
        path = root / relative
        if relative == MANIFEST_PATH:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == ".shiki/README.md":
            classes = "\n".join(f"- `{state_class}`" for state_class in manifest.get("state_classes", {}))
            path.write_text(
                "# .shiki Mirror\n\n## Layout\n\n"
                + render_manifest_layout(manifest)
                + "\n\n## State Classes\n\n"
                + classes
                + "\n",
                encoding="utf-8",
            )
        else:
            path.write_text("{}\n" if relative.endswith(".json") else "fixture\n", encoding="utf-8")
    docs = root / "docs" / "agents"
    docs.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / "docs" / "agents").glob("*.md"):
        shutil.copy2(source, docs / source.name)
    run(["git", "init", "-b", "main"], root)


def expect_fail(name: str, callback, expected: str) -> None:
    try:
        callback()
    except ValidationError as error:
        if expected not in str(error):
            raise SystemExit(f"{name}: expected {expected!r}, got {error}") from error
        return
    raise SystemExit(f"{name}: expected failure")


def with_fixture(callback, manifest: dict[str, object] | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="shiki-state-classes-") as tmp:
        root = Path(tmp)
        write_fixture(root, manifest)
        callback(root)


assert classify_shiki_path(".shiki/goals/G-0012.json", BASE_MANIFEST) == "mirror"
assert classify_shiki_path(".shiki/tasks/T-0048.json", BASE_MANIFEST) == "mirror"
assert classify_shiki_path(".shiki/ledger/L-test.json", BASE_MANIFEST) == "append-only-evidence"
assert classify_shiki_path(".shiki/guardian-policy.json", BASE_MANIFEST) == "governance-policy"
assert classify_shiki_path(".shiki/migrations/state.json", BASE_MANIFEST) == "migration-state"
assert classify_shiki_path(".shiki/gha/cca-verdict.json", BASE_MANIFEST) == "workflow-runtime-evidence"
assert classify_shiki_path(".shiki/unknown/file.json", BASE_MANIFEST) == "unknown"

with_fixture(lambda root: validate_shiki_manifest(root))

missing_classes = copy.deepcopy(BASE_MANIFEST)
missing_classes.pop("state_classes", None)
with_fixture(lambda root: expect_fail("missing state_classes", lambda: validate_shiki_manifest(root), "state_classes must be defined"), missing_classes)

missing_entry_class = copy.deepcopy(BASE_MANIFEST)
missing_entry_class["directories"][".shiki/goals"].pop("state_class", None)
with_fixture(lambda root: expect_fail("missing entry state_class", lambda: validate_shiki_manifest(root), ".shiki/goals must declare state_class"), missing_entry_class)

unknown_class = copy.deepcopy(BASE_MANIFEST)
unknown_class["directories"][".shiki/goals"]["state_class"] = "not-a-class"
with_fixture(lambda root: expect_fail("unknown state_class", lambda: validate_shiki_manifest(root), "unknown state_class"), unknown_class)

runtime_tracked = copy.deepcopy(BASE_MANIFEST)
runtime_tracked["runtime_directories"][".shiki/gha"]["tracked"] = True
with_fixture(lambda root: expect_fail("runtime tracked", lambda: validate_shiki_manifest(root), "must not be tracked"), runtime_tracked)

gha_wrong_class = copy.deepcopy(BASE_MANIFEST)
gha_wrong_class["runtime_directories"][".shiki/gha"]["state_class"] = "mirror"
with_fixture(lambda root: expect_fail("gha wrong class", lambda: validate_shiki_manifest(root), ".shiki/gha must classify as workflow-runtime-evidence"), gha_wrong_class)

with_fixture(
    lambda root: (
        (root / "docs" / "agents" / "state-classes.md").write_text("missing docs\n", encoding="utf-8"),
        expect_fail("docs missing state class mention", lambda: validate_shiki_manifest(root), "missing state class documentation marker"),
    )
)


def mergegate_reasons(entries: list[ChangedFile], *, target: Path | None = None, task: dict[str, object] | None = None) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="shiki-state-mergegate-") as tmp:
        root = target or Path(tmp)
        (root / ".shiki" / "ledger").mkdir(parents=True, exist_ok=True)
        (root / ".shiki" / "tasks").mkdir(parents=True, exist_ok=True)
        (root / ".shiki" / "goals").mkdir(parents=True, exist_ok=True)
        default_task = {
            "id": "T-0048",
            "goal_id": "G-0012",
            "ledger_evidence": ["L-test"],
            "locks": ["path:.shiki/tasks/T-0048.json"],
        }
        default_task.update(task or {})
        ledger = {"id": "L-test", "task_id": "T-0048", "goal_id": "G-0012"}
        write_json(root / ".shiki" / "ledger" / "L-test.json", ledger)
        blocking: list[str] = []
        warnings: list[str] = []
        enforce_untrusted_shiki_mutations(
            target=root,
            manifest=BASE_MANIFEST,
            base_shiki=None,
            changed_files_status=entries,
            task=default_task,
            goal_id="G-0012",
            task_id="T-0048",
            pr={"number": 94},
            blocking=blocking,
            warnings=warnings,
        )
        return blocking


assert any("state_class=workflow-runtime-evidence" in reason for reason in mergegate_reasons([ChangedFile("A", ".shiki/gha/cca-verdict.json")]))
assert any("state_class=unknown" in reason for reason in mergegate_reasons([ChangedFile("A", ".shiki/unknown/file.json")]))
assert not mergegate_reasons([ChangedFile("A", ".shiki/ledger/L-test.json")])
assert any("state_class=append-only-evidence" in reason for reason in mergegate_reasons([ChangedFile("M", ".shiki/ledger/L-test.json")]))
assert any("state_class=append-only-evidence" in reason for reason in mergegate_reasons([ChangedFile("D", ".shiki/ledger/L-test.json")]))
assert not mergegate_reasons([ChangedFile("M", ".shiki/tasks/T-0048.json")])
assert any("unrelated Shiki task" in reason and "state_class=mirror" in reason for reason in mergegate_reasons([ChangedFile("M", ".shiki/tasks/T-0047.json")]))
assert not mergegate_reasons([ChangedFile("M", ".shiki/goals/G-0012.json")])
assert any("unrelated Shiki goal" in reason and "state_class=mirror" in reason for reason in mergegate_reasons([ChangedFile("M", ".shiki/goals/G-0011.json")]))
assert classify_shiki_path(".shiki/guardian-policy.json", BASE_MANIFEST) == "governance-policy"
assert not files_outside_locks([".shiki/guardian-policy.json"], ["path:.shiki/guardian-policy.json"])

with_fixture(
    lambda root: (
        (root / ".shiki" / "unknown").mkdir(parents=True, exist_ok=True),
        (root / ".shiki" / "unknown" / "file.json").write_text("{}\n", encoding="utf-8"),
        run(["git", "add", "--", ".shiki/unknown/file.json"], root),
        (
            lambda findings: (
                (_ for _ in ()).throw(SystemExit("doctor should report unknown .shiki path"))
                if not any(f.id == "doctor.state_classes.unknown_paths" and f.status == "fail" for f in findings)
                else None
            )
        )(_state_class_findings(root)),
    )
)

finding_ids = {finding.id for finding in _state_class_findings(ROOT)}
for finding_id in {
    "doctor.state_classes.manifest",
    "doctor.state_classes.unknown_paths",
    "doctor.state_classes.runtime_only",
    "doctor.state_classes.append_only",
}:
    assert finding_id in finding_ids

status = migration_status(ROOT)
assert STATE_CLASSES_MIGRATION_ID in status["registry_ids"]
assert status["pending_count"] == 0, status

print("shiki state class tests passed")
PY
