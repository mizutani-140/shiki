"""Goal loop decision engine contract (G-D, SADR-0008/SADR-0009).

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

    def test_merged_impl_pr_opens_closeout(self) -> None:
        # SADR-0012: a merged impl PR (no closeout_pr yet) routes to a closeout PR
        # that pushes completion to main — NOT a local-only mark_done.
        decision = decide(task("review"), pr_state={"merged": True}, checks=green())
        self.assertEqual(decision["action"], "create_closeout_pr")

    def test_reopened_ready_task_cuts_fresh_implementation_not_closeout(self) -> None:
        # Measured 2026-08-06 (PR #308 / T-...dbdae1cf): a Spec Amendment re-opened a
        # task to `ready` while its expected_pr still named the merged closeout PR.
        # A non-terminal status + a merged pointer is a STALE pointer from a previous
        # cycle: the loop must cut a fresh implementation (dispatch + clear the stale
        # pointer), NEVER read the merged pointer as done and open a source-free
        # closeout on the first cycle.
        decision = decide(task("ready"), pr_state={"merged": True})
        self.assertEqual(decision["action"], "dispatch")
        self.assertNotEqual(decision["action"], "create_closeout_pr")
        self.assertTrue(decision.get("clear_expected_pr"))

    def test_reopened_planned_task_waits_not_closeout(self) -> None:
        # The same holds for a re-opened task still waiting on dependencies.
        decision = decide(task("planned"), pr_state={"merged": True})
        self.assertEqual(decision["action"], "wait_dependencies")
        self.assertNotEqual(decision["action"], "create_closeout_pr")

    def test_ready_with_unmerged_pr_dispatches_without_clearing(self) -> None:
        # An UNMERGED expected_pr on a non-terminal task is untouched.
        decision = decide(task("ready"), pr_state={"merged": False})
        self.assertEqual(decision["action"], "dispatch")
        self.assertFalse(decision.get("clear_expected_pr"))

    def test_merged_closeout_pr_marks_done(self) -> None:
        # Once the closeout PR (expected_pr repointed, closeout_pr set) merges, the
        # completion is durable on main, so the task is marked done.
        t = {**task("review"), "closeout_pr": 9}
        decision = decide(t, pr_state={"merged": True}, checks=green())
        self.assertEqual(decision["action"], "mark_done")

    def test_closeout_pr_pending_waits(self) -> None:
        checks = green()
        checks["CCA verdict"] = "pending"
        t = {**task("review"), "closeout_pr": 9}
        decision = decide(t, pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "wait_checks")

    def test_closeout_pr_green_merges(self) -> None:
        t = {**task("review"), "closeout_pr": 9}
        decision = decide(t, pr_state={"merged": False}, checks=green())
        self.assertEqual(decision["action"], "merge")

    def test_closeout_pr_failed_check_stops_no_repair(self) -> None:
        # A bookkeeping closeout has no implementation to repair -> fail closed.
        checks = green()
        checks["Validate Shiki mirror"] = "fail"
        t = {**task("review"), "closeout_pr": 9}
        decision = decide(t, pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "stop_blocked")

    def test_closeout_pr_cca_race_reruns(self) -> None:
        # Only CCA red against green siblings on the closeout PR -> one rerun.
        checks = green()
        checks["CCA verdict"] = "fail"
        t = {**task("review"), "closeout_pr": 9}
        decision = decide(t, pr_state={"merged": False}, checks=checks, reruns=0)
        self.assertEqual(decision["action"], "rerun_cca")

    def test_closeout_pr_cca_fail_policy_skipped_reruns(self) -> None:
        # The actual #155 shape: CCA red, MergeGate policy gate gated-behind-CCA so
        # it is skipped/missing (-> pending). The policy gate MUST be stripped so
        # this stays a CCA-only race (one rerun), not a multi-check stop_blocked.
        checks = {"Validate Shiki mirror": "pass", "CCA verdict": "fail",
                  "MergeGate metadata check": "pass"}  # policy omitted -> pending
        t = {**task("review"), "closeout_pr": 9}
        decision = decide(t, pr_state={"merged": False}, checks=checks, reruns=0)
        self.assertEqual(decision["action"], "rerun_cca")

    def test_closeout_pr_cca_fail_policy_fail_reruns(self) -> None:
        checks = green()
        checks["CCA verdict"] = "fail"
        checks["MergeGate policy check"] = "fail"
        t = {**task("review"), "closeout_pr": 9}
        decision = decide(t, pr_state={"merged": False}, checks=checks, reruns=0)
        self.assertEqual(decision["action"], "rerun_cca")

    def test_closeout_pr_cca_fail_with_other_pending_waits(self) -> None:
        # CCA red but a real (non-policy) sibling still pending -> wait, not rerun.
        checks = green()
        checks["CCA verdict"] = "fail"
        checks["Validate Shiki mirror"] = "pending"
        t = {**task("review"), "closeout_pr": 9}
        decision = decide(t, pr_state={"merged": False}, checks=checks, reruns=0)
        self.assertEqual(decision["action"], "wait_checks")

    def test_closeout_pr_rerun_budget_exhausted_stops(self) -> None:
        checks = green()
        checks["CCA verdict"] = "fail"
        t = {**task("review"), "closeout_pr": 9}
        decision = decide(t, pr_state={"merged": False}, checks=checks, reruns=2)
        self.assertEqual(decision["action"], "stop_blocked")

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

    def test_green_high_risk_merges_via_policy_guardian_gate(self) -> None:
        # The MergeGate policy check (a required check) is the Guardian gate; when
        # green, Guardian approval was recorded (human or external AI), so the
        # loop may merge high/critical autonomously (SADR-0010).
        decision = decide(task("review", risk="high"), pr_state={"merged": False}, checks=green())
        self.assertEqual(decision["action"], "merge")

    def test_green_high_risk_without_policy_gate_stops_for_guardian(self) -> None:
        decision = decide_task_action(
            task("review", risk="high"),
            checks={"Validate Shiki mirror": "pass"},
            pr_state={"merged": False},
            repair_attempts=0,
            repair_limit=3,
            required_checks=["Validate Shiki mirror"],
        )
        self.assertEqual(decision["action"], "stop_guardian")

    def test_cca_only_failure_reruns(self) -> None:
        checks = green()
        checks["CCA verdict"] = "fail"
        checks["MergeGate policy check"] = "fail"
        decision = decide(task("review"), pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "rerun_cca")

    def test_lone_policy_gate_failure_stops_for_guardian(self) -> None:
        # CCA is green but the MergeGate policy Guardian gate failed on its own:
        # the governance gate said NO. The loop must stop for a recorded
        # authority, never rerun (CCA is already green) or auto-repair past it.
        checks = green()
        checks["MergeGate policy check"] = "fail"
        decision = decide(task("review", risk="high"), pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "stop_guardian")

    def test_lone_policy_gate_failure_not_repaired_even_at_repair_budget(self) -> None:
        # Even with repair budget remaining, a failing Guardian gate is never
        # dispatched as a bounded repair.
        checks = green()
        checks["MergeGate policy check"] = "fail"
        decision = decide(task("review", risk="low"), pr_state={"merged": False}, checks=checks, attempts=0)
        self.assertEqual(decision["action"], "stop_guardian")

    def test_policy_gate_stripped_from_mixed_failure_repair(self) -> None:
        # When CCA AND a non-chain check AND the Guardian gate all fail, the loop
        # repairs the genuine failures but NEVER hands the Guardian gate to the
        # runner as a repair item (would re-enable the impersonation pathway).
        checks = green()
        checks["CCA verdict"] = "fail"
        checks["MergeGate policy check"] = "fail"
        checks["Validate Shiki mirror"] = "fail"
        decision = decide(task("review", risk="critical"), pr_state={"merged": False}, checks=checks)
        self.assertEqual(decision["action"], "dispatch_repair")
        self.assertIn("Validate Shiki mirror", decision["failed_checks"])
        self.assertIn("CCA verdict", decision["failed_checks"])
        self.assertNotIn("MergeGate policy check", decision["failed_checks"])

    def test_policy_gate_stripped_from_cca_race_repair_after_reruns(self) -> None:
        # The sanctioned CCA-completion race ({CCA verdict, policy gate}) reruns
        # while budget remains; once exhausted it repairs CCA but the Guardian
        # gate is still never a repair item.
        checks = green()
        checks["CCA verdict"] = "fail"
        checks["MergeGate policy check"] = "fail"
        decision = decide(task("review", risk="high"), pr_state={"merged": False}, checks=checks, reruns=2)
        self.assertEqual(decision["action"], "dispatch_repair")
        self.assertEqual(decision["failed_checks"], ["CCA verdict"])
        self.assertNotIn("MergeGate policy check", decision["failed_checks"])

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
