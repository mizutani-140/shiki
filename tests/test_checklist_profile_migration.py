"""The cca_checklist_profile backfill migration edits tasks in place.

``M-20260801-0001-checklist-profile`` walks ``.shiki/tasks/`` and normalises the
``cca_checklist_profile`` field so it can become a required, CCA-gate-matchable
contract:

* a task lacking the key gains ``[]`` inserted directly after ``assigned_runtime``;
* a bare-family value (entries with no ``-`` and no digits, e.g.
  ``['PR','TDD','V','CCA']``) — which the gate cannot match — collapses to ``[]``;
* an id-shaped value (``CCA-08``, ``V-04``) and an already-empty value are left
  untouched.

The measured hazard is byte layout: 14 of the stored task files are serialized
with unsorted keys, so a ``json.dumps(..., sort_keys=True)`` round-trip would
churn files the migration never intended to touch. These tests drive the real
registered migration over a *temporary fixture tree* — never the repository
mirror — and assert it inserts/collapses in place, changes no other key, is
idempotent, and writes nothing under ``dry_run``.

Dependency-free ``unittest`` only.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_migrations import (
    CHECKLIST_PROFILE_MIGRATION_ID,
    migration_registry,
    validate_migration_registry,
)

# A non-canonical fixture: keys are deliberately out of alphabetical order and
# there is no trailing newline, mirroring the real unsorted task files. The
# migration must touch only the one inserted line.
NONCANON_MISSING = (
    "{\n"
    '  "id": "T-noncanon",\n'
    '  "title": "Non canonical task",\n'
    '  "assigned_runtime": "codex-front",\n'
    '  "status": "planned",\n'
    '  "scope": "Something"\n'
    "}"
)

NONCANON_BARE = (
    "{\n"
    '  "id": "T-noncanon-bare",\n'
    '  "assigned_runtime": "claude-code",\n'
    '  "cca_checklist_profile": [\n'
    '    "PR",\n'
    '    "TDD",\n'
    '    "V",\n'
    '    "CCA"\n'
    "  ],\n"
    '  "status": "review"\n'
    "}"
)


def _canonical(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


class ChecklistProfileMigrationTests(unittest.TestCase):
    def _apply_fn(self) -> Callable[[Path, bool], dict[str, Any]]:
        by_id = {migration.id: migration for migration in migration_registry()}
        migration = by_id[CHECKLIST_PROFILE_MIGRATION_ID]
        self.assertIsNotNone(migration.apply, "migration must carry an apply callable")
        return migration.apply  # type: ignore[return-value]

    def _tree(self, files: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp(prefix="shiki-checklist-profile-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        tasks = root / ".shiki" / "tasks"
        tasks.mkdir(parents=True)
        for name, content in files.items():
            (tasks / name).write_text(content, encoding="utf-8")
        return root

    @staticmethod
    def _read(root: Path, name: str) -> str:
        return (root / ".shiki" / "tasks" / name).read_text(encoding="utf-8")

    # --- registration -----------------------------------------------------

    def test_registered_non_destructive_with_apply(self) -> None:
        self.assertEqual(validate_migration_registry(), [])
        by_id = {migration.id: migration for migration in migration_registry()}
        self.assertIn(CHECKLIST_PROFILE_MIGRATION_ID, by_id)
        migration = by_id[CHECKLIST_PROFILE_MIGRATION_ID]
        self.assertFalse(migration.destructive)
        self.assertTrue(callable(migration.apply))

    # --- per-value behaviour ----------------------------------------------

    def test_missing_key_gains_empty_list_after_assigned_runtime(self) -> None:
        before = _canonical(
            {"assigned_runtime": "claude-code", "id": "T-a", "status": "planned"}
        )
        self.assertNotIn("cca_checklist_profile", before)
        root = self._tree({"T-a.json": before})
        self._apply_fn()(root, False)
        after = self._read(root, "T-a.json")
        data = json.loads(after)
        self.assertEqual(data["cca_checklist_profile"], [])
        # Inserted directly after assigned_runtime, before the next key.
        self.assertIn(
            '"assigned_runtime": "claude-code",\n  "cca_checklist_profile": [],', after
        )

    def test_bare_family_value_collapses_to_empty(self) -> None:
        before = _canonical(
            {
                "assigned_runtime": "claude-code",
                "cca_checklist_profile": ["PR", "TDD", "V", "CCA"],
                "id": "T-b",
            }
        )
        root = self._tree({"T-b.json": before})
        self._apply_fn()(root, False)
        self.assertEqual(json.loads(self._read(root, "T-b.json"))["cca_checklist_profile"], [])

    def test_id_shaped_value_untouched(self) -> None:
        before = _canonical(
            {
                "assigned_runtime": "claude-code",
                "cca_checklist_profile": ["CCA-08", "PR-01", "V-04"],
                "id": "T-c",
            }
        )
        root = self._tree({"T-c.json": before})
        self._apply_fn()(root, False)
        self.assertEqual(self._read(root, "T-c.json"), before)

    def test_already_empty_value_untouched(self) -> None:
        before = _canonical(
            {"assigned_runtime": "claude-code", "cca_checklist_profile": [], "id": "T-d"}
        )
        root = self._tree({"T-d.json": before})
        self._apply_fn()(root, False)
        self.assertEqual(self._read(root, "T-d.json"), before)

    # --- byte-layout preservation -----------------------------------------

    def test_noncanonical_layout_preserved_except_inserted_key(self) -> None:
        root = self._tree({"T-noncanon.json": NONCANON_MISSING})
        self._apply_fn()(root, False)
        after = self._read(root, "T-noncanon.json")

        anchor = '  "assigned_runtime": "codex-front",'
        expected = NONCANON_MISSING.replace(
            anchor, anchor + '\n  "cca_checklist_profile": [],'
        )
        self.assertEqual(after, expected)
        # The ONLY textual change is the single inserted line.
        self.assertEqual(after.replace('\n  "cca_checklist_profile": [],', ""), NONCANON_MISSING)
        # No trailing newline was introduced.
        self.assertFalse(after.endswith("\n"))

    def test_noncanonical_bare_value_collapses_and_preserves_layout(self) -> None:
        root = self._tree({"T-noncanon-bare.json": NONCANON_BARE})
        self._apply_fn()(root, False)
        after = self._read(root, "T-noncanon-bare.json")
        self.assertEqual(json.loads(after)["cca_checklist_profile"], [])
        # Only the array collapsed; sibling text and the absent trailing newline hold.
        self.assertIn('  "cca_checklist_profile": [],\n', after)
        self.assertFalse(after.endswith("\n"))

    def test_no_other_key_value_changes(self) -> None:
        before = _canonical(
            {
                "assigned_runtime": "claude-code",
                "cca_checklist_profile": ["PR", "TDD"],
                "dependencies": ["T-x"],
                "id": "T-e",
                "scope": "keep me",
                "status": "planned",
            }
        )
        root = self._tree({"T-e.json": before})
        self._apply_fn()(root, False)
        after_data = json.loads(self._read(root, "T-e.json"))
        before_data = json.loads(before)
        before_data.pop("cca_checklist_profile")
        after_data.pop("cca_checklist_profile")
        self.assertEqual(after_data, before_data)

    # --- idempotency and dry-run ------------------------------------------

    def test_idempotent_second_run_no_diff(self) -> None:
        files = {
            "T-missing.json": _canonical(
                {"assigned_runtime": "claude-code", "id": "T-missing"}
            ),
            "T-bare.json": _canonical(
                {
                    "assigned_runtime": "claude-code",
                    "cca_checklist_profile": ["PR", "TDD", "V", "CCA"],
                    "id": "T-bare",
                }
            ),
            "T-id.json": _canonical(
                {
                    "assigned_runtime": "claude-code",
                    "cca_checklist_profile": ["CCA-08"],
                    "id": "T-id",
                }
            ),
            "T-noncanon.json": NONCANON_MISSING,
        }
        root = self._tree(files)
        self._apply_fn()(root, False)
        first = {name: self._read(root, name) for name in files}
        result = self._apply_fn()(root, False)
        second = {name: self._read(root, name) for name in files}
        self.assertEqual(first, second)
        self.assertEqual(result["summary"], "Backfilled cca_checklist_profile in 0 task file(s).")

    def test_dry_run_writes_nothing(self) -> None:
        files = {
            "T-missing.json": _canonical(
                {"assigned_runtime": "claude-code", "id": "T-missing"}
            ),
            "T-bare.json": _canonical(
                {
                    "assigned_runtime": "claude-code",
                    "cca_checklist_profile": ["PR", "TDD"],
                    "id": "T-bare",
                }
            ),
            "T-noncanon.json": NONCANON_MISSING,
        }
        root = self._tree(files)
        result = self._apply_fn()(root, True)
        self.assertTrue(result["dry_run"])
        for name, content in files.items():
            self.assertEqual(self._read(root, name), content, f"{name} must be untouched")


if __name__ == "__main__":
    unittest.main()
