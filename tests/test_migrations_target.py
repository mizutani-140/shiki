"""Migrations must apply in an installed Shiki target, not only the platform repo.

The baseline migration once required the platform-only goal artifact
``.shiki/goals/G-0012.json``. Every ``install-target`` ships the four baseline
layout entries (``manifest.json``, ``tasks``, ``goals``, ``ledger``) but never
that platform goal, so ``migrate apply`` could never succeed in a real target —
it stopped on the missing prerequisite. These tests drive the real CLI against a
freshly installed target and assert the whole path works:

* ``migrate plan`` lists the baseline as pending after the state is removed;
* ``migrate apply --execute`` writes a valid state and clears every pending
  migration;
* a genuinely invalid migration state fails through the ``[shiki] error:`` exit
  contract instead of dumping a raw Python traceback;
* ``doctor`` on the repaired target passes both migration findings.

Dependency-free ``unittest`` only; drives ``scripts/shiki.py`` via subprocess so
the assertions exercise the same exit contract an operator sees.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_migrations import BASELINE_MIGRATION_ID

SHIKI_CLI = shiki_test_support.SCRIPTS_DIR / "shiki.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SHIKI_CLI), *args],
        capture_output=True,
        text=True,
    )


def _finding_status(doctor_json: str, finding_id: str) -> str:
    report = json.loads(doctor_json)
    for finding in report["findings"]:
        if finding["id"] == finding_id:
            return finding["status"]
    raise AssertionError(f"missing doctor finding {finding_id}")


class MigrationsTargetPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-migrations-target-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "target"
        self.target.mkdir()
        install = _run("install-target", str(self.target), "--local-only", "--no-validate")
        self.assertEqual(install.returncode, 0, install.stderr)
        self.state = self.target / ".shiki" / "migrations" / "state.json"

    def test_fresh_target_ships_layout_but_not_platform_goal(self) -> None:
        # Guards the premise: the platform-only goal artifact never ships to a
        # target, but the four baseline layout entries always do.
        for relative in (".shiki/manifest.json", ".shiki/tasks", ".shiki/goals", ".shiki/ledger"):
            self.assertTrue((self.target / relative).exists(), relative)
        self.assertFalse((self.target / ".shiki/goals/G-0012.json").exists())

    def test_plan_lists_baseline_pending_after_state_removed(self) -> None:
        self.state.unlink()
        result = _run("migrate", "plan", "--json", "--target", str(self.target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["dry_run"])
        planned_ids = [item["id"] for item in payload["planned"]]
        self.assertIn(BASELINE_MIGRATION_ID, planned_ids)
        # A dry-run plan must never write migration state.
        self.assertFalse(self.state.exists())

    def test_apply_execute_writes_valid_state_and_clears_pending(self) -> None:
        self.state.unlink()
        applied = _run("migrate", "apply", "--execute", "--target", str(self.target))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertNotIn("Traceback (most recent call last)", applied.stderr)
        self.assertTrue(self.state.exists())

        status = _run("migrate", "status", "--json", "--target", str(self.target))
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertTrue(payload["valid"], payload["errors"])
        self.assertEqual(payload["pending_count"], 0, payload["pending"])

    def test_invalid_state_uses_shiki_error_exit_contract(self) -> None:
        # A parseable-but-semantically-invalid state (wrong schema version) cannot
        # be auto-recovered, so apply must surface it through the [shiki] error:
        # contract with a non-zero exit and no raw traceback.
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["version"] = 999
        self.state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        result = _run("migrate", "apply", "--execute", "--target", str(self.target))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[shiki] error:", result.stderr)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)

    def test_doctor_passes_both_migration_findings_after_repair(self) -> None:
        self.state.unlink()
        applied = _run("migrate", "apply", "--execute", "--target", str(self.target))
        self.assertEqual(applied.returncode, 0, applied.stderr)

        doctor = _run("doctor", "--json", "--target", str(self.target))
        self.assertEqual(_finding_status(doctor.stdout, "doctor.migrations.state"), "pass")
        self.assertEqual(_finding_status(doctor.stdout, "doctor.migrations.pending"), "pass")


if __name__ == "__main__":
    unittest.main()
