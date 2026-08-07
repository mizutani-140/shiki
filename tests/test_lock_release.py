"""T-20260806T084356215508Z-7a7dd694 — ``shiki lock release`` is the deliberate
counterpart to ``shiki lock acquire``.

The only writer of ``state: released`` before this command was the goal loop's
own closeout (``shiki_loop._release_lock``). A task that ends any OTHER way — done
outside the loop, or hand-closed — held its declared paths forever, and because
``has_active_lock_conflict`` refuses a new acquisition against an active record,
the leak blocked every future task declaring an overlapping path while naming a
task that was already finished.

``cmd_lock_release`` closes that leak with the same discipline as acquire:

* it is the AUTHORITY for a release — no status change, dispatch, or validation
  pass may release a lock (asserted below, not assumed);
* a lock is releasable only when its task is FINISHED, decided from the task
  record (terminal status, ``FINISHED_TASK_STATUSES``); an unfinished task is
  refused with the task and its status named;
* releasing an already-released lock is a no-op success (safe to re-run);
* a missing or unreadable lock record is refused with the path named;
* the record is MARKED (``state: released`` + ``released_by``/``released_at``
  alongside the acquisition fields), never deleted, and the JSON result mirrors
  ``cmd_lock_acquire``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_tasks
from shiki_process import ensure_control_dirs, shiki_path, write_json
from shiki_tasks import (
    FINISHED_TASK_STATUSES,
    cmd_dispatch_check,
    cmd_lock_acquire,
    cmd_lock_release,
    lock_record,
    load_task,
)

GOAL = "G-20260806T084356214001Z-8198f059"
TASK = "T-20260806T084356215508Z-7a7dd694"
OTHER = "T-20260806T000000000000Z-0000other"
PATHS = ["path:src/audit/*"]

NON_TERMINAL_STATUSES = ("planned", "ready", "running", "blocked", "review", "repair-needed")


def _task(task_id: str = TASK, *, status: str = "done", locks=PATHS, goal_id: str = GOAL) -> dict:
    return {
        "id": task_id,
        "goal_id": goal_id,
        "title": "t",
        "scope": "s",
        "non_goals": [],
        "dependencies": [],
        "locks": list(locks),
        "assigned_runtime": "claude-code",
        "risk_level": "low",
        "acceptance_checks": ["a"],
        "expected_branch": f"shiki/{task_id.lower()}",
        "expected_pr": None,
        "ledger_evidence": [],
        "status": status,
    }


def _lock(task_id: str = TASK, *, locks=PATHS, state: str = "active", owner: str = "shiki-cli", goal_id: str = GOAL) -> dict:
    return {
        "task_id": task_id,
        "goal_id": goal_id,
        "locks": list(locks),
        "state": state,
        "owner": owner,
        "created_at": "2026-08-06T00:00:00+00:00",
    }


class _TargetCase(unittest.TestCase):
    def setUp(self) -> None:
        # cmd_* commands require a GitHub-first target; the release logic is
        # independent of that preflight, so stub it for a fast unit test (mirrors
        # tests/test_lock_lifecycle.CmdWiringTests).
        self._original = shiki_tasks.require_github_first_target
        shiki_tasks.require_github_first_target = lambda *_a, **_k: None
        self.addCleanup(setattr, shiki_tasks, "require_github_first_target", self._original)

    def _target(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ensure_control_dirs(tmp)
        return tmp

    def _write_task(self, tmp: Path, task: dict) -> None:
        write_json(shiki_path(tmp, "tasks", f"{task['id']}.json"), task)

    def _write_lock(self, tmp: Path, record: dict) -> None:
        write_json(shiki_path(tmp, "locks", f"{record['task_id']}.json"), record)

    def _ledger_count(self, tmp: Path) -> int:
        directory = shiki_path(tmp, "ledger")
        return len(list(directory.glob("*.json"))) if directory.exists() else 0

    def _run(self, func, **kwargs) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = func(argparse.Namespace(**kwargs))
        return code, json.loads(buffer.getvalue())

    def _release(self, tmp: Path, task_id: str = TASK, owner: str = "shiki-cli") -> tuple[int, dict]:
        return self._run(cmd_lock_release, target=str(tmp), owner=owner, task_id=task_id)


class TerminalStatusConsistency(unittest.TestCase):
    """The releasable set is single-sourced with the validator's terminal set."""

    def test_finished_statuses_match_validate_shiki_terminal(self) -> None:
        import validate_shiki

        self.assertEqual(set(FINISHED_TASK_STATUSES), set(validate_shiki.TERMINAL_TASK_STATUSES))


class FinishedReleaseTests(_TargetCase):
    def test_finished_task_lock_releases_and_records_releaser(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="done"))
        self._write_lock(tmp, _lock(owner="shiki-cli"))

        code, result = self._release(tmp, owner="ops-person")

        self.assertEqual(code, 0)
        self.assertTrue(result["released"])
        self.assertEqual(result["task_id"], TASK)
        self.assertEqual(result["locks_released"], PATHS)
        self.assertIn("lock_file", result)
        self.assertIn("ledger_id", result)

        record = lock_record(tmp, TASK)
        # Marked, not deleted: the release fields sit alongside the acquisition
        # fields so the acquire/release pair reads from one record.
        self.assertEqual(record["state"], "released")
        self.assertEqual(record["released_by"], "ops-person")
        self.assertIn("released_at", record)
        self.assertEqual(record["owner"], "shiki-cli")
        self.assertEqual(record["created_at"], "2026-08-06T00:00:00+00:00")
        self.assertEqual(record["locks"], PATHS)

    def test_release_record_shape_mirrors_acquire_result_contract(self) -> None:
        # acquire returns task_id / locks_requested / <granted flag> /
        # blocking_reasons, then lock_file + ledger_id on success; release reads
        # as its pair.
        tmp = self._target()
        self._write_task(tmp, _task(status="planned", locks=PATHS))
        acq_code, acquire = self._run(cmd_lock_acquire, target=str(tmp), owner="shiki-cli", task_id=TASK)
        self.assertEqual(acq_code, 0)

        self._write_task(tmp, _task(status="done", locks=PATHS))
        _, release = self._release(tmp)

        self.assertEqual(
            set(release) - {"already_released"},
            {"task_id", "locks_released", "released", "blocking_reasons", "lock_file", "ledger_id"},
        )
        self.assertEqual(release["locks_released"], acquire["locks_requested"])
        self.assertEqual(release["blocking_reasons"], [])

    def test_released_paths_are_acquirable_by_a_different_task(self) -> None:
        # The property that matters: a different task declaring the SAME paths is
        # blocked while the finished task holds them, and granted once released.
        tmp = self._target()
        self._write_task(tmp, _task(status="done", locks=PATHS))
        self._write_lock(tmp, _lock())
        self._write_task(tmp, _task(OTHER, status="planned", locks=PATHS))

        blocked_code, blocked = self._run(cmd_lock_acquire, target=str(tmp), owner="shiki-cli", task_id=OTHER)
        self.assertEqual(blocked_code, 1)
        self.assertFalse(blocked["locks_granted"])
        self.assertTrue(any(TASK in reason for reason in blocked["blocking_reasons"]))

        self._release(tmp)

        granted_code, granted = self._run(cmd_lock_acquire, target=str(tmp), owner="shiki-cli", task_id=OTHER)
        self.assertEqual(granted_code, 0)
        self.assertTrue(granted["locks_granted"])
        self.assertEqual(lock_record(tmp, OTHER)["state"], "active")


class RefusalTests(_TargetCase):
    def test_unfinished_task_is_refused_naming_task_and_status(self) -> None:
        for status in NON_TERMINAL_STATUSES:
            with self.subTest(status=status):
                tmp = self._target()
                self._write_task(tmp, _task(status=status))
                self._write_lock(tmp, _lock())

                code, result = self._release(tmp)

                self.assertEqual(code, 1)
                self.assertFalse(result["released"])
                reason = " ".join(result["blocking_reasons"])
                self.assertIn(TASK, reason)
                self.assertIn(status, reason)
                # Refused: the grant is left untouched so the task keeps its paths.
                record = lock_record(tmp, TASK)
                self.assertEqual(record["state"], "active")
                self.assertNotIn("released_by", record)

    def test_missing_lock_record_is_refused_naming_path(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="done"))  # finished, but no lock record

        code, result = self._release(tmp)

        self.assertEqual(code, 1)
        self.assertFalse(result["released"])
        reason = " ".join(result["blocking_reasons"])
        self.assertIn(str(shiki_path(tmp, "locks", f"{TASK}.json")), reason)

    def test_unreadable_lock_record_is_refused_naming_path(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="done"))
        lock_file = shiki_path(tmp, "locks", f"{TASK}.json")
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text("{not valid json", encoding="utf-8")

        code, result = self._release(tmp)

        self.assertEqual(code, 1)
        self.assertFalse(result["released"])
        self.assertIn(str(lock_file), " ".join(result["blocking_reasons"]))


class IdempotencyTests(_TargetCase):
    def test_pre_released_lock_is_a_noop_success(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="done"))
        self._write_lock(tmp, _lock(state="released"))
        before = self._ledger_count(tmp)

        code, result = self._release(tmp)

        self.assertEqual(code, 0)
        self.assertTrue(result["released"])
        self.assertTrue(result["already_released"])
        # A no-op must not rewrite the record or append a duplicate ledger.
        self.assertNotIn("released_by", lock_record(tmp, TASK))
        self.assertEqual(self._ledger_count(tmp), before)

    def test_re_running_after_a_real_release_is_a_noop(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="done"))
        self._write_lock(tmp, _lock())

        _, first = self._release(tmp, owner="ops-person")
        released_at = lock_record(tmp, TASK)["released_at"]
        after_first = self._ledger_count(tmp)

        code, second = self._release(tmp, owner="someone-else")

        self.assertEqual(code, 0)
        self.assertTrue(second["released"])
        self.assertTrue(second["already_released"])
        record = lock_record(tmp, TASK)
        # The original release stands: releaser and timestamp are not overwritten,
        # and no second ledger is appended.
        self.assertEqual(record["released_by"], "ops-person")
        self.assertEqual(record["released_at"], released_at)
        self.assertEqual(self._ledger_count(tmp), after_first)


class NoImplicitReleaseTests(_TargetCase):
    """No status change, dispatch, or validation pass releases a lock."""

    def test_dispatch_check_does_not_release(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="running", locks=PATHS))
        self._write_lock(tmp, _lock())

        code, result = self._run(cmd_dispatch_check, target=str(tmp), task_id=TASK, require_worktree=False)

        self.assertEqual(code, 0)
        self.assertTrue(result["locks_granted"])
        self.assertEqual(lock_record(tmp, TASK)["state"], "active")

    def test_acquire_status_promotion_does_not_release(self) -> None:
        # cmd_lock_acquire promotes planned -> ready; that status change must not
        # release the grant it just wrote.
        tmp = self._target()
        self._write_task(tmp, _task(status="planned", locks=PATHS))

        self._run(cmd_lock_acquire, target=str(tmp), owner="shiki-cli", task_id=TASK)

        self.assertEqual(load_task(tmp, TASK)["status"], "ready")
        self.assertEqual(lock_record(tmp, TASK)["state"], "active")


if __name__ == "__main__":
    unittest.main()
