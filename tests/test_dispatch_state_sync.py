"""Dispatch state sync (T-20260729T065622770911Z-a8db8b69).

A dispatch hands the implementer a worktree, but nothing guarantees that
worktree holds the contract the coordinator currently has, nor the evidence that
contract references. Two gaps, one seam:

 (a) shiki_runtime.sync_contract_into_worktree — before a headless session
     starts, the worktree's task/goal/lock mirror is refreshed from the
     coordinator so the implementer judges against CURRENT terms (e.g. a repair
     dispatched after a Spec Amendment tightened the locks). Only THIS task's /
     goal's id-named files are carried, never a sibling's or a foreign goal's.

 (b) shiki_loop._evidence_relatives_for_task — the transitive ledger -> evidence
     walk expands a ledger ONLY when its goal_id AND task_id both equal the
     task's. That is deliberately STRICTER than
     mergegate_check.ledger_entry_allowed_for_task, which also accepts a
     goal-scoped ledger with an empty task_id; reusing the looser bound would let
     a padded ledger_evidence inherit the .shiki paths a foreign or goal-level
     ledger references.

The suite is dependency free: standard-library ``unittest`` only.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_loop import _evidence_relatives_for_task
from shiki_runtime import sync_contract_into_worktree

TASK = "T-20260729T065622770911Z-a8db8b69"
GOAL = "G-20260729T065622761503Z-4479b0d3"
OTHER_TASK = "T-20260101T000000000000Z-sibling0"
OTHER_GOAL = "G-20260101T000000000000Z-foreign00"


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class ContractSyncTests(unittest.TestCase):
    """Gap (a): the coordinator's current contract lands in the worktree."""

    def _coordinator(self, tmp: Path, *, locks) -> Path:
        target = tmp / "repo"
        _write(
            target / ".shiki" / "tasks" / f"{TASK}.json",
            {"id": TASK, "goal_id": GOAL, "status": "repair-needed", "locks": locks},
        )
        _write(target / ".shiki" / "goals" / f"{GOAL}.json", {"id": GOAL, "status": "planned"})
        _write(
            target / ".shiki" / "locks" / f"{TASK}.json",
            {"task_id": TASK, "goal_id": GOAL, "state": "active", "locks": locks},
        )
        return target

    def _stale_worktree(self, tmp: Path, *, locks) -> Path:
        wt = tmp / "wt"
        # A worktree cut before the amendment carries the STALE contract.
        _write(
            wt / ".shiki" / "tasks" / f"{TASK}.json",
            {"id": TASK, "goal_id": GOAL, "status": "running", "locks": locks},
        )
        return wt

    def test_amended_contract_lands_in_worktree(self):
        # Acceptance 1: a repair dispatched after a contract amendment sees the
        # amended contract — the task file it reads declares the amended locks.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            amended = ["path:scripts/a.py", "path:scripts/b.py", "path:scripts/c.py"]
            target = self._coordinator(tmp, locks=amended)
            wt = self._stale_worktree(tmp, locks=["path:scripts/a.py"])  # stale 1-lock
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, wt)

            wt_task = _read(wt / ".shiki" / "tasks" / f"{TASK}.json")
            self.assertEqual(wt_task["locks"], amended)
            self.assertIn(f".shiki/tasks/{TASK}.json", synced)

    def test_goal_and_lock_records_are_carried(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            locks = ["path:scripts/x.py"]
            target = self._coordinator(tmp, locks=locks)
            wt = self._stale_worktree(tmp, locks=["path:old.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, wt)

            self.assertIn(f".shiki/goals/{GOAL}.json", synced)
            self.assertIn(f".shiki/locks/{TASK}.json", synced)
            self.assertEqual(
                _read(wt / ".shiki" / "goals" / f"{GOAL}.json"),
                _read(target / ".shiki" / "goals" / f"{GOAL}.json"),
            )
            self.assertEqual(_read(wt / ".shiki" / "locks" / f"{TASK}.json")["locks"], locks)

    def test_foreign_goal_file_is_not_carried(self):
        # Only this task's own goal rides along; an unrelated goal's mirror state
        # must never leak into the worktree.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            target = self._coordinator(tmp, locks=["path:scripts/x.py"])
            _write(target / ".shiki" / "goals" / f"{OTHER_GOAL}.json", {"id": OTHER_GOAL})
            wt = self._stale_worktree(tmp, locks=["path:old.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, wt)

            self.assertNotIn(f".shiki/goals/{OTHER_GOAL}.json", synced)
            self.assertFalse((wt / ".shiki" / "goals" / f"{OTHER_GOAL}.json").exists())

    def test_idempotent_no_duplicate_or_conflicting_records(self):
        # Acceptance 5: re-running a dispatch is idempotent — the id-named mirror
        # files are overwritten in place, never duplicated or left conflicting.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            locks = ["path:scripts/x.py", "path:scripts/y.py"]
            target = self._coordinator(tmp, locks=locks)
            wt = self._stale_worktree(tmp, locks=["path:old.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            first = sync_contract_into_worktree(target, task, wt)
            second = sync_contract_into_worktree(target, task, wt)

            self.assertEqual(first, second)
            # Exactly one file per id-named path; content equals the coordinator's.
            self.assertEqual(len(list((wt / ".shiki" / "tasks").glob("*.json"))), 1)
            self.assertEqual(len(list((wt / ".shiki" / "goals").glob("*.json"))), 1)
            self.assertEqual(len(list((wt / ".shiki" / "locks").glob("*.json"))), 1)
            self.assertEqual(
                _read(wt / ".shiki" / "tasks" / f"{TASK}.json"),
                _read(target / ".shiki" / "tasks" / f"{TASK}.json"),
            )

    def test_missing_coordinator_files_are_skipped(self):
        # A coordinator with no lock record must not crash the sync nor fabricate
        # a lock file in the worktree.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            target = self._coordinator(tmp, locks=["path:scripts/x.py"])
            (target / ".shiki" / "locks" / f"{TASK}.json").unlink()
            wt = self._stale_worktree(tmp, locks=["path:old.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, wt)

            self.assertNotIn(f".shiki/locks/{TASK}.json", synced)
            self.assertFalse((wt / ".shiki" / "locks" / f"{TASK}.json").exists())
            self.assertIn(f".shiki/tasks/{TASK}.json", synced)  # the rest still rode

    def test_no_op_when_worktree_is_the_target_checkout(self):
        # Headless dispatch requires an isolated worktree; syncing onto the target
        # itself would be a no-op self-copy, so it is refused.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            target = self._coordinator(tmp, locks=["path:scripts/x.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, target)

            self.assertEqual(synced, [])


class EvidenceOwnershipBoundTests(unittest.TestCase):
    """Gap (b): a ledger's transitive evidence rides on the branch only when the
    ledger is unambiguously THIS task's own (goal_id AND task_id both match)."""

    def _seed_task(self, target: Path, ledger_ids) -> dict:
        task = {
            "id": TASK,
            "goal_id": GOAL,
            "status": "review",
            "expected_branch": "b",
            "ledger_evidence": list(ledger_ids),
        }
        _write(target / ".shiki" / "tasks" / f"{TASK}.json", task)
        _write(target / ".shiki" / "worktrees" / f"{TASK}.json", {"path": "x", "branch": "b"})
        return task

    def _check_ledger(self, target: Path, ledger_id: str, *, goal_id, task_id, exec_name):
        _write(
            target / ".shiki" / "ledger" / f"{ledger_id}.json",
            {
                "id": ledger_id,
                "type": "check",
                "goal_id": goal_id,
                "task_id": task_id,
                "evidence": [f".shiki/runner/{exec_name}.json"],
            },
        )
        _write(target / ".shiki" / "runner" / f"{exec_name}.json", {"id": exec_name})

    def test_own_ledger_carries_its_exec_record(self):
        # Acceptance 2: a check ledger owned by this task carries its EXEC record
        # onto the branch, so MergeGate sees no dangling evidence.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            self._check_ledger(target, "L-OWN", goal_id=GOAL, task_id=TASK, exec_name="EXEC-own")
            task = self._seed_task(target, ["L-OWN"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertIn(".shiki/ledger/L-OWN.json", rel)
            self.assertIn(".shiki/runner/EXEC-own.json", rel)

    def test_foreign_goal_ledger_does_not_pull_refs(self):
        # Acceptance 3: a ledger of ANOTHER goal contributes nothing transitively.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            self._check_ledger(target, "L-FG", goal_id=OTHER_GOAL, task_id=TASK, exec_name="EXEC-fg")
            task = self._seed_task(target, ["L-FG"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertNotIn(".shiki/runner/EXEC-fg.json", rel)

    def test_foreign_task_ledger_does_not_pull_refs(self):
        # Acceptance 3: a ledger owned by a DIFFERENT task of the same goal is the
        # anti-padding case — its referenced paths are not pulled in.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            self._check_ledger(target, "L-FT", goal_id=GOAL, task_id=OTHER_TASK, exec_name="EXEC-ft")
            task = self._seed_task(target, ["L-FT"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertNotIn(".shiki/runner/EXEC-ft.json", rel)

    def test_goal_scoped_ledger_does_not_pull_refs(self):
        # The strict-bound differentiator: a goal-level ledger (empty task_id,
        # matching goal) is accepted by mergegate_check.ledger_entry_allowed_for_task
        # but is NOT expanded here — reusing that looser bound would defeat the
        # anti-padding property this sync exists to enforce.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            _write(
                target / ".shiki" / "ledger" / "L-GS.json",
                {
                    "id": "L-GS",
                    "type": "completion",
                    "goal_id": GOAL,
                    "task_id": "",
                    "evidence": [".shiki/reports/R-gs.json"],
                },
            )
            _write(target / ".shiki" / "reports" / "R-gs.json", {"id": "R-gs"})
            task = self._seed_task(target, ["L-GS"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertNotIn(".shiki/reports/R-gs.json", rel)

    def test_bound_is_per_ledger_not_all_or_nothing(self):
        # Own and foreign ledgers side by side: the own ledger's EXEC rides along,
        # the foreign ledger's does not.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            self._check_ledger(target, "L-OWN", goal_id=GOAL, task_id=TASK, exec_name="EXEC-own")
            self._check_ledger(target, "L-FT", goal_id=GOAL, task_id=OTHER_TASK, exec_name="EXEC-ft")
            task = self._seed_task(target, ["L-OWN", "L-FT"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertIn(".shiki/runner/EXEC-own.json", rel)
            self.assertNotIn(".shiki/runner/EXEC-ft.json", rel)


if __name__ == "__main__":
    unittest.main()
