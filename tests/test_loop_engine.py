"""Goal loop decision engine contract (G-D, ADR 0008/0009).

Every decision branch of the pure engine is covered by state fixtures: no
filesystem, git, or GitHub access happens in these tests.
"""

from __future__ import annotations

import unittest

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_loop import AUTO_MERGE_RISKS, decide_goal_action, decide_task_action

REQUIRED = [
    "Validate Shiki mirror",
    "CCA verdict",
    "MergeGate metadata check",
    "MergeGate policy check",
]


def task(status: str, *, risk: str = "low", task_id: str = "T-0001", pr: int | None = 7) -> dict:
    return {
        "id": task_id,
        "goal_id": "G-0001",
        "status": status,
        "risk_level": risk,
        "expected_pr": pr,
        "assigned_runtime": "claude-code",
    }


def decide(t, *, checks=None, pr_state=None, attempts=0, limit=3, reruns=0):
    return decide_task_action(
        t,
        checks=checks,
        pr_state=pr_state,
        repair_attempts=attempts,
        repair_limit=limit,
        required_checks=REQUIRED,
        cca_reruns=reruns,
    )


def green():
    return {name: "pass" for name in REQUIRED}


class TaskDecisionTests(unittest.TestCase):
    def test_ready_dispatches(self) -> None:
        self.assertEqual(decide(task("ready"))["action"], "dispatch")

    def test_running_waits(self) -> None:
        self.assertEqual(decide(task("running"))["action"], "wait_runner")

    def test_planned_waits_for_dependencies(self) -> None:
        self.assertEqual(decide(task("planned"))["action"], "wait_dependencies")

    def test_done_is_none(self) -> None:
        self.assertEqual(decide(task("done"))["action"], "none")

    def test_review_without_pr_creates_pr(self) -> None:
        self.assertEqual(decide(task("review"), pr_state=None)["action"], "create_pr")

    def test_merged_pr_marks_done(self) -> None:
        decision = decide(task("review"), pr_state={"merged": True}, checks=green())
        self.assertEqual(decision["action"], "mark_done")

    def test_pending_checks_wait(self) -> None:
        checks = green()
        checks["CCA verdict"] = "pending"
        decision = decide(task("review"), pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "wait_checks")

    def test_missing_check_counts_as_pending(self) -> None:
        checks = green()
        del checks["MergeGate policy check"]
        decision = decide(task("review"), pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "wait_checks")

    def test_green_low_risk_merges(self) -> None:
        decision = decide(task("review", risk="low"), pr_state={"merged": False}, checks=green())
        self.assertEqual(decision["action"], "merge")

    def test_green_medium_risk_merges(self) -> None:
        self.assertIn("medium", AUTO_MERGE_RISKS)
        decision = decide(task("review", risk="medium"), pr_state={"merged": False}, checks=green())
        self.assertEqual(decision["action"], "merge")

    def test_green_high_risk_stops_for_guardian(self) -> None:
        decision = decide(task("review", risk="high"), pr_state={"merged": False}, checks=green())
        self.assertEqual(decision["action"], "stop_guardian")

    def test_cca_only_failure_reruns(self) -> None:
        checks = green()
        checks["CCA verdict"] = "fail"
        checks["MergeGate policy check"] = "fail"
        decision = decide(task("review"), pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "rerun_cca")

    def test_cca_failure_with_pending_siblings_waits(self) -> None:
        checks = green()
        checks["CCA verdict"] = "fail"
        checks["Validate Shiki mirror"] = "pending"
        decision = decide(task("review"), pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "wait_checks")

    def test_cca_rerun_budget_exhausted_repairs(self) -> None:
        checks = green()
        checks["CCA verdict"] = "fail"
        decision = decide(task("review"), pr_state={"merged": False}, checks=checks, reruns=2)
        self.assertEqual(decision["action"], "dispatch_repair")

    def test_non_cca_failure_dispatches_repair(self) -> None:
        checks = green()
        checks["Validate Shiki mirror"] = "fail"
        decision = decide(task("review"), pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "dispatch_repair")
        self.assertEqual(decision["failed_checks"], ["Validate Shiki mirror"])

    def test_repair_limit_stops_for_guardian(self) -> None:
        checks = green()
        checks["Validate Shiki mirror"] = "fail"
        decision = decide(task("review"), pr_state={"merged": False}, checks=checks, attempts=3)
        self.assertEqual(decision["action"], "stop_guardian")

    def test_repair_needed_dispatches_repair(self) -> None:
        self.assertEqual(decide(task("repair-needed"))["action"], "dispatch_repair")

    def test_repair_needed_at_limit_stops(self) -> None:
        self.assertEqual(decide(task("repair-needed"), attempts=3)["action"], "stop_guardian")

    def test_repair_needed_without_pr_fails_closed(self) -> None:
        decision = decide(task("repair-needed", pr=None))
        self.assertEqual(decision["action"], "stop_blocked")

    def test_missing_risk_level_fails_closed(self) -> None:
        fixture = task("review")
        del fixture["risk_level"]
        decision = decide(fixture, pr_state={"merged": False}, checks=green())
        self.assertEqual(decision["action"], "stop_guardian")

    def test_pr_state_error_waits(self) -> None:
        decision = decide(task("review"), pr_state={"number": 7, "error": True}, checks={})
        self.assertEqual(decision["action"], "wait_checks")


class GoalDecisionTests(unittest.TestCase):
    def test_all_done_completes_goal(self) -> None:
        tasks = [task("done"), task("done", task_id="T-0002")]
        decision = decide_goal_action([decide(t) for t in tasks], tasks)
        self.assertEqual(decision["action"], "goal_complete")

    def test_stop_takes_precedence(self) -> None:
        tasks = [task("repair-needed"), task("ready", task_id="T-0002")]
        decisions = [decide(tasks[0], attempts=3), decide(tasks[1])]
        self.assertEqual(decide_goal_action(decisions, tasks)["action"], "stop_guardian")

    def test_merge_beats_dispatch(self) -> None:
        tasks = [task("review"), task("ready", task_id="T-0002")]
        decisions = [decide(tasks[0], pr_state={"merged": False}, checks=green()), decide(tasks[1])]
        self.assertEqual(decide_goal_action(decisions, tasks)["action"], "merge")

    def test_blocked_only_tasks_trigger_unblock(self) -> None:
        tasks = [task("done"), task("planned", task_id="T-0002")]
        decisions = [decide(t) for t in tasks]
        self.assertEqual(decide_goal_action(decisions, tasks)["action"], "unblock")

    def test_waiting_goal_waits(self) -> None:
        tasks = [task("review")]
        checks = green()
        checks["CCA verdict"] = "pending"
        decisions = [decide(tasks[0], pr_state={"merged": False}, checks=checks)]
        self.assertEqual(decide_goal_action(decisions, tasks)["action"], "wait_checks")


if __name__ == "__main__":
    unittest.main()
