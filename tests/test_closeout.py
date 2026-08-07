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
      in-flight goal's files to main's stale version. The sync must be scoped to
      the completing goal's own paths and leave that goal's task files
      BYTE-IDENTICAL to main (the source of truth for a completed goal) so a later
      ``git merge origin/main`` fast-forwards with no manual step. The loop's own
      coordinator-only merge/mark_done ledger FILES stay on disk as durable
      evidence; the completed task mirrors main's reference set rather than carrying
      coordinator-only extras that would diverge it from main and block the merge.

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
    """(b) goal_complete syncs only the completing goal from origin/main, leaving its
    task files byte-identical to main (so a later `git merge origin/main` resolves
    cleanly) and COMMITTING its .shiki surface at the loop's single covering point
    (nothing outside .shiki; the disposable coordinator branch may advance)."""

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

    def test_goal_complete_syncs_completing_task_to_main_and_commits_only_shiki(self):
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            self._build(env)
            head_before = _rev_parse(env.target, "HEAD")

            result = execute_action(env.target, GOAL_A, {"action": "goal_complete"}, repair_limit=3)
            self.assertEqual(result.get("goal_status"), "complete")

            # The completing task file is BYTE-IDENTICAL to main (the source of truth):
            # it carries main's reference set ([L_BASE, L_PULL]) and NOT the loop's own
            # coordinator-only merge/mark_done ids — re-appending those would diverge it
            # from main and make a later `git merge origin/main` abort. main is
            # authoritative for a completed goal, so the coordinator mirrors it.
            evidence = load_task(env.target, T1).get("ledger_evidence", [])
            self.assertEqual(evidence, [L_BASE, L_PULL], evidence)
            self.assertNotIn(L_MERGE, evidence)
            self.assertNotIn(L_MARK, evidence)
            # The loop's own merge/mark_done ledger FILES stay on disk as evidence
            # (each names its task + goal); only the task's reference set follows main.
            self.assertTrue((env.target / f".shiki/ledger/{L_MERGE}.json").is_file())
            self.assertTrue((env.target / f".shiki/ledger/{L_MARK}.json").is_file())
            # main's authoritative completion synced into the coordinator working tree.
            self.assertEqual(load_goal(env.target, GOAL_A).get("status"), "complete")
            self.assertTrue((env.target / f".shiki/reports/{REPORT}.json").is_file(),
                            "the scorecard report from main must ride along")
            # The single covering point COMMITS the coordinator's .shiki surface (so a
            # returning file never aborts a NON-fast-forward `git merge origin/main`):
            # the completing task IS committed, NOTHING outside .shiki is, no mirror
            # record is deleted, and HEAD advances (the disposable coordinator branch
            # may advance — the removed non-goal).
            self.assertNotEqual(_rev_parse(env.target, "HEAD"), head_before,
                                "goal_complete must COMMIT the coordinator mirror (HEAD advances)")
            committed = [line.split("\t") for line in
                         _git(env.target, "diff", "--name-status", head_before, "HEAD").stdout.splitlines()
                         if line.strip()]
            paths = [row[-1] for row in committed]
            self.assertIn(f".shiki/tasks/{T1}.json", paths,
                          "the completing task must be committed so the merge absorbs it")
            self.assertEqual([p for p in paths if not p.startswith(".shiki/")], [], paths)
            self.assertEqual([row[-1] for row in committed if row[0].startswith("D")], [],
                             "the append-only mirror commit must delete no record")


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


class CloseoutCoordinatorMergeTests(unittest.TestCase):
    """The measured coordinator collision, reproduced end to end — and its fix.

    After a task runs through its closeout — ``create_closeout_pr`` opens the
    closeout PR, it merges to main, then ``mark_done`` + ``goal_complete`` write /
    sync the terminal mirror — the terminal records (goal, task, lock, closeout
    ledgers, the scorecard report) return through ``git merge origin/main``.

    The coordinator carries its OWN commits (the covering point commits every
    effector), so that merge is ALWAYS non-fast-forward. Merely staging the returning
    records made the non-fast-forward merge abort with "Your local changes to the
    following files would be overwritten by merge"; committing them absorbs the merge
    cleanly. Each merge below is asserted DIVERGENT (non-fast-forward) BEFORE it runs
    — the previous tests passed while the criterion was false because their
    repositories were fast-forward-only. Driven against a REAL repo + bare origin,
    twice in a row, plus the exceptional genuinely-differing-file case."""

    def _merge_origin_main(self, env: CloseoutEnv):
        # `git fetch origin` hits the fake GitHub URL and no-ops; the authoritative
        # origin/main tracking ref is set directly by the closeout simulation below.
        _git(env.target, "fetch", "origin", check=False)
        return _git(env.target, "merge", "origin/main", check=False)

    def _assert_divergent(self, env: CloseoutEnv) -> None:
        # NON-fast-forward: neither HEAD nor origin/main is an ancestor of the other,
        # so `git merge origin/main` is a real 3-way merge, not a fast-forward. This
        # is what the coordinator's own commits guarantee — and what the old
        # fast-forward-only tests never exercised.
        self.assertNotEqual(
            _git(env.target, "merge-base", "--is-ancestor", "origin/main", "HEAD", check=False).returncode, 0,
            "origin/main must NOT be an ancestor of HEAD (else the merge is a no-op/fast-forward)")
        self.assertNotEqual(
            _git(env.target, "merge-base", "--is-ancestor", "HEAD", "origin/main", check=False).returncode, 0,
            "HEAD must NOT be an ancestor of origin/main (else the merge fast-forwards)")

    def _assert_real_merge_commit(self, env: CloseoutEnv) -> None:
        # A non-fast-forward merge records a commit with TWO parents; a fast-forward
        # would leave HEAD at a single-parent commit.
        parents = _git(env.target, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
        self.assertEqual(len(parents), 3, "the merge must be a real (two-parent) merge commit")

    def _seed_single_task_goal_on_main(self, env: CloseoutEnv, goal_id: str, task_id: str) -> None:
        branch = f"shiki/{task_id.lower()}-slice"
        env.write(f".shiki/goals/{goal_id}.json", _goal(goal_id))
        env.write(f".shiki/dag/{goal_id}.json", {"goal_id": goal_id, "nodes": [task_id], "edges": []})
        env.write(f".shiki/tasks/{task_id}.json", _task(task_id, goal_id, expected_branch=branch))
        env.write(f".shiki/locks/{task_id}.json", _lock(task_id, goal_id))
        env.commit_push_main(f"seed {goal_id} for closeout")

    def _run_task_through_closeout(self, env: CloseoutEnv, goal_id: str, task_id: str) -> None:
        """Drive the REAL closeout effectors and simulate the closeout PR merging,
        leaving the coordinator carrying the terminal mirror for `git merge`.

        Asserts the covering point COMMITS: HEAD advances across the three effectors
        and nothing outside ``.shiki`` is committed (the disposable coordinator branch
        may advance — the previous contract's unmeasured "must not advance" non-goal
        is removed; advancing it is exactly what makes the returning merge succeed)."""
        head_before = _rev_parse(env.target, "HEAD")
        orig = shiki_loop._gh
        shiki_loop._gh = _FakeGh()
        try:
            r1 = execute_action(env.target, goal_id,
                                {"action": "create_closeout_pr", "task_id": task_id}, repair_limit=3)
            self.assertEqual(r1.get("action"), "create_closeout_pr", r1)
            self.assertIs(r1.get("completes_goal"), True, r1)
            # The closeout PR merges: main fast-forwards to the closeout branch tip.
            closeout_branch = f"shiki/{task_id.lower()}-closeout"
            tip = _git(env.remote, "rev-parse", f"refs/heads/{closeout_branch}").stdout.strip()
            _git(env.remote, "update-ref", "refs/heads/main", tip)
            _git(env.target, "update-ref", "refs/remotes/origin/main", tip)
            # The loop then records done + goal completion in the coordinator mirror.
            r2 = execute_action(env.target, goal_id, {"action": "mark_done", "task_id": task_id}, repair_limit=3)
            self.assertEqual(r2.get("status"), "done", r2)
            r3 = execute_action(env.target, goal_id, {"action": "goal_complete", "task_id": task_id}, repair_limit=3)
            self.assertEqual(r3.get("goal_status"), "complete", r3)
        finally:
            shiki_loop._gh = orig
        self.assertNotEqual(_rev_parse(env.target, "HEAD"), head_before,
                            "the closeout effectors must COMMIT the coordinator mirror (HEAD advances)")
        committed = [line.split("\t") for line in
                     _git(env.target, "diff", "--name-status", head_before, "HEAD").stdout.splitlines()
                     if line.strip()]
        self.assertEqual([row[-1] for row in committed if not row[-1].startswith(".shiki/")], [], committed)
        self.assertEqual([row[-1] for row in committed if row[0].startswith("D")], [],
                         "the append-only mirror commit must delete no record")

    def test_merge_origin_main_succeeds_after_closeout_and_again_next_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            # cycle 1: complete a single-task goal through the real closeout effectors.
            self._seed_single_task_goal_on_main(env, GOAL_A, T1)
            env.use_github_fetch_url()
            self._run_task_through_closeout(env, GOAL_A, T1)
            self._assert_divergent(env)  # the merge is genuinely NON-fast-forward
            first = self._merge_origin_main(env)
            self.assertEqual(first.returncode, 0,
                             f"first `git merge origin/main` aborted:\n{first.stdout}{first.stderr}")
            self.assertNotIn("would be overwritten by merge", first.stderr)
            self._assert_real_merge_commit(env)

            # cycle 2: a genuinely different goal + task must still leave the
            # coordinator mergeable with no manual step.
            self._seed_single_task_goal_on_main(env, GOAL_B, TB)
            self._run_task_through_closeout(env, GOAL_B, TB)
            self._assert_divergent(env)
            second = self._merge_origin_main(env)
            self.assertEqual(second.returncode, 0,
                             f"second `git merge origin/main` aborted:\n{second.stdout}{second.stderr}")
            self.assertNotIn("would be overwritten by merge", second.stderr)
            self._assert_real_merge_commit(env)

    def test_genuinely_differing_file_conflicts_normally_not_a_refusal(self):
        # The exception the fix documents rather than suppresses: when the
        # coordinator's copy and the incoming version genuinely DIFFER, the merge
        # produces a normal `CONFLICT (add/add)` — not the hard "would be overwritten
        # by merge" refusal — and main stays the source of truth for the operator to
        # resolve. No merge driver / strategy option / automatic resolution is added.
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            self._seed_single_task_goal_on_main(env, GOAL_A, T1)
            env.use_github_fetch_url()
            # origin/main gains a report the coordinator will ALSO write, but with
            # DIFFERENT bytes — the rare real collision (measured ~1 of 28).
            collide = ".shiki/reports/R-collide.json"

            def mutate(wt: Path) -> None:
                write_json(wt / collide, {"id": "R-collide", "side": "main"})
            env.advance_origin_main(mutate)
            # The coordinator writes its OWN differing copy (unstaged); an effector's
            # covering point commits it.
            env.write(collide, {"id": "R-collide", "side": "coordinator"})
            orig = shiki_loop._gh
            shiki_loop._gh = _FakeGh()
            try:
                execute_action(env.target, GOAL_A, {"action": "mark_done", "task_id": T1}, repair_limit=3)
            finally:
                shiki_loop._gh = orig

            self._assert_divergent(env)
            merge = self._merge_origin_main(env)
            self.assertNotEqual(merge.returncode, 0, "a genuine disagreement must not merge silently")
            combined = merge.stdout + merge.stderr
            self.assertIn("CONFLICT", combined, combined)
            self.assertNotIn("would be overwritten by merge", combined,
                             "a genuine disagreement must be a normal conflict, NOT a hard refusal")


class ReopenedTaskDoesNotCloseoutTests(unittest.TestCase):
    """A Spec Amendment re-opens a task (status -> ready) while its expected_pr —
    frozen to base — still names the previous cycle's merged closeout PR, and its
    closeout_pr is unset (the closeout branch pushes only expected_pr, see
    ``_create_closeout_pr``). The loop must clear the stale pointer as it dispatches,
    so the fresh implementation's create_pr opens a NEW PR instead of the loop reading
    the merged pointer as this cycle's completed work and driving a source-free
    closeout. Measured 2026-08-06 (PR #308 / T-...dbdae1cf)."""

    def _seed_reopened_ready_task(self, env: CloseoutEnv) -> None:
        branch = f"shiki/{T1.lower()}-slice"
        env.write(f".shiki/goals/{GOAL_A}.json", _goal(GOAL_A))
        env.write(f".shiki/dag/{GOAL_A}.json", {"goal_id": GOAL_A, "nodes": [T1], "edges": []})
        # Re-opened: ready, expected_pr names the merged closeout PR #309, closeout_pr
        # unset, cca_rerun_count reset — exactly what an amendment leaves behind.
        env.write(f".shiki/tasks/{T1}.json",
                  _task(T1, GOAL_A, status="ready", expected_branch=branch, expected_pr=309))
        env.write(f".shiki/locks/{T1}.json", _lock(T1, GOAL_A))
        env.commit_push_main("re-opened task: ready, stale merged expected_pr=309")
        env.use_github_fetch_url()

    def test_first_cycle_decides_dispatch_not_closeout(self):
        # cycle 1: the pure engine sees a merged pointer on a ready task -> dispatch
        # + clear_expected_pr, and NEVER create_closeout_pr.
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            self._seed_reopened_ready_task(env)
            decision = decide_task_action(
                load_task(env.target, T1), checks=None,
                pr_state={"number": 309, "merged": True},
                repair_attempts=0, repair_limit=3, required_checks=REQUIRED, cca_reruns=0,
            )
            self.assertEqual(decision["action"], "dispatch", decision)
            self.assertNotEqual(decision["action"], "create_closeout_pr")
            self.assertTrue(decision.get("clear_expected_pr"), decision)

    def test_dispatch_clears_pointer_and_next_cycle_cuts_fresh_pr(self):
        with tempfile.TemporaryDirectory() as d:
            env = CloseoutEnv(Path(d))
            self._seed_reopened_ready_task(env)
            decision = decide_task_action(
                load_task(env.target, T1), checks=None,
                pr_state={"number": 309, "merged": True},
                repair_attempts=0, repair_limit=3, required_checks=REQUIRED, cca_reruns=0,
            )

            # The dispatch effector clears the stale pointer BEFORE running the
            # implementer (stubbed so no real session runs).
            orig = shiki_loop._dispatch
            shiki_loop._dispatch = lambda *a, **k: 0
            try:
                result = execute_action(env.target, GOAL_A, decision, repair_limit=3)
            finally:
                shiki_loop._dispatch = orig
            self.assertEqual(result["action"], "dispatch", result)
            self.assertIsNone(load_task(env.target, T1).get("expected_pr"),
                              "the stale merged pointer must be cleared before the fresh dispatch")

            # cycle 2 (measured sequence): the dispatched session leaves the task in
            # `review`; with the pointer gone there is no PR -> create_pr (a fresh
            # implementation PR), NOT create_closeout_pr.
            reopened = load_task(env.target, T1)
            reopened["status"] = "review"
            fresh = decide_task_action(
                reopened, checks={}, pr_state=None,
                repair_attempts=0, repair_limit=3, required_checks=REQUIRED, cca_reruns=0,
            )
            self.assertEqual(fresh["action"], "create_pr", fresh)
            self.assertNotEqual(fresh["action"], "create_closeout_pr")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
