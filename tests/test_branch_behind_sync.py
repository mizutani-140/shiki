"""A PR branch that fell BEHIND its base is synced, not stopped (G — sync_branch).

Branch protection that requires branches to be up to date rejects ``gh pr merge``
when ``mergeStateStatus`` is ``BEHIND`` even though every required check is green
and no real conflict exists. The loop must recognise that state and sync the base
into the branch (a plain merge + push — never a rebase or force-push) before
merging, so an unattended run resolves mechanical staleness on its own and only
stops for a genuine conflict or a push failure.

The decision cases exercise the pure engine with fixtures (no git). The effector
cases drive the REAL ``_sync_branch`` against a REAL temporary git repository with
a bare origin, so the merge/abort/push behaviour is proven, not mocked.
"""

from __future__ import annotations

import argparse
import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_loop
from shiki_loop import decide_task_action

REQUIRED = [
    "Validate Shiki mirror",
    "CCA verdict",
    "MergeGate metadata check",
    "MergeGate policy check",
]


def _task(status: str = "review", *, risk: str = "low", pr: int | None = 7, **extra) -> dict:
    task = {
        "id": "T-0001",
        "goal_id": "G-0001",
        "status": status,
        "risk_level": risk,
        "expected_pr": pr,
        "assigned_runtime": "claude-code",
    }
    task.update(extra)
    return task


def _decide(task, *, checks=None, pr_state=None, attempts=0, limit=3, reruns=0):
    return decide_task_action(
        task,
        checks=checks,
        pr_state=pr_state,
        repair_attempts=attempts,
        repair_limit=limit,
        required_checks=REQUIRED,
        cca_reruns=reruns,
    )


def _green() -> dict:
    return {name: "pass" for name in REQUIRED}


def _pr(merge_state_status: str, *, merged: bool = False) -> dict:
    return {"number": 7, "merged": merged, "merge_state_status": merge_state_status}


class BehindDecisionTests(unittest.TestCase):
    """The pure decision: BEHIND at the merge gate becomes sync_branch."""

    def test_behind_impl_pr_yields_sync_not_merge(self) -> None:
        decision = _decide(_task(risk="low"), checks=_green(), pr_state=_pr("BEHIND"))
        self.assertEqual(decision["action"], "sync_branch")

    def test_behind_high_risk_impl_pr_with_policy_gate_yields_sync(self) -> None:
        # The third merge site: high/critical risk merges through the green policy
        # (Guardian) gate. A BEHIND branch there syncs too, before merging.
        decision = _decide(_task(risk="high"), checks=_green(), pr_state=_pr("BEHIND"))
        self.assertEqual(decision["action"], "sync_branch")

    def test_behind_closeout_pr_yields_sync(self) -> None:
        task = _task(closeout_pr=9)
        decision = _decide(task, checks=_green(), pr_state=_pr("BEHIND"))
        self.assertEqual(decision["action"], "sync_branch")

    def test_clean_pr_still_merges(self) -> None:
        decision = _decide(_task(risk="low"), checks=_green(), pr_state=_pr("CLEAN"))
        self.assertEqual(decision["action"], "merge")

    def test_clean_closeout_pr_still_merges(self) -> None:
        task = _task(closeout_pr=9)
        decision = _decide(task, checks=_green(), pr_state=_pr("CLEAN"))
        self.assertEqual(decision["action"], "merge")

    def test_pr_without_merge_state_still_merges(self) -> None:
        # A pr_state that predates this change (no merge_state_status key) must keep
        # merging — a missing status is never treated as BEHIND.
        decision = _decide(_task(risk="low"), checks=_green(), pr_state={"merged": False})
        self.assertEqual(decision["action"], "merge")

    def test_blocked_pr_is_not_synced(self) -> None:
        decision = _decide(_task(risk="low"), checks=_green(), pr_state=_pr("BLOCKED"))
        self.assertNotEqual(decision["action"], "sync_branch")
        self.assertEqual(decision["action"], "merge")

    def test_dirty_pr_is_not_synced(self) -> None:
        # DIRTY is a real conflict, not mechanical staleness: it must fall through to
        # the merge effector (whose existing failure message covers real conflicts).
        decision = _decide(_task(risk="low"), checks=_green(), pr_state=_pr("DIRTY"))
        self.assertNotEqual(decision["action"], "sync_branch")
        self.assertEqual(decision["action"], "merge")

    def test_behind_but_pending_checks_is_not_synced(self) -> None:
        # Not otherwise ready: a pending check must wait, never sync.
        checks = _green()
        checks["CCA verdict"] = "pending"
        decision = _decide(_task(risk="low"), checks=checks, pr_state=_pr("BEHIND"))
        self.assertEqual(decision["action"], "wait_checks")

    def test_behind_but_failing_check_is_not_synced(self) -> None:
        # Not otherwise ready: a failing check repairs, never syncs.
        checks = _green()
        checks["Validate Shiki mirror"] = "fail"
        decision = _decide(_task(risk="low"), checks=checks, pr_state=_pr("BEHIND"))
        self.assertNotEqual(decision["action"], "sync_branch")

    def test_behind_but_lone_policy_gate_failing_is_not_synced(self) -> None:
        # The Guardian gate red on its own stops for an authority, never syncs.
        checks = _green()
        checks["MergeGate policy check"] = "fail"
        decision = _decide(_task(risk="high"), checks=checks, pr_state=_pr("BEHIND"))
        self.assertEqual(decision["action"], "stop_guardian")


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True
    )


class _SyncEnv:
    """A real git repo with a bare origin and a task branch that is BEHIND main.

    ``main`` gains a commit after the task branch forks. With ``conflict=False`` the
    two commits touch different files (a clean base merge); with ``conflict=True``
    they edit the same file (the base merge conflicts).
    """

    BRANCH = "shiki/behind-task"

    def __init__(self, tmp: Path, *, conflict: bool = False):
        self.remote = tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.work = tmp / "work"
        self.work.mkdir()
        _git(self.work, "init", "-b", "main")
        _git(self.work, "config", "user.email", "t@t")
        _git(self.work, "config", "user.name", "t")
        (self.work / "shared.txt").write_text("base\n")
        (self.work / "README.md").write_text("readme\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "base")
        _git(self.work, "remote", "add", "origin", str(self.remote))
        _git(self.work, "push", "-u", "origin", "main")

        # Task branch forks from the base and adds its own work.
        _git(self.work, "checkout", "-b", self.BRANCH)
        if conflict:
            (self.work / "shared.txt").write_text("branch side\n")
        else:
            (self.work / "feature.txt").write_text("feature\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "task work")
        _git(self.work, "push", "-u", "origin", self.BRANCH)
        self.branch_tip_before = self.origin_branch_tip()

        # main advances past the fork point, so the branch is now BEHIND.
        _git(self.work, "checkout", "main")
        if conflict:
            (self.work / "shared.txt").write_text("main side\n")
        else:
            (self.work / "other.txt").write_text("other\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-m", "main advances")
        _git(self.work, "push", "origin", "main")

        # Refresh the tracking refs the effector's checkout/merge read.
        _git(self.work, "fetch", "origin")

    def origin_branch_tip(self) -> str:
        return _git(self.remote, "rev-parse", self.BRANCH).stdout.strip()

    def origin_branch_parents(self) -> list[str]:
        out = _git(self.remote, "show", "--no-patch", "--format=%P", self.BRANCH).stdout.strip()
        return out.split()

    def task(self) -> dict:
        return {"id": "T-0001", "goal_id": "G-0001", "expected_branch": self.BRANCH}


class SyncEffectorTests(unittest.TestCase):
    """The real effector against a real temporary git repository."""

    def test_successful_sync_advances_head_and_next_decision_waits(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = _SyncEnv(Path(d))
            result = shiki_loop._sync_branch(env.work, env.task(), base="main")
            self.assertEqual(result["action"], "sync_branch")
            # The pushed head advanced to a real merge commit (two parents).
            self.assertNotEqual(env.origin_branch_tip(), env.branch_tip_before)
            self.assertEqual(len(env.origin_branch_parents()), 2)
            # The next decision must wait for the re-run checks against the new head,
            # never merge the previous head.
            checks = _green()
            checks["CCA verdict"] = "pending"
            decision = _decide(_task(risk="low"), checks=checks, pr_state=_pr("BLOCKED"))
            self.assertEqual(decision["action"], "wait_checks")

    def test_conflict_aborts_and_leaves_branch_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = _SyncEnv(Path(d), conflict=True)
            result = shiki_loop._sync_branch(env.work, env.task(), base="main")
            self.assertEqual(result["action"], "stop_blocked")
            # A distinct conflict reason that names the branch (not the generic merge
            # failure), so an operator can tell a real conflict from staleness.
            self.assertIn(env.BRANCH, result["reason"])
            self.assertIn("conflict", result["reason"].lower())
            # The branch head is byte-identical: nothing was pushed.
            self.assertEqual(env.origin_branch_tip(), env.branch_tip_before)

    def test_push_failure_is_a_distinct_stop_reason(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = _SyncEnv(Path(d))
            # The base merge succeeds locally, but the push has nowhere to go.
            _git(env.work, "remote", "set-url", "--push", "origin", str(Path(d) / "nope.git"))
            result = shiki_loop._sync_branch(env.work, env.task(), base="main")
            self.assertEqual(result["action"], "stop_blocked")
            self.assertIn("push", result["reason"].lower())
            self.assertIn(env.BRANCH, result["reason"])
            # The push failed, so the remote branch is unchanged.
            self.assertEqual(env.origin_branch_tip(), env.branch_tip_before)

    def test_missing_expected_branch_stops_without_touching_git(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = _SyncEnv(Path(d))
            task = env.task()
            task["expected_branch"] = ""
            result = shiki_loop._sync_branch(env.work, task, base="main")
            self.assertEqual(result["action"], "stop_blocked")
            self.assertEqual(env.origin_branch_tip(), env.branch_tip_before)


class NoHistoryRewriteTests(unittest.TestCase):
    """The effector must never rebase or force-push (governance-file safety)."""

    def test_effector_never_rebases_or_force_pushes(self) -> None:
        source = inspect.getsource(shiki_loop._sync_branch)
        self.assertNotIn("rebase", source, "sync effector must never rebase")
        for line in source.splitlines():
            if "push" in line:
                self.assertNotIn("--force", line, "sync push must never be a force-push")
                self.assertNotIn("--force-with-lease", line)
                self.assertNotIn('"-f"', line)


class LoopRunWaitsAfterSyncTests(unittest.TestCase):
    """`shiki loop run` must pause after a sync so the new head's checks re-run."""

    def test_loop_run_sleeps_after_a_successful_sync_before_re_snapshotting(self) -> None:
        # A successful sync returns the forward `sync_branch` action; the run loop
        # must treat it like a wait (sleep the interval) instead of immediately
        # re-snapshotting the just-pushed head and racing GitHub's re-queue.
        steps = iter(
            [
                {"action": "sync_branch", "task_id": "T-0001", "reason": "synced"},
                {"action": "goal_complete", "reason": "done"},
            ]
        )
        slept: list[float] = []
        args = argparse.Namespace(target=".", goal_id="G-0001", max_cycles=5, interval=7)
        with mock.patch.object(shiki_loop, "goal_loop_step", side_effect=lambda t, g: next(steps)), \
            mock.patch.object(shiki_loop, "target_path", side_effect=lambda p: Path(p)), \
            mock.patch("shiki_tasks.require_github_first_target", lambda t: None), \
            mock.patch.object(shiki_loop, "print_json", lambda *a, **k: None), \
            mock.patch.object(shiki_loop.time, "sleep", side_effect=slept.append):
            rc = shiki_loop.cmd_loop_run(args)
        self.assertEqual(rc, 0)
        self.assertEqual(slept, [7])  # exactly one wait, after the sync cycle


if __name__ == "__main__":
    unittest.main()
