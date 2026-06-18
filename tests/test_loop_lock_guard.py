"""T4 — loop task-lock guard (PRD 0002, gap #5, Q5).

A loop-executed task (claude-code/codex) is dispatched into an isolated worktree
where the goal loop syncs the FULL .shiki evidence set onto the task branch
(see shiki_loop._sync_state_to_branch / _evidence_relatives_for_task). That sync
writes task files, the worktree record, ledgers, runner/EXEC records and reports
into the branch's .shiki tree. So a loop task must hold a lock that covers
.shiki/** — otherwise the branch carries .shiki mutations outside the task's
declared locks and MergeGate's files-outside-locks gate trips.

Design decision (round-1 verify finding): do NOT auto-mutate the registered task
file's locks (that breaks goal_reconcile frozen-plan lock-match and retroactively
warns 40 existing tasks). Instead:

  (1) validate_shiki WARNS (never errors) when a loop-executed task of an ACTIVE
      goal lacks .shiki/** coverage, scoped so it does not spam terminal/archived
      goals or non-loop tasks; and
  (2) the loop GUARANTEES .shiki/** coverage at dispatch / worktree-allocation
      time (lock record + worktree record), without touching the task file.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_tasks import (
    LOOP_SHIKI_STATE_LOCK,
    allocate_worktree_record,
    is_loop_executed_runtime,
    locks_cover_shiki_state,
    try_acquire_locks,
)


GOAL = "G-20260617T031753970001Z-1de3b322"
TASK = "T-20260617T090739482036Z-c92b21b3"


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _seed_target(tmp: Path, *, runtime: str, locks: list[str]) -> dict:
    """Minimal .shiki mirror with one loop task and its goal."""
    (tmp / ".shiki" / "locks").mkdir(parents=True, exist_ok=True)
    (tmp / ".shiki" / "ledger").mkdir(parents=True, exist_ok=True)
    (tmp / ".shiki" / "worktrees").mkdir(parents=True, exist_ok=True)
    task = {
        "id": TASK,
        "goal_id": GOAL,
        "title": "loop task",
        "scope": "x",
        "non_goals": [],
        "dependencies": [],
        "locks": list(locks),
        "assigned_runtime": runtime,
        "risk_level": "medium",
        "acceptance_checks": ["a"],
        "expected_branch": "shiki/t4-probe",
        "ledger_evidence": ["L-seed"],
        "status": "planned",
    }
    _write(tmp / ".shiki" / "tasks" / f"{TASK}.json", task)
    return task


class CoverageHelperTests(unittest.TestCase):
    def test_canonical_lock_covers_state(self) -> None:
        self.assertTrue(locks_cover_shiki_state([LOOP_SHIKI_STATE_LOCK]))
        self.assertEqual(LOOP_SHIKI_STATE_LOCK, "path:.shiki/**")

    def test_partial_shiki_subtree_does_not_cover(self) -> None:
        # A task that only locks one .shiki subtree does NOT cover the full
        # evidence set the loop syncs (tasks + worktrees + ledger + runner + reports).
        self.assertFalse(locks_cover_shiki_state(["path:.shiki/tasks/**"]))
        self.assertFalse(
            locks_cover_shiki_state(["path:.shiki/goals/**", "path:.shiki/tasks/**"])
        )

    def test_trailing_slash_form_covers(self) -> None:
        self.assertTrue(locks_cover_shiki_state(["path:.shiki/"]))

    def test_unrelated_locks_do_not_cover(self) -> None:
        self.assertFalse(locks_cover_shiki_state([]))
        self.assertFalse(locks_cover_shiki_state(["path:scripts/shiki_tasks.py"]))


class LoopRuntimeClassificationTests(unittest.TestCase):
    def test_loop_runtimes(self) -> None:
        self.assertTrue(is_loop_executed_runtime("claude-code"))
        self.assertTrue(is_loop_executed_runtime("codex"))

    def test_non_loop_runtimes(self) -> None:
        # Reviewers/action/CCA/human/placeholder runtimes are not dispatched into
        # a loop worktree, so they must not be flagged.
        self.assertFalse(is_loop_executed_runtime("claude-code-action"))
        self.assertFalse(is_loop_executed_runtime("human"))
        self.assertFalse(is_loop_executed_runtime("other"))
        self.assertFalse(is_loop_executed_runtime("hermes-runner"))


if __name__ == "__main__":
    unittest.main()
