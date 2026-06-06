"""P2.3.1 — lock matching, overlap, and active conflict detection.

Exercises the real shiki_locks helpers: path_matches_lock, locks_overlap,
files_outside_locks, and active_lock_conflicts against on-disk lock records.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_locks import (
    active_lock_conflicts,
    files_outside_locks,
    locks_overlap,
    path_matches_lock,
)


class PathMatchesLockTests(unittest.TestCase):
    def test_glob_lock_matches_nested_path(self) -> None:
        self.assertTrue(path_matches_lock("tests/test_x.py", "path:tests/**"))
        self.assertTrue(path_matches_lock("tests/sub/test_y.py", "path:tests/**"))

    def test_lock_does_not_match_sibling(self) -> None:
        self.assertFalse(path_matches_lock("scripts/x.py", "path:tests/**"))

    def test_literal_file_lock(self) -> None:
        lock = "path:.github/workflows/shiki-validate.yml"
        self.assertTrue(path_matches_lock(".github/workflows/shiki-validate.yml", lock))
        self.assertFalse(path_matches_lock(".github/workflows/other.yml", lock))

    def test_dot_slash_prefix_normalized(self) -> None:
        self.assertTrue(path_matches_lock("./tests/test_x.py", "path:tests/**"))

    def test_semantic_lock_expands_to_paths(self) -> None:
        # shiki:state expands to .shiki/tasks/** etc.
        self.assertTrue(path_matches_lock(".shiki/tasks/T-1.json", "shiki:state"))
        self.assertTrue(path_matches_lock(".shiki/ledger/L-1.json", "shiki:state"))


class LocksOverlapTests(unittest.TestCase):
    def test_identical_locks_overlap(self) -> None:
        self.assertTrue(locks_overlap("path:tests/**", "path:tests/**"))

    def test_nested_path_locks_overlap(self) -> None:
        self.assertTrue(locks_overlap("path:tests/**", "path:tests/sub/**"))

    def test_disjoint_path_locks_do_not_overlap(self) -> None:
        self.assertFalse(locks_overlap("path:tests/**", "path:scripts/**"))

    def test_different_kinds_do_not_overlap(self) -> None:
        self.assertFalse(locks_overlap("path:tests/**", "shiki:contracts"))

    def test_semantic_governance_overlaps_workflow_path(self) -> None:
        # shiki:governance includes .github/workflows/**
        self.assertTrue(
            locks_overlap("shiki:governance", "path:.github/workflows/shiki-validate.yml")
        )


class FilesOutsideLocksTests(unittest.TestCase):
    def test_all_inside(self) -> None:
        files = ["tests/test_a.py", ".github/workflows/shiki-validate.yml"]
        locks = ["path:tests/**", "path:.github/workflows/shiki-validate.yml"]
        self.assertEqual(files_outside_locks(files, locks), [])

    def test_some_outside(self) -> None:
        files = ["tests/test_a.py", "scripts/mergegate_check.py"]
        locks = ["path:tests/**"]
        self.assertEqual(files_outside_locks(files, locks), ["scripts/mergegate_check.py"])


class ActiveLockConflictTests(unittest.TestCase):
    def _make_target(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".shiki" / "locks").mkdir(parents=True)
        return tmp

    def _write_lock(self, target: Path, task_id: str, locks: list[str], state: str = "active") -> None:
        record = {"task_id": task_id, "state": state, "locks": locks}
        (target / ".shiki" / "locks" / f"{task_id}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )

    def test_overlapping_active_lock_conflicts(self) -> None:
        target = self._make_target()
        self._write_lock(target, "T-held", ["path:tests/**"])
        conflicts = active_lock_conflicts(target, "T-mine", ["path:tests/sub/**"])
        self.assertTrue(conflicts)
        self.assertIn("T-held", conflicts[0])

    def test_same_task_does_not_conflict_with_itself(self) -> None:
        target = self._make_target()
        self._write_lock(target, "T-mine", ["path:tests/**"])
        self.assertEqual(active_lock_conflicts(target, "T-mine", ["path:tests/**"]), [])

    def test_released_lock_does_not_conflict(self) -> None:
        target = self._make_target()
        self._write_lock(target, "T-held", ["path:tests/**"], state="released")
        self.assertEqual(active_lock_conflicts(target, "T-mine", ["path:tests/**"]), [])

    def test_disjoint_active_lock_does_not_conflict(self) -> None:
        target = self._make_target()
        self._write_lock(target, "T-held", ["path:scripts/**"])
        self.assertEqual(active_lock_conflicts(target, "T-mine", ["path:tests/**"]), [])

    def test_missing_locks_dir_returns_empty(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(active_lock_conflicts(tmp, "T-mine", ["path:tests/**"]), [])


if __name__ == "__main__":
    unittest.main()
