"""Goal-level completion ledgers widen a closeout task's derived mirror locks.

``cmd_goal_complete`` writes the goal's completion ledger at GOAL level with
``task_id: null`` (it belongs to the goal, not to any single task), and its
``evidence`` cites the goal report ``.shiki/reports/R-*.json`` that carries the
scorecard. The goal loop lists that ledger in the closeout task's
``ledger_evidence`` and syncs both the ledger and the report onto the closeout
branch, so MergeGate's files-outside-locks gate must accept them.

``mergegate_check._derive_task_mirror_locks`` recomputes a task's id-scoped
mirror lock set at judgment time. Its ledger-expansion bound must therefore
accept a same-goal ledger whose ``task_id`` is empty (a goal-level ledger of the
task's own goal) while still rejecting every foreign reference — a ledger of
another goal, or a ledger explicitly owned by a DIFFERENT task (the anti-padding
property: a PR-authored task file must not pad ``ledger_evidence`` with someone
else's task ledger to inherit the .shiki paths its evidence references).

Regression origin: closeout PR #199 blocked with
``Changed file .shiki/ledger/L-...json is outside declared task locks`` and the
same for its ``.shiki/reports/R-...json``, even though the completion ledger was
listed in the closeout task's ``ledger_evidence`` and the report was the very
evidence that ledger cites. The earlier closeout PR #185 carried the identical
pair but merged only because its task declared ``path:.shiki/**``; the
lock-narrowing discipline (SADR-0016) replaced that directory glob with named
files, exposing the gap and making every goal-completing closeout unmergeable.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from mergegate_check import _derive_task_mirror_locks
from shiki_locks import files_outside_locks
from shiki_process import shiki_path, write_json

CONTROL_DIRS = (
    "goals",
    "tasks",
    "dag",
    "ledger",
    "locks",
    "worktrees",
    "reports",
    "runner",
)

GOAL_ID = "G-20260730T014952000000Z-completion"
TASK_ID = "T-20260730T014952356458Z-closeout"
LEDGER_ID = "L-20260730T014952356458Z-646217d8"
REPORT_REL = ".shiki/reports/R-20260730T014952341651Z-aa477a7e.json"


def _make_target() -> Path:
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(tmp)], check=True)
    for name in CONTROL_DIRS:
        (tmp / ".shiki" / name).mkdir(parents=True, exist_ok=True)
    return tmp


def _write_ledger(
    target: Path,
    ledger_id: str,
    *,
    goal_id: str,
    task_id: str | None,
    evidence: list[str],
    ledger_type: str = "completion",
) -> None:
    """Write a ledger file exactly as ``_derive_task_mirror_locks`` reads it.

    ``task_id=None`` writes ``"task_id": null`` — the shape ``cmd_goal_complete``
    produces for a goal-level completion ledger.
    """
    write_json(
        shiki_path(target, "ledger", f"{ledger_id}.json"),
        {
            "id": ledger_id,
            "goal_id": goal_id,
            "task_id": task_id,
            "type": ledger_type,
            "summary": f"{ledger_type} ledger",
            "evidence": list(evidence),
        },
    )


def _task(
    *,
    task_id: str = TASK_ID,
    goal_id: str = GOAL_ID,
    ledger_evidence: list[str] | None = None,
    locks: list[str] | None = None,
) -> dict:
    return {
        "id": task_id,
        "goal_id": goal_id,
        "ledger_evidence": list(ledger_evidence or []),
        "locks": list(locks or []),
    }


class GoalLevelCompletionLedgerTests(unittest.TestCase):
    """A goal-level completion ledger of the task's OWN goal expands to cover
    both its own path and every ``.shiki``-contained path in its evidence."""

    def test_goal_level_completion_ledger_widens_ledger_and_report(self) -> None:
        target = _make_target()
        _write_ledger(target, LEDGER_ID, goal_id=GOAL_ID, task_id=None, evidence=[REPORT_REL])
        task = _task(ledger_evidence=[LEDGER_ID])

        derived = _derive_task_mirror_locks(target, task)

        self.assertIn(f"path:.shiki/ledger/{LEDGER_ID}.json", derived)
        self.assertIn(f"path:{REPORT_REL}", derived)


class ForeignGoalLedgerTests(unittest.TestCase):
    """A completion ledger whose ``goal_id`` is foreign contributes nothing:
    neither its own path nor its evidence refs widen the derived set."""

    def test_foreign_goal_completion_ledger_contributes_nothing(self) -> None:
        target = _make_target()
        _write_ledger(target, LEDGER_ID, goal_id="G-FOREIGN", task_id=None, evidence=[REPORT_REL])
        task = _task(ledger_evidence=[LEDGER_ID])

        derived = _derive_task_mirror_locks(target, task)

        self.assertNotIn(f"path:.shiki/ledger/{LEDGER_ID}.json", derived)
        self.assertNotIn(f"path:{REPORT_REL}", derived)


class ForeignTaskLedgerTests(unittest.TestCase):
    """Anti-padding: a same-goal ledger explicitly owned by a DIFFERENT task
    still contributes nothing, so a PR-authored task file cannot pad
    ``ledger_evidence`` with another task's ledger to inherit its evidence paths.
    This property must never regress when the bound is loosened for the
    ``task_id`` empty (goal-level) case."""

    def test_different_task_id_same_goal_contributes_nothing(self) -> None:
        target = _make_target()
        _write_ledger(
            target,
            LEDGER_ID,
            goal_id=GOAL_ID,
            task_id="T-SOME-OTHER-TASK",
            evidence=[REPORT_REL],
        )
        task = _task(ledger_evidence=[LEDGER_ID])

        derived = _derive_task_mirror_locks(target, task)

        self.assertNotIn(f"path:.shiki/ledger/{LEDGER_ID}.json", derived)
        self.assertNotIn(f"path:{REPORT_REL}", derived)


class GoalLevelEvidenceContainmentTests(unittest.TestCase):
    """An accepted goal-level ledger still filters each evidence ref: a ref that
    escapes the ``.shiki`` subtree, or is not ``.shiki``-prefixed, is dropped.
    The ledger's own path is still covered (it is an accepted same-goal ledger);
    only the bad evidence ref contributes nothing."""

    def test_evidence_ref_escaping_shiki_is_dropped(self) -> None:
        target = _make_target()
        _write_ledger(
            target,
            LEDGER_ID,
            goal_id=GOAL_ID,
            task_id=None,
            evidence=[".shiki/../outside.json"],
        )
        task = _task(ledger_evidence=[LEDGER_ID])

        derived = _derive_task_mirror_locks(target, task)

        self.assertIn(f"path:.shiki/ledger/{LEDGER_ID}.json", derived)
        self.assertNotIn("path:.shiki/../outside.json", derived)
        self.assertFalse(any("outside.json" in lock for lock in derived), derived)

    def test_evidence_ref_without_shiki_prefix_is_dropped(self) -> None:
        target = _make_target()
        # A ref that does not start with ".shiki/" — e.g. a bare product path —
        # is not a synced mirror artifact and must never widen the mirror locks.
        _write_ledger(
            target,
            LEDGER_ID,
            goal_id=GOAL_ID,
            task_id=None,
            evidence=["reports/R-not-prefixed.json"],
        )
        task = _task(ledger_evidence=[LEDGER_ID])

        derived = _derive_task_mirror_locks(target, task)

        self.assertIn(f"path:.shiki/ledger/{LEDGER_ID}.json", derived)
        self.assertNotIn("path:reports/R-not-prefixed.json", derived)
        self.assertFalse(any("not-prefixed" in lock for lock in derived), derived)


class CloseoutFilesOutsideLocksTests(unittest.TestCase):
    """End-to-end, shaped like PR #199: a closeout task with three named-file
    product locks, whose diff carries its id-scoped mirror files plus the
    goal-level completion ledger and its scorecard report, produces no
    files-outside-locks blocker through the real gate."""

    PRODUCT_LOCKS = [
        "path:scripts/mergegate_check.py",
        "path:scripts/shiki_tasks.py",
        "path:tests/test_completion_lock_scope.py",
    ]

    def _outside_lock_blockers(self, target: Path, task: dict, changed: list[str]) -> list[str]:
        # Reproduce mergegate_check's files-outside-locks gate exactly: union the
        # declared product locks with the read-time derived mirror set, then flag
        # every changed file that no effective lock covers.
        effective_locks = list(
            dict.fromkeys([*[str(lock) for lock in task.get("locks") or []], *_derive_task_mirror_locks(target, task)])
        )
        return [f"Changed file {path} is outside declared task locks" for path in files_outside_locks(changed, effective_locks)]

    def test_closeout_diff_has_no_files_outside_locks_blocker(self) -> None:
        target = _make_target()
        _write_ledger(target, LEDGER_ID, goal_id=GOAL_ID, task_id=None, evidence=[REPORT_REL])
        write_json(shiki_path(target, "reports", Path(REPORT_REL).name), {"id": Path(REPORT_REL).stem, "goal_id": GOAL_ID})
        task = _task(ledger_evidence=[LEDGER_ID], locks=self.PRODUCT_LOCKS)

        changed = [
            f".shiki/tasks/{TASK_ID}.json",
            f".shiki/locks/{TASK_ID}.json",
            f".shiki/worktrees/{TASK_ID}.json",
            f".shiki/goals/{GOAL_ID}.json",
            f".shiki/dag/{GOAL_ID}.json",
            f".shiki/ledger/{LEDGER_ID}.json",
            REPORT_REL,
        ]

        self.assertEqual(self._outside_lock_blockers(target, task, changed), [])

    def test_without_derivation_the_ledger_and_report_would_block(self) -> None:
        # Proves the derivation is doing the work: the declared product locks
        # alone do not cover the synced completion ledger or its report.
        target = _make_target()
        _write_ledger(target, LEDGER_ID, goal_id=GOAL_ID, task_id=None, evidence=[REPORT_REL])
        changed = [f".shiki/ledger/{LEDGER_ID}.json", REPORT_REL]

        outside = files_outside_locks(changed, self.PRODUCT_LOCKS)

        self.assertEqual(sorted(outside), sorted(changed))


if __name__ == "__main__":
    unittest.main()
