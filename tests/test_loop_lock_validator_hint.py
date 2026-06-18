"""T4 — validate_shiki loop-lock WARN-ONLY hint.

validate_shiki must WARN (never error) when a loop-executed task of an ACTIVE
goal lacks .shiki/** lock coverage. The warning is scoped:
  - only loop-executed runtimes (claude-code/codex),
  - only goals that are still active (not complete/archived/historical),
so it does not retroactively spam the ~40 pre-existing terminal-goal tasks.
"""

from __future__ import annotations

import unittest

import shiki_test_support  # noqa: F401  (path bootstrap)

from validate_shiki import loop_lock_warnings


GOAL = "G-20260617T031753970001Z-1de3b322"
TASK = "T-20260617T090739482036Z-c92b21b3"


def _task(*, runtime: str, locks: list[str], goal: str = GOAL) -> dict:
    return {"id": TASK, "goal_id": goal, "assigned_runtime": runtime, "locks": list(locks)}


class LoopLockWarningTests(unittest.TestCase):
    def test_warns_for_loop_task_missing_shiki_coverage_on_active_goal(self) -> None:
        goals = {GOAL: {"id": GOAL, "status": "ready"}}
        tasks = {GOAL: [_task(runtime="claude-code", locks=["path:scripts/x.py"])]}
        warnings = loop_lock_warnings(goals, tasks)
        self.assertEqual(len(warnings), 1)
        self.assertIn(TASK, warnings[0])
        self.assertIn(".shiki/**", warnings[0])

    def test_no_warning_when_loop_task_covers_shiki(self) -> None:
        goals = {GOAL: {"id": GOAL, "status": "ready"}}
        tasks = {GOAL: [_task(runtime="codex", locks=["path:.shiki/**"])]}
        self.assertEqual(loop_lock_warnings(goals, tasks), [])

    def test_no_warning_for_non_loop_runtime(self) -> None:
        goals = {GOAL: {"id": GOAL, "status": "ready"}}
        tasks = {GOAL: [_task(runtime="human", locks=["path:docs/**"])]}
        self.assertEqual(loop_lock_warnings(goals, tasks), [])

    def test_scoped_out_for_terminal_or_archived_goals(self) -> None:
        # The ~40 pre-existing tasks live under complete/archived/historical goals;
        # the warning must not fire for them.
        for status in ("complete", "archived", "historical"):
            goals = {GOAL: {"id": GOAL, "status": status}}
            tasks = {GOAL: [_task(runtime="claude-code", locks=["path:scripts/x.py"])]}
            self.assertEqual(
                loop_lock_warnings(goals, tasks), [], f"status={status} must be scoped out"
            )

    def test_missing_goal_does_not_crash(self) -> None:
        tasks = {GOAL: [_task(runtime="claude-code", locks=["path:scripts/x.py"])]}
        # No goal payload at all: be conservative and do not warn (can't prove active).
        self.assertEqual(loop_lock_warnings({}, tasks), [])


if __name__ == "__main__":
    unittest.main()
