#!/usr/bin/env bash
# P2.3.3 — A malicious PR that mutates protected .shiki paths is rejected by
# the real mergegate_check.enforce_untrusted_shiki_mutations.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

from mergegate_check import ChangedFile, enforce_untrusted_shiki_mutations
from shiki_manifest import load_manifest

manifest = load_manifest(Path.cwd())
target = Path(tempfile.mkdtemp())

task_id = "T-ATTACKER"
goal_id = "G-ATTACKER"
task = {"id": task_id, "goal_id": goal_id, "locks": ["path:tests/**"], "ledger_evidence": []}
pr = {"number": 999}

# Attacker mutates someone else's goal and someone else's task file.
changed = [
    ChangedFile(status="M", path=".shiki/goals/G-VICTIM.json"),
    ChangedFile(status="M", path=".shiki/tasks/T-VICTIM.json"),
]

blocking: list[str] = []
warnings: list[str] = []
enforce_untrusted_shiki_mutations(
    target=target,
    manifest=manifest,
    base_shiki=None,
    changed_files_status=changed,
    task=task,
    goal_id=goal_id,
    task_id=task_id,
    pr=pr,
    blocking=blocking,
    warnings=warnings,
)

joined = "\n".join(blocking)
assert any("unrelated Shiki goal file" in reason for reason in blocking), joined
assert any("unrelated Shiki task file" in reason for reason in blocking), joined

# A clean PR that only touches its own task ledger-allowed paths must NOT be
# flagged for protected-state mutation.
clean_task = {
    "id": task_id,
    "goal_id": goal_id,
    "locks": ["path:tests/**"],
    "ledger_evidence": [],
}
clean_changed = [ChangedFile(status="A", path="tests/test_new.py")]
clean_blocking: list[str] = []
enforce_untrusted_shiki_mutations(
    target=target,
    manifest=manifest,
    base_shiki=None,
    changed_files_status=clean_changed,
    task=clean_task,
    goal_id=goal_id,
    task_id=task_id,
    pr=pr,
    blocking=clean_blocking,
    warnings=[],
)
assert clean_blocking == [], clean_blocking

print("P2.3.3 malicious protected-path PR rejection passed")
PY
