"""A re-opened task must not read a previous cycle's PR pointer as this cycle's
completed work (``shiki_loop.decide_task_action``).

The goal loop repoints ``expected_pr`` at a task's closeout PR; a Spec Amendment can
then re-open the task (status -> planned/ready) while that now-merged pointer
survives, frozen to base by MergeGate (``mergegate_check._AMENDMENT_TASK_FROZEN_FIELDS``
freezes ``expected_pr``/``closeout_pr``/``expected_branch``/``ledger_evidence``). A
non-terminal status and a MERGED ``expected_pr`` cannot both describe the current
cycle — no PR has been cut for this cycle's work — so the pointer is stale: the task
must cut a fresh implementation, never a closeout that ships no source change.

A ``review`` task with a merged ``expected_pr`` is the normal path INTO the closeout
and is deliberately unchanged. Detection is from the TASK RECORD ALONE — the status
plus the pointer's merge state — never an amendment marker, a ledger scan or a PR
timestamp.

Measured 2026-08-06 on PR #308 / task ``T-20260806T065143409817Z-dbdae1cf``: an
amendment merged, the loop was re-dispatched, and it went straight to
``create_closeout_pr`` (its ``expected_pr`` named the merged closeout PR #309, its
``closeout_pr`` unset because the closeout branch pushes only ``expected_pr``) — the
amended contract silently unimplemented, with the CCA confirming ``complete``.
"""

from __future__ import annotations

import unittest

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_loop import decide_task_action

REQUIRED = [
    "Validate Shiki mirror",
    "CCA verdict",
    "MergeGate metadata check",
    "MergeGate policy check",
]

# The closeout PR number the loop repointed expected_pr at last cycle.
CLOSEOUT_PR = 309
MERGED = {"number": CLOSEOUT_PR, "merged": True}
OPEN = {"number": CLOSEOUT_PR, "merged": False}


def _task(status: str, *, expected_pr: int | None = CLOSEOUT_PR, closeout_pr: int | None = None) -> dict:
    task = {
        "id": "T-0001",
        "goal_id": "G-0001",
        "status": status,
        "risk_level": "low",
        "expected_pr": expected_pr,
        "assigned_runtime": "claude-code",
    }
    if closeout_pr is not None:
        task["closeout_pr"] = closeout_pr
    return task


def _decide(task: dict, *, pr_state: dict | None, checks: dict | None = None) -> dict:
    return decide_task_action(
        task,
        checks=checks,
        pr_state=pr_state,
        repair_attempts=0,
        repair_limit=3,
        required_checks=REQUIRED,
        cca_reruns=0,
    )


class ReopenedNonTerminalTaskTests(unittest.TestCase):
    """A non-terminal task (planned/ready) carrying a MERGED expected_pr is holding a
    stale pointer: cut a fresh implementation, never a closeout."""

    def test_ready_with_merged_pointer_cuts_fresh_implementation(self):
        decision = _decide(_task("ready"), pr_state=MERGED)
        self.assertEqual(decision["action"], "dispatch", decision)
        self.assertNotEqual(decision["action"], "create_closeout_pr")
        self.assertTrue(
            decision.get("clear_expected_pr"),
            "a ready task with a merged pointer must signal the stale pointer be cleared",
        )

    def test_planned_with_merged_pointer_does_not_open_closeout(self):
        decision = _decide(_task("planned"), pr_state=MERGED)
        self.assertEqual(decision["action"], "wait_dependencies", decision)
        self.assertNotEqual(decision["action"], "create_closeout_pr")


class UnmergedPointerUntouchedTests(unittest.TestCase):
    """An UNMERGED expected_pr on a non-terminal task is untouched — no clearing."""

    def test_ready_with_unmerged_pointer_dispatches_without_clearing(self):
        decision = _decide(_task("ready"), pr_state=OPEN)
        self.assertEqual(decision["action"], "dispatch", decision)
        self.assertFalse(decision.get("clear_expected_pr"), decision)

    def test_ready_without_any_pr_dispatches_without_clearing(self):
        # The ordinary fresh task: no expected_pr, no PR snapshot.
        decision = _decide(_task("ready", expected_pr=None), pr_state=None)
        self.assertEqual(decision["action"], "dispatch", decision)
        self.assertFalse(decision.get("clear_expected_pr"), decision)

    def test_planned_with_unmerged_pointer_waits_without_clearing(self):
        decision = _decide(_task("planned"), pr_state=OPEN)
        self.assertEqual(decision["action"], "wait_dependencies", decision)
        self.assertFalse(decision.get("clear_expected_pr"), decision)


class ReviewPathUnchangedTests(unittest.TestCase):
    """A ``review`` task with a merged expected_pr is the normal path into the
    closeout and must keep working exactly as today."""

    def test_review_with_merged_pointer_opens_closeout(self):
        decision = _decide(_task("review"), pr_state=MERGED)
        self.assertEqual(decision["action"], "create_closeout_pr", decision)
        self.assertFalse(decision.get("clear_expected_pr"), decision)

    def test_review_with_closeout_pr_and_merged_pointer_marks_done(self):
        decision = _decide(_task("review", closeout_pr=310), pr_state=MERGED)
        self.assertEqual(decision["action"], "mark_done", decision)
        self.assertFalse(decision.get("clear_expected_pr"), decision)


if __name__ == "__main__":
    unittest.main()
