"""Fail-closed closeout: the loop never completes a multi-task goal on main after
only its first task merges, its goal_complete sync touches only the completing
goal, and a closeout PR gets a fresh CCA rerun budget.

Three defects this suite pins (ADR 0012):

  (a) ``_create_closeout_pr`` computed ``completes_goal`` from the closeout
      worktree alone. The worktree is cut from ``origin/main``; for a multi-task
      goal whose first task merged but whose siblings' files are not yet on main,
      only that one task rides in, so ``all(status == done for tasks_for_goal(
      worktree))`` was vacuously true and the goal was completed on main after a
      single task. It must instead require every task id the COORDINATOR knows for
      the goal to be present in the worktree AND done there.

  (b) ``execute_action``'s ``goal_complete`` synced with a whole-tree
      ``git checkout origin/main -- .shiki``. That reverts every unrelated
      in-flight goal's files to main's stale version and drops the loop's own
      merge/mark_done ledger ids (coordinator-only, never on main) off the
      completing task. The sync must be scoped to the completing goal's own paths,
      re-append the reverted merge/mark_done ledger ids, and leave nothing staged.

  (c) The closeout repoint did not reset ``cca_rerun_count``, so a task that
      exhausted its 2 CCA reruns on the impl PR fails closed to ``stop_blocked`` on
      the closeout PR's first CCA same-head race instead of rerunning. The fresh
      closeout PR head must get a fresh rerun budget.

These tests import the real ``scripts/`` modules and drive the real effectors over
a real git repo + bare origin, stubbing only ``shiki_loop._gh``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_loop
from shiki_loop import decide_task_action, execute_action
from shiki_process import ensure_control_dirs, read_json, shiki_path, write_json
from shiki_tasks import load_goal, load_task

REQUIRED = [
    "Validate Shiki mirror",
    "CCA verdict",
    "MergeGate metadata check",
    "MergeGate policy check",
]


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True
    )


def _rev_parse(repo, ref: str) -> str:
    out = _git(repo, "rev-parse", ref, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


class _FakeGh:
    """Stand in for ``shiki_loop._gh`` for the closeout effector: ``pr list`` finds
    no prior closeout PR, ``pr create`` returns a fresh PR URL. Everything else
    (worktree add, commit, push) runs for real against the bare origin."""

    def __init__(self, first: int = 100):
        self.next = first
        self.calls: list[list[str]] = []

    def __call__(self, target, args, check=True):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["pr", "list"]:
            return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")
        if args[:2] == ["pr", "create"]:
            self.next += 1
            return types.SimpleNamespace(
                returncode=0, stdout=f"https://github.com/o/r/pull/{self.next}\n", stderr=""
            )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class CloseoutEnv:
    """A real git target with a bare origin. ``origin/main`` is authoritative for
    what "already landed on main"; the coordinator working tree can then diverge
    (extra tasks, in-flight sibling goals, loop-authored ledgers) to reproduce the
    exact state each closeout defect needs."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.remote = tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.target = tmp / "repo"
        self.target.mkdir()
        _git(self.target, "init", "-b", "main")
        _git(self.target, "config", "user.email", "t@t")
        _git(self.target, "config", "user.name", "t")
        (self.target / "README.md").write_text("x\n")
        _git(self.target, "add", "-A")
        _git(self.target, "commit", "-m", "init")
        # origin starts fully bare (fetch+push) so the initial pushes establish
        # refs/remotes/origin/main; the GitHub fetch URL is swapped in afterwards.
        _git(self.target, "remote", "add", "origin", str(self.remote))
        _git(self.target, "push", "-u", "origin", "main")
        ensure_control_dirs(self.target)
        self._n = 0

    def write(self, relpath: str, obj: dict) -> None:
        write_json(self.target / relpath, obj)

    def commit_push_main(self, msg: str) -> None:
        _git(self.target, "add", "-A")
        _git(self.target, "commit", "-m", msg)
        _git(self.target, "push", "origin", "main")
        _git(self.target, "update-ref", "refs/remotes/origin/main", "HEAD")

    def use_github_fetch_url(self) -> None:
        # After main is established, mirror the production runtime: a GitHub fetch
        # URL (so require_github_first_target passes and `git fetch origin main` is
        # a harmless no-op) and the bare repo as the push URL (so real pushes work).
        _git(self.target, "remote", "set-url", "origin", "https://github.com/o/r.git")
        _git(self.target, "remote", "set-url", "--push", "origin", str(self.remote))

    def advance_origin_main(self, mutate) -> str:
        """Build a commit on top of origin/main in a scratch worktree (objects land
        in the target's shared store), point refs/remotes/origin/main at it, and
        return its sha — mirroring what the merged closeout PR does to main without
        disturbing the coordinator's HEAD/working tree."""
        self._n += 1
        wt = self.tmp / f"mainwt-{self._n}"
        _git(self.target, "worktree", "add", "--force", "--detach", str(wt), "origin/main")
        mutate(wt)
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "advance origin/main")
        sha = _rev_parse(wt, "HEAD")
        _git(self.target, "update-ref", "refs/remotes/origin/main", sha)
        _git(self.target, "worktree", "remove", "--force", str(wt))
        return sha

    def origin_show(self, branch: str, relpath: str) -> str:
        out = _git(self.remote, "show", f"refs/heads/{branch}:{relpath}", check=False)
        return out.stdout if out.returncode == 0 else ""

    def origin_scorecards(self, branch: str) -> list[str]:
        out = _git(self.remote, "ls-tree", "-r", "--name-only", f"refs/heads/{branch}",
                   "--", ".shiki/reports", check=False)
        # A committed .gitkeep keeps the dir non-empty; only a real R-*.json report
        # is a scorecard, so filter to those.
        return [line for line in (out.stdout or "").splitlines()
                if line.rsplit("/", 1)[-1].startswith("R-")]


def _task(task_id, goal_id, **over):
    base = {
        "id": task_id,
        "goal_id": goal_id,
        "status": "review",
        "title": "t",
        "scope": "s",
        "risk_level": "low",
        "assigned_runtime": "claude-code",
        "expected_branch": f"shiki/{task_id.lower()}-slice",
        "expected_pr": 11,
        "locks": ["path:.shiki/**"],
        "required_skills": ["tdd"],
        "acceptance_checks": ["done"],
        "ledger_evidence": [],
    }
    base.update(over)
    return base


def _goal(goal_id, **over):
    base = {"id": goal_id, "status": "in-progress", "title": "g", "outcome": "o",
            "risk_level": "low", "ledger_evidence": []}
    base.update(over)
    return base


def _lock(task_id, goal_id, state="active"):
    return {"task_id": task_id, "goal_id": goal_id, "state": state,
            "owner": "shiki-run", "locks": ["path:.shiki/**"]}


GOAL_A = "G-20260729T065622761503Z-4479b0d3"
GOAL_B = "G-20260729T065622761503Z-000000b0"
T1 = "T-20260729T065622764822Z-aaaa0001"
T2 = "T-20260729T065622764822Z-bbbb0002"
TB = "T-20260729T065622764822Z-cccc00b1"


class CompletesGoalTests(unittest.TestCase):
    """(a) completes_goal is judged against the whole coordinator goal, not just
    what has ridden onto main so far."""

    def test_multitask_first_task_only_on_main_does_not_complete(self):
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            branch1 = f"shiki/{T1.lower()}-slice"
            # main carries ONLY the first task file (its impl PR merged); the DAG
            # names both nodes but task 2's file is not on main yet.
            env.write(f".shiki/goals/{GOAL_A}.json", _goal(GOAL_A))
            env.write(f".shiki/dag/{GOAL_A}.json", {"goal_id": GOAL_A, "nodes": [T1, T2], "edges": []})
            env.write(f".shiki/tasks/{T1}.json", _task(T1, GOAL_A, expected_branch=branch1))
            env.write(f".shiki/locks/{T1}.json", _lock(T1, GOAL_A))
            env.commit_push_main("goal A + task 1 on main")
            # The coordinator ALSO knows task 2 (in flight, file not on main yet).
            env.write(f".shiki/tasks/{T2}.json", _task(T2, GOAL_A, status="running",
                                                       expected_branch=f"shiki/{T2.lower()}-slice"))
            env.use_github_fetch_url()

            fake = _FakeGh()
            orig = shiki_loop._gh
            shiki_loop._gh = fake
            try:
                result = shiki_loop._create_closeout_pr(env.target, GOAL_A, T1)
            finally:
                shiki_loop._gh = orig

            self.assertEqual(result.get("action"), "create_closeout_pr", result)
            self.assertIs(result.get("completes_goal"), False, result)
            # The pushed closeout head carries NO goal-complete status and NO scorecard.
            closeout_branch = f"shiki/{T1.lower()}-closeout"
            goal_on_head = json.loads(env.origin_show(closeout_branch, f".shiki/goals/{GOAL_A}.json"))
            self.assertNotEqual(goal_on_head.get("status"), "complete", goal_on_head)
            self.assertEqual(env.origin_scorecards(closeout_branch), [])

    def test_control_both_tasks_on_main_both_done_completes(self):
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            branch2 = f"shiki/{T2.lower()}-slice"
            # main carries both task files + the DAG; task 1 already done, task 2 is
            # the completing task (in review) whose closeout marks it done.
            env.write(f".shiki/goals/{GOAL_A}.json", _goal(GOAL_A))
            env.write(f".shiki/dag/{GOAL_A}.json", {"goal_id": GOAL_A, "nodes": [T1, T2], "edges": []})
            env.write(f".shiki/tasks/{T1}.json", _task(T1, GOAL_A, status="done",
                                                       expected_branch=f"shiki/{T1.lower()}-slice"))
            env.write(f".shiki/tasks/{T2}.json", _task(T2, GOAL_A, expected_branch=branch2))
            env.write(f".shiki/locks/{T1}.json", _lock(T1, GOAL_A, state="released"))
            env.write(f".shiki/locks/{T2}.json", _lock(T2, GOAL_A))
            env.commit_push_main("goal A: task 1 done, task 2 in review")
            env.use_github_fetch_url()

            fake = _FakeGh()
            orig = shiki_loop._gh
            shiki_loop._gh = fake
            try:
                result = shiki_loop._create_closeout_pr(env.target, GOAL_A, T2)
            finally:
                shiki_loop._gh = orig

            self.assertEqual(result.get("action"), "create_closeout_pr", result)
            self.assertIs(result.get("completes_goal"), True, result)
            closeout_branch = f"shiki/{T2.lower()}-closeout"
            goal_on_head = json.loads(env.origin_show(closeout_branch, f".shiki/goals/{GOAL_A}.json"))
            self.assertEqual(goal_on_head.get("status"), "complete", goal_on_head)
            self.assertTrue(env.origin_scorecards(closeout_branch),
                            "the completing closeout must push a scorecard report")

    def test_single_task_goal_completes(self):
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            branch1 = f"shiki/{T1.lower()}-slice"
            env.write(f".shiki/goals/{GOAL_A}.json", _goal(GOAL_A))
            env.write(f".shiki/dag/{GOAL_A}.json", {"goal_id": GOAL_A, "nodes": [T1], "edges": []})
            env.write(f".shiki/tasks/{T1}.json", _task(T1, GOAL_A, expected_branch=branch1))
            env.write(f".shiki/locks/{T1}.json", _lock(T1, GOAL_A))
            env.commit_push_main("single-task goal A on main")
            env.use_github_fetch_url()

            fake = _FakeGh()
            orig = shiki_loop._gh
            shiki_loop._gh = fake
            try:
                result = shiki_loop._create_closeout_pr(env.target, GOAL_A, T1)
            finally:
                shiki_loop._gh = orig

            self.assertEqual(result.get("action"), "create_closeout_pr", result)
            self.assertIs(result.get("completes_goal"), True, result)


# The loop-authored ledgers that live only in the coordinator mirror.
L_BASE = "L-20260729T065622764822Z-base00a1"
L_PULL = "L-20260729T065622764822Z-pull00a1"
L_COMP = "L-20260729T065622764822Z-comp00a1"
L_MERGE = "L-20260729T065622764822Z-merge0a1"
L_MARK = "L-20260729T065622764822Z-mark00a1"
REPORT = "R-20260729T065622764822Z-score0a1"


class GoalCompleteMirrorSyncTests(unittest.TestCase):
    """(b) goal_complete syncs only the completing goal from origin/main, preserving
    the loop's own merge/mark_done ledger ids and leaving nothing staged."""

    def _build(self, env: CloseoutEnv) -> None:
        # Base main (== coordinator HEAD): goal A in-flight, its task in review with
        # one shared ledger; an unrelated goal B whose task is `ready` on main.
        env.write(f".shiki/goals/{GOAL_A}.json", _goal(GOAL_A))
        env.write(f".shiki/dag/{GOAL_A}.json", {"goal_id": GOAL_A, "nodes": [T1], "edges": []})
        env.write(f".shiki/tasks/{T1}.json", _task(T1, GOAL_A, ledger_evidence=[L_BASE]))
        env.write(f".shiki/locks/{T1}.json", _lock(T1, GOAL_A))
        env.write(f".shiki/ledger/{L_BASE}.json",
                  {"id": L_BASE, "goal_id": GOAL_A, "task_id": T1, "type": "check",
                   "summary": "base", "evidence": []})
        env.write(f".shiki/goals/{GOAL_B}.json", _goal(GOAL_B))
        env.write(f".shiki/dag/{GOAL_B}.json", {"goal_id": GOAL_B, "nodes": [TB], "edges": []})
        env.write(f".shiki/tasks/{TB}.json", _task(TB, GOAL_B, status="ready",
                                                   expected_branch=f"shiki/{TB.lower()}-slice"))
        env.write(f".shiki/locks/{TB}.json", _lock(TB, GOAL_B))
        env.commit_push_main("base: goal A in-flight, goal B ready")

        # origin/main advances to "goal A's closeout merged": goal complete + a
        # scorecard report + completion & /pull ledgers; task done; lock released.
        def mutate(wt: Path):
            write_json(wt / f".shiki/goals/{GOAL_A}.json",
                       _goal(GOAL_A, status="complete", ledger_evidence=[L_COMP]))
            write_json(wt / f".shiki/tasks/{T1}.json",
                       _task(T1, GOAL_A, status="done", ledger_evidence=[L_BASE, L_PULL]))
            write_json(wt / f".shiki/locks/{T1}.json", _lock(T1, GOAL_A, state="released"))
            write_json(wt / f".shiki/reports/{REPORT}.json",
                       {"id": REPORT, "goal_id": GOAL_A, "status": "complete",
                        "scorecard": {"goal_id": GOAL_A}})
            write_json(wt / f".shiki/ledger/{L_COMP}.json",
                       {"id": L_COMP, "goal_id": GOAL_A, "type": "completion",
                        "summary": "goal A complete", "evidence": [f".shiki/reports/{REPORT}.json"]})
            write_json(wt / f".shiki/ledger/{L_PULL}.json",
                       {"id": L_PULL, "goal_id": GOAL_A, "task_id": T1, "type": "lock",
                        "summary": "closeout /pull", "evidence": [f".shiki/tasks/{T1}.json"]})
        env.advance_origin_main(mutate)

        # The coordinator's post-merge local state: task done, carrying the loop's
        # own merge + mark_done ledgers (coordinator-only, absent from main); goal B
        # advanced to `running` locally (in flight). goal A stays active locally
        # (goal_complete has not synced it yet).
        env.write(f".shiki/tasks/{T1}.json",
                  _task(T1, GOAL_A, status="done", ledger_evidence=[L_BASE, L_MERGE, L_MARK]))
        env.write(f".shiki/ledger/{L_MERGE}.json",
                  {"id": L_MERGE, "goal_id": GOAL_A, "task_id": T1, "type": "mergegate",
                   "summary": "Goal loop merged PR #12 for " + T1, "evidence": []})
        env.write(f".shiki/ledger/{L_MARK}.json",
                  {"id": L_MARK, "goal_id": GOAL_A, "task_id": T1, "type": "check",
                   "summary": "Goal loop marked " + T1 + " done: PR already merged", "evidence": []})
        env.write(f".shiki/tasks/{TB}.json", _task(TB, GOAL_B, status="running",
                                                   expected_branch=f"shiki/{TB.lower()}-slice"))
        env.use_github_fetch_url()

    def test_goal_complete_leaves_unrelated_goal_b_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            self._build(env)
            before = (env.target / f".shiki/tasks/{TB}.json").read_text()

            result = execute_action(env.target, GOAL_A, {"action": "goal_complete"}, repair_limit=3)

            self.assertEqual(result["action"], "goal_complete")
            self.assertEqual(result.get("goal_status"), "complete")
            after = (env.target / f".shiki/tasks/{TB}.json").read_text()
            self.assertEqual(before, after, "goal B's in-flight task file must be untouched")
            self.assertEqual(load_task(env.target, TB)["status"], "running")

    def test_goal_complete_reappends_loop_ledgers_and_stages_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            self._build(env)

            result = execute_action(env.target, GOAL_A, {"action": "goal_complete"}, repair_limit=3)
            self.assertEqual(result.get("goal_status"), "complete")

            evidence = load_task(env.target, T1).get("ledger_evidence", [])
            self.assertIn(L_MERGE, evidence, "the loop's own merge ledger id was dropped by the sync")
            self.assertIn(L_MARK, evidence, "the loop's own mark_done ledger id was dropped by the sync")
            # main's authoritative completion synced into the coordinator working tree.
            self.assertEqual(load_goal(env.target, GOAL_A).get("status"), "complete")
            self.assertTrue((env.target / f".shiki/reports/{REPORT}.json").is_file(),
                            "the scorecard report from main must ride along")
            # No reverted path is left staged: the coordinator carries .shiki
            # mutations unstaged, exactly as the loop normally leaves them.
            staged = _git(env.target, "diff", "--cached", "--name-only").stdout.splitlines()
            self.assertEqual([p for p in staged if p.startswith(".shiki/")], [], staged)


class CloseoutCcaRerunResetTests(unittest.TestCase):
    """(c) the closeout repoint resets cca_rerun_count so the fresh closeout PR head
    gets a fresh CCA rerun budget."""

    def _closeout_checks(self):
        checks = {name: "pass" for name in REQUIRED}
        checks["CCA verdict"] = "fail"  # the same-head CCA race against green siblings
        return checks

    def test_repoint_resets_count_and_closeout_cca_race_reruns(self):
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            branch1 = f"shiki/{T1.lower()}-slice"
            env.write(f".shiki/goals/{GOAL_A}.json", _goal(GOAL_A))
            env.write(f".shiki/dag/{GOAL_A}.json", {"goal_id": GOAL_A, "nodes": [T1], "edges": []})
            # The impl PR exhausted its 2 CCA reruns before merging.
            env.write(f".shiki/tasks/{T1}.json",
                      _task(T1, GOAL_A, expected_branch=branch1, cca_rerun_count=2))
            env.write(f".shiki/locks/{T1}.json", _lock(T1, GOAL_A))
            env.commit_push_main("single-task goal A, impl PR used 2 CCA reruns")
            env.use_github_fetch_url()

            fake = _FakeGh()
            orig = shiki_loop._gh
            shiki_loop._gh = fake
            try:
                result = shiki_loop._create_closeout_pr(env.target, GOAL_A, T1)
            finally:
                shiki_loop._gh = orig
            self.assertEqual(result.get("action"), "create_closeout_pr", result)

            task = load_task(env.target, T1)
            self.assertEqual(task.get("cca_rerun_count"), 0,
                             "the closeout repoint must reset the impl PR's rerun tally")
            self.assertEqual(task.get("closeout_pr"), result.get("closeout_pr"))

            # With the reset budget, the closeout PR's CCA same-head race reruns.
            decision = decide_task_action(
                task, checks=self._closeout_checks(), pr_state={"merged": False},
                repair_attempts=0, repair_limit=3, required_checks=REQUIRED,
                cca_reruns=int(task.get("cca_rerun_count") or 0),
            )
            self.assertEqual(decision["action"], "rerun_cca", decision)

            # Guard: had the count NOT been reset (still 2), the same race would fail
            # closed to stop_blocked — so the reset is load-bearing, not cosmetic.
            stalled = decide_task_action(
                task, checks=self._closeout_checks(), pr_state={"merged": False},
                repair_attempts=0, repair_limit=3, required_checks=REQUIRED, cca_reruns=2,
            )
            self.assertEqual(stalled["action"], "stop_blocked", stalled)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
