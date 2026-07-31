"""T-20260729T065622770078Z-3662bff2 — the loop must NAME a lock-conflict stop.

``scripts/shiki_loop._unblock_ready_tasks`` used to do::

    ok, blockers, _ = try_acquire_locks(...)
    if not ok:
        continue

so a dependency-complete task whose locks overlap another task's ACTIVE lock was
skipped with no ledger, no stop, and no reason. A correctly serialized queue then
read as a deadlock — exactly how the PR #179 stall presented. The loop must
instead emit a distinct ``stop_lock_blocked`` result that names the owning task
and the overlapping locks, collapse several overlapping locks from one owner to a
single message, and report the fact when every task of a goal is lock-blocked
rather than appearing idle.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_loop import (
    STOP_ACTIONS,
    _group_lock_conflicts,
    _unblock_ready_tasks,
    decide_goal_action,
    decide_task_action,
    execute_action,
)
from shiki_process import ensure_control_dirs, shiki_path, write_json
from shiki_tasks import tasks_for_goal

GOAL = "G-20260729T065622761503Z-4479b0d3"
OTHER_GOAL = "G-20260729T065622761503Z-00000000"
OWNER = "T-20260729T000000000000Z-owner001"
OWNER_TWO = "T-20260729T000000000000Z-owner002"
B = "T-20260729T000000000001Z-blocked1"
C = "T-20260729T000000000002Z-blocked2"
D = "T-20260729T000000000003Z-running1"

REQUIRED = ["Validate Shiki mirror", "CCA verdict"]


def _task(task_id: str, goal_id: str, *, status: str = "planned", locks, deps=None) -> dict:
    return {
        "id": task_id,
        "goal_id": goal_id,
        "title": "t",
        "scope": "s",
        "non_goals": [],
        "dependencies": list(deps or []),
        "locks": list(locks),
        "assigned_runtime": "claude-code",
        "risk_level": "low",
        "acceptance_checks": ["a"],
        "expected_branch": f"shiki/{task_id.lower()}",
        "ledger_evidence": [],
        "status": status,
    }


def _active_lock(owner_id: str, goal_id: str, locks) -> dict:
    return {
        "task_id": owner_id,
        "goal_id": goal_id,
        "locks": list(locks),
        "state": "active",
        "owner": "shiki-run",
        "created_at": "2026-07-29T00:00:00+00:00",
    }


def _decide(task: dict) -> dict:
    return decide_task_action(
        task,
        checks={},
        pr_state=None,
        repair_attempts=0,
        repair_limit=3,
        required_checks=REQUIRED,
    )


class GroupLockConflictsTests(unittest.TestCase):
    """Check #3: several locks held by ONE owner collapse to one message."""

    def test_one_owner_many_locks_collapses_to_one_record(self) -> None:
        blockers = [
            "Lock conflict: path:a.py held by T-A",
            "Lock conflict: path:b.py held by T-A",
        ]
        self.assertEqual(
            _group_lock_conflicts(blockers),
            [{"owner_task": "T-A", "locks": ["path:a.py", "path:b.py"]}],
        )

    def test_distinct_owners_stay_separate_in_first_seen_order(self) -> None:
        blockers = [
            "Lock conflict: path:a.py held by T-A",
            "Lock conflict: path:c.py held by T-B",
            "Lock conflict: path:d.py held by T-A",
        ]
        self.assertEqual(
            _group_lock_conflicts(blockers),
            [
                {"owner_task": "T-A", "locks": ["path:a.py", "path:d.py"]},
                {"owner_task": "T-B", "locks": ["path:c.py"]},
            ],
        )

    def test_duplicate_lock_for_owner_is_not_repeated(self) -> None:
        blockers = [
            "Lock conflict: path:a.py held by T-A",
            "Lock conflict: path:a.py held by T-A",
        ]
        self.assertEqual(
            _group_lock_conflicts(blockers),
            [{"owner_task": "T-A", "locks": ["path:a.py"]}],
        )

    def test_empty_blockers_group_to_nothing(self) -> None:
        self.assertEqual(_group_lock_conflicts([]), [])


class UnblockReadyTasksTests(unittest.TestCase):
    def _target(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ensure_control_dirs(tmp)
        return tmp

    def test_lock_blocked_task_is_named_with_owner_and_locks(self) -> None:
        # Check #1: a task blocked by another task's active lock yields a named
        # blocked-on-lock record carrying the owning task id and overlapping locks.
        tmp = self._target()
        write_json(shiki_path(tmp, "tasks", f"{B}.json"), _task(B, GOAL, locks=["path:src/x.py"]))
        write_json(shiki_path(tmp, "locks", f"{OWNER}.json"), _active_lock(OWNER, OTHER_GOAL, ["path:src/x.py"]))

        unblocked, lock_blocked = _unblock_ready_tasks(tmp, GOAL)

        self.assertEqual(unblocked, [])
        self.assertEqual(
            lock_blocked,
            [{"task_id": B, "conflicts": [{"owner_task": OWNER, "locks": ["path:src/x.py"]}]}],
        )

    def test_several_locks_one_owner_produce_one_conflict(self) -> None:
        # Check #3 end-to-end through the acquire path: one owner holding two
        # overlapping locks yields a single conflict record, not one per lock.
        tmp = self._target()
        write_json(
            shiki_path(tmp, "tasks", f"{B}.json"),
            _task(B, GOAL, locks=["path:src/x.py", "path:src/y.py"]),
        )
        write_json(
            shiki_path(tmp, "locks", f"{OWNER}.json"),
            _active_lock(OWNER, OTHER_GOAL, ["path:src/x.py", "path:src/y.py"]),
        )

        _, lock_blocked = _unblock_ready_tasks(tmp, GOAL)

        self.assertEqual(len(lock_blocked), 1)
        conflicts = lock_blocked[0]["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["owner_task"], OWNER)
        self.assertEqual(sorted(conflicts[0]["locks"]), ["path:src/x.py", "path:src/y.py"])

    def test_task_with_incomplete_dependency_is_not_lock_blocked(self) -> None:
        # A task whose dependency is not done (and no lock conflict) is neither
        # unblocked nor recorded as lock-blocked: it is genuinely dependency-blocked.
        tmp = self._target()
        write_json(shiki_path(tmp, "tasks", f"{D}.json"), _task(D, GOAL, status="running", locks=["path:src/d.py"]))
        write_json(shiki_path(tmp, "tasks", f"{B}.json"), _task(B, GOAL, locks=["path:src/b.py"], deps=[D]))

        unblocked, lock_blocked = _unblock_ready_tasks(tmp, GOAL)

        self.assertEqual(unblocked, [])
        self.assertEqual(lock_blocked, [])


class UnblockEffectorTests(unittest.TestCase):
    def _target(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ensure_control_dirs(tmp)
        return tmp

    def test_unblock_emits_distinct_named_lock_stop(self) -> None:
        # Checks #1 + #2: the stop names the owner + locks and is a DISTINCT action
        # from a guardian stop and from a blocked-evidence stop_blocked.
        tmp = self._target()
        write_json(shiki_path(tmp, "tasks", f"{B}.json"), _task(B, GOAL, locks=["path:src/x.py"]))
        write_json(shiki_path(tmp, "locks", f"{OWNER}.json"), _active_lock(OWNER, OTHER_GOAL, ["path:src/x.py"]))

        result = execute_action(
            tmp, GOAL, {"action": "unblock", "task_id": B, "reason": "attempt to unblock"}, repair_limit=3
        )

        self.assertEqual(result["action"], "stop_lock_blocked")
        self.assertIn("stop_lock_blocked", STOP_ACTIONS)
        self.assertNotIn(result["action"], {"stop_guardian", "stop_blocked"})
        self.assertEqual(result["task_id"], B)
        self.assertIn(OWNER, result["reason"])
        self.assertIn("path:src/x.py", result["reason"])
        self.assertNotIn("deadlock", result["reason"].replace("not a deadlock", ""))
        self.assertEqual(result["lock_conflicts"][0]["conflicts"][0]["owner_task"], OWNER)

    def test_unblock_incomplete_dependencies_stays_generic_stop_blocked(self) -> None:
        # Check #2 (other direction): a non-lock block stays the generic
        # stop_blocked and carries no lock_conflicts payload.
        tmp = self._target()
        write_json(shiki_path(tmp, "tasks", f"{D}.json"), _task(D, GOAL, status="running", locks=["path:src/d.py"]))
        write_json(shiki_path(tmp, "tasks", f"{B}.json"), _task(B, GOAL, locks=["path:src/b.py"], deps=[D]))

        result = execute_action(
            tmp, GOAL, {"action": "unblock", "task_id": B, "reason": "attempt to unblock"}, repair_limit=3
        )

        self.assertEqual(result["action"], "stop_blocked")
        self.assertNotIn("lock_conflicts", result)

    def test_every_task_lock_blocked_reports_fact_not_idle(self) -> None:
        # Check #4: a goal whose every task is lock-blocked routes to unblock (not
        # an idle "none") and the effector reports each blocked task.
        tmp = self._target()
        write_json(shiki_path(tmp, "tasks", f"{B}.json"), _task(B, GOAL, locks=["path:src/x.py"]))
        write_json(shiki_path(tmp, "tasks", f"{C}.json"), _task(C, GOAL, locks=["path:src/y.py"]))
        write_json(
            shiki_path(tmp, "locks", f"{OWNER}.json"),
            _active_lock(OWNER, OTHER_GOAL, ["path:src/x.py", "path:src/y.py"]),
        )

        tasks = tasks_for_goal(tmp, GOAL)
        decisions = [_decide(task) for task in tasks]
        goal_decision = decide_goal_action(decisions, tasks)
        self.assertEqual(goal_decision["action"], "unblock")

        result = execute_action(tmp, GOAL, goal_decision, repair_limit=3)

        self.assertEqual(result["action"], "stop_lock_blocked")
        blocked_ids = {entry["task_id"] for entry in result["lock_conflicts"]}
        self.assertEqual(blocked_ids, {B, C})

    def test_two_owners_block_one_task_named_separately(self) -> None:
        # One task blocked by two different owners keeps them as two conflict
        # records (dedup is per-owner, not global).
        tmp = self._target()
        write_json(
            shiki_path(tmp, "tasks", f"{B}.json"),
            _task(B, GOAL, locks=["path:src/x.py", "path:src/y.py"]),
        )
        write_json(shiki_path(tmp, "locks", f"{OWNER}.json"), _active_lock(OWNER, OTHER_GOAL, ["path:src/x.py"]))
        write_json(shiki_path(tmp, "locks", f"{OWNER_TWO}.json"), _active_lock(OWNER_TWO, OTHER_GOAL, ["path:src/y.py"]))

        result = execute_action(
            tmp, GOAL, {"action": "unblock", "task_id": B, "reason": "attempt to unblock"}, repair_limit=3
        )

        self.assertEqual(result["action"], "stop_lock_blocked")
        owners = {conflict["owner_task"] for conflict in result["lock_conflicts"][0]["conflicts"]}
        self.assertEqual(owners, {OWNER, OWNER_TWO})


if __name__ == "__main__":
    unittest.main()
