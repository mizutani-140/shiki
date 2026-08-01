"""T-20260729T065622768738Z-fa978e4a — lock acquisition must preserve the task
lifecycle and follow contract amendments.

Two defects observed live on PR #179 (scripts/shiki_tasks.py):

(a) ``cmd_lock_acquire`` / ``try_acquire_locks`` set ``status='ready'``
    unconditionally, so re-acquiring a grant rewound a task that already had an
    open PR from ``review`` back to ``ready`` — MergeGate's post-CCA rule then
    blocked it. Only ``planned`` (a freshly registered task) may become
    ``ready``; any status at or beyond ``running`` is preserved.

(b) A Spec Amendment that widens a task's locks silently invalidates its own
    grant, because the grant is checked as a superset of the contract and nothing
    in the amendment path refreshes it. It must be detected and the grant
    refreshed — or, when the widened contract collides with another task's active
    lock, reported as a grant/contract divergence naming BOTH lock sets and the
    owning task, never a bare 'locks are not granted'.
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
from shiki_process import ensure_control_dirs, read_json, shiki_path, write_json
from shiki_tasks import (
    cmd_dispatch_check,
    cmd_lock_acquire,
    evaluate_lock_grant,
    lock_record,
    load_task,
    refresh_lock_grant,
    status_after_lock_acquire,
    try_acquire_locks,
)

GOAL = "G-20260729T065622761503Z-4479b0d3"
TASK = "T-20260729T065622768738Z-fa978e4a"
OWNER = "T-20260729T000000000000Z-owner001"

CONTRACT = "path:scripts/shiki_tasks.py"
WIDENED = "path:tests/test_lock_lifecycle.py"


def _task(task_id: str = TASK, *, status: str = "planned", locks=(CONTRACT,), goal_id: str = GOAL) -> dict:
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


def _active_lock(owner_id: str, locks, goal_id: str = "G-other") -> dict:
    return {
        "task_id": owner_id,
        "goal_id": goal_id,
        "locks": list(locks),
        "state": "active",
        "owner": "shiki-run",
        "created_at": "2026-07-29T00:00:00+00:00",
    }


class _TargetCase(unittest.TestCase):
    def _target(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ensure_control_dirs(tmp)
        return tmp

    def _write_task(self, tmp: Path, task: dict) -> None:
        write_json(shiki_path(tmp, "tasks", f"{task['id']}.json"), task)

    def _write_grant(self, tmp: Path, task_id: str, locks, *, goal_id: str = GOAL, state: str = "active") -> None:
        write_json(
            shiki_path(tmp, "locks", f"{task_id}.json"),
            {
                "task_id": task_id,
                "goal_id": goal_id,
                "locks": list(locks),
                "state": state,
                "owner": "shiki-cli",
                "created_at": "2026-07-29T00:00:00+00:00",
            },
        )


class StatusAfterLockAcquireTests(unittest.TestCase):
    """Defect (a): only a freshly registered task is promoted to ready."""

    def test_planned_becomes_ready(self) -> None:
        self.assertEqual(status_after_lock_acquire("planned"), "ready")

    def test_missing_status_becomes_ready(self) -> None:
        # A freshly registered task without a status still gains ready.
        self.assertEqual(status_after_lock_acquire(None), "ready")
        self.assertEqual(status_after_lock_acquire(""), "ready")

    def test_advanced_statuses_are_preserved(self) -> None:
        # A re-acquisition must never rewind a task through its lifecycle.
        for status in ("ready", "blocked", "running", "review", "repair-needed", "done"):
            with self.subTest(status=status):
                self.assertEqual(status_after_lock_acquire(status), status)


class TryAcquireLocksStatusTests(_TargetCase):
    """Defect (a) through the loop/orchestrator seam (try_acquire_locks)."""

    def test_planned_task_becomes_ready(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="planned"))
        ok, blockers, _ = try_acquire_locks(tmp, TASK)
        self.assertTrue(ok)
        self.assertEqual(blockers, [])
        self.assertEqual(load_task(tmp, TASK)["status"], "ready")

    def test_review_task_stays_in_review(self) -> None:
        # The exact PR #179 rewind: a task with an open PR is in review; a
        # re-acquisition must leave it in review, not rewind to ready.
        tmp = self._target()
        self._write_task(tmp, _task(status="review"))
        ok, _, _ = try_acquire_locks(tmp, TASK)
        self.assertTrue(ok)
        self.assertEqual(load_task(tmp, TASK)["status"], "review")

    def test_running_and_repair_needed_are_preserved(self) -> None:
        for status in ("running", "repair-needed"):
            with self.subTest(status=status):
                tmp = self._target()
                self._write_task(tmp, _task(status=status))
                ok, _, _ = try_acquire_locks(tmp, TASK)
                self.assertTrue(ok)
                self.assertEqual(load_task(tmp, TASK)["status"], status)


class RefreshLockGrantTests(_TargetCase):
    """Defect (b): refresh a diverged grant, or refuse and name the owner."""

    def test_refresh_widens_grant_to_current_contract(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="review", locks=[CONTRACT, WIDENED]))
        self._write_grant(tmp, TASK, [CONTRACT])  # grant left behind by the old contract

        refreshed, conflicts, record = refresh_lock_grant(tmp, TASK)

        self.assertTrue(refreshed)
        self.assertEqual(conflicts, [])
        self.assertEqual(set(record["locks"]), {CONTRACT, WIDENED})
        self.assertEqual(set(lock_record(tmp, TASK)["locks"]), {CONTRACT, WIDENED})
        # A refresh is bookkeeping: it must not rewind the task's lifecycle.
        self.assertEqual(load_task(tmp, TASK)["status"], "review")

    def test_refresh_preserves_planned_status(self) -> None:
        # A passive refresh (triggered by a readiness check) must NOT advance the
        # lifecycle: only explicit acquisition promotes planned -> ready. Promoting
        # here would make the loop's planned-only unblock skip the task.
        tmp = self._target()
        self._write_task(tmp, _task(status="planned", locks=[CONTRACT, WIDENED]))
        self._write_grant(tmp, TASK, [CONTRACT])

        refreshed, _, _ = refresh_lock_grant(tmp, TASK)

        self.assertTrue(refreshed)
        self.assertEqual(load_task(tmp, TASK)["status"], "planned")

    def test_refresh_into_foreign_lock_is_refused_and_names_owner(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="review", locks=[CONTRACT, WIDENED]))
        self._write_grant(tmp, TASK, [CONTRACT])
        write_json(shiki_path(tmp, "locks", f"{OWNER}.json"), _active_lock(OWNER, [WIDENED]))

        refreshed, conflicts, record = refresh_lock_grant(tmp, TASK)

        self.assertFalse(refreshed)
        self.assertIsNone(record)
        self.assertTrue(any(OWNER in conflict for conflict in conflicts))
        # The grant is left untouched — never widened into a held lock (non-goal).
        self.assertEqual(lock_record(tmp, TASK)["locks"], [CONTRACT])


class EvaluateLockGrantTests(_TargetCase):
    """Defect (b) as the dispatch gate sees it (evaluate_lock_grant)."""

    def test_no_declared_locks_is_granted(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(locks=[]))
        self.assertEqual(evaluate_lock_grant(tmp, TASK), (True, None))

    def test_grant_covering_contract_is_granted_without_refresh(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="running", locks=[CONTRACT]))
        self._write_grant(tmp, TASK, [CONTRACT])

        granted, blocker = evaluate_lock_grant(tmp, TASK)

        self.assertTrue(granted)
        self.assertIsNone(blocker)
        # No divergence → no churn: the grant is not rewritten.
        self.assertNotIn("refreshed_at", lock_record(tmp, TASK))

    def test_never_acquired_reports_bare_not_granted(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(locks=[CONTRACT]))  # no grant record at all
        granted, blocker = evaluate_lock_grant(tmp, TASK)
        self.assertFalse(granted)
        self.assertEqual(blocker, "locks are not granted")

    def test_amendment_without_conflict_auto_refreshes(self) -> None:
        # Acceptance: amending a task's locks does not require a manual
        # re-acquisition before dispatch — the gate refreshes the grant itself.
        tmp = self._target()
        self._write_task(tmp, _task(status="review", locks=[CONTRACT, WIDENED]))
        self._write_grant(tmp, TASK, [CONTRACT])

        granted, blocker = evaluate_lock_grant(tmp, TASK)

        self.assertTrue(granted)
        self.assertIsNone(blocker)
        self.assertEqual(set(lock_record(tmp, TASK)["locks"]), {CONTRACT, WIDENED})

    def test_divergence_with_conflict_names_both_sets_and_owner(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="review", locks=[CONTRACT, WIDENED]))
        self._write_grant(tmp, TASK, [CONTRACT])
        write_json(shiki_path(tmp, "locks", f"{OWNER}.json"), _active_lock(OWNER, [WIDENED]))

        granted, blocker = evaluate_lock_grant(tmp, TASK)

        self.assertFalse(granted)
        self.assertNotEqual(blocker, "locks are not granted")
        self.assertIn("divergence", blocker)
        self.assertIn(CONTRACT, blocker)  # the granted set is named
        self.assertIn(WIDENED, blocker)  # the widened contract set is named
        self.assertIn(OWNER, blocker)  # the colliding owner is named


class CmdWiringTests(_TargetCase):
    """The CLI surfaces delegate to the helpers (defects a + b end to end)."""

    def setUp(self) -> None:
        # cmd_* commands require a GitHub-first target; the lock lifecycle logic is
        # independent of that preflight, so stub it out for a fast unit test.
        self._original = shiki_tasks.require_github_first_target
        shiki_tasks.require_github_first_target = lambda *_a, **_k: None
        self.addCleanup(setattr, shiki_tasks, "require_github_first_target", self._original)

    def _run(self, func, **kwargs) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = func(argparse.Namespace(**kwargs))
        return code, json.loads(buffer.getvalue())

    def test_lock_acquire_preserves_review_status(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="review", locks=[CONTRACT]))
        code, _ = self._run(cmd_lock_acquire, target=str(tmp), owner="shiki-cli", task_id=TASK)
        self.assertEqual(code, 0)
        self.assertEqual(load_task(tmp, TASK)["status"], "review")

    def test_dispatch_check_reports_divergence_not_bare_string(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="review", locks=[CONTRACT, WIDENED]))
        self._write_grant(tmp, TASK, [CONTRACT])
        write_json(shiki_path(tmp, "locks", f"{OWNER}.json"), _active_lock(OWNER, [WIDENED]))

        code, result = self._run(
            cmd_dispatch_check, target=str(tmp), task_id=TASK, require_worktree=False
        )

        self.assertEqual(code, 1)
        self.assertFalse(result["locks_granted"])
        divergence = [reason for reason in result["blocking_reasons"] if "divergence" in reason]
        self.assertEqual(len(divergence), 1)
        self.assertIn(OWNER, divergence[0])
        self.assertNotIn("locks are not granted", result["blocking_reasons"])


if __name__ == "__main__":
    unittest.main()
