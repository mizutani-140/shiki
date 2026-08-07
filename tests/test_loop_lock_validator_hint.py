"""validate_shiki loop-lock advisory — derive-at-judgment-time model.

Under the mirror-lock model (SADR-0016 / A-LOCKS) a registered task's stored
``locks`` are EXACTLY what the plan declared; nothing is injected. MergeGate
recomputes the task's id-scoped mirror set at judgment time
(``mergegate_check._derive_task_mirror_locks``) and unions it into the effective
locks passed to ``files_outside_locks``. A narrow-lock loop task is therefore the
intended, fully-covered state, so ``loop_lock_warnings`` must NOT warn about it,
and its advisory text must not claim a dispatch-time ``.shiki/**`` guarantee — no
such guarantee (``loop_guaranteed_locks``) ever existed. The advisory hook is kept
WARN-ONLY; the future mirror-identity rule (SADR-0016 step B / remediation R-02)
will decide any blocking behaviour.
"""

from __future__ import annotations

import unittest

import shiki_test_support  # noqa: F401  (path bootstrap)

from validate_shiki import loop_lock_warnings


GOAL = "G-20260617T031753970001Z-1de3b322"
TASK = "T-20260617T090739482036Z-c92b21b3"


def _task(*, runtime: str, locks: list[str], goal: str = GOAL) -> dict:
    return {"id": TASK, "goal_id": goal, "assigned_runtime": runtime, "locks": list(locks)}


class LoopLockAdvisoryTests(unittest.TestCase):
    def test_no_warning_for_narrow_lock_loop_task_on_active_goal(self) -> None:
        # The exact case the old advisory warned on. MergeGate now derives the
        # task's mirror set at judgment time, so a narrow-lock loop task is fully
        # covered and must not be flagged.
        goals = {GOAL: {"id": GOAL, "status": "ready"}}
        tasks = {GOAL: [_task(runtime="claude-code", locks=["path:scripts/x.py"])]}
        self.assertEqual(loop_lock_warnings(goals, tasks), [])

    def test_silent_across_loop_runtimes_and_active_statuses(self) -> None:
        # No active-goal / loop-runtime combination may reintroduce the warning.
        for runtime in ("claude-code", "codex"):
            for status in ("planned", "ready", "blocked"):
                goals = {GOAL: {"id": GOAL, "status": status}}
                tasks = {GOAL: [_task(runtime=runtime, locks=["path:scripts/x.py"])]}
                self.assertEqual(
                    loop_lock_warnings(goals, tasks),
                    [],
                    f"runtime={runtime} status={status} must not warn",
                )

    def test_no_warning_when_task_declares_shiki_lock(self) -> None:
        # A task may still declare path:.shiki/** as a circularity break; the
        # advisory neither requires nor forbids it (SADR-0016 step B / R-02).
        goals = {GOAL: {"id": GOAL, "status": "ready"}}
        tasks = {GOAL: [_task(runtime="claude-code", locks=["path:.shiki/**"])]}
        self.assertEqual(loop_lock_warnings(goals, tasks), [])

    def test_missing_goal_does_not_crash(self) -> None:
        tasks = {GOAL: [_task(runtime="claude-code", locks=["path:scripts/x.py"])]}
        self.assertEqual(loop_lock_warnings({}, tasks), [])


if __name__ == "__main__":
    unittest.main()
