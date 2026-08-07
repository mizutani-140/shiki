"""Dispatch state sync (T-20260729T065622770911Z-a8db8b69).

A dispatch hands the implementer a worktree, but nothing guarantees that
worktree holds the contract the coordinator currently has, nor the evidence that
contract references. Two gaps, one seam:

 (a) shiki_runtime.sync_contract_into_worktree — before a headless session
     starts, the worktree's task/goal/lock mirror is refreshed from the
     coordinator so the implementer judges against CURRENT terms (e.g. a repair
     dispatched after a Spec Amendment tightened the locks). Only THIS task's /
     goal's id-named files are carried, never a sibling's or a foreign goal's.

 (b) shiki_loop._evidence_relatives_for_task — the transitive ledger -> evidence
     walk expands a ledger ONLY when its goal_id AND task_id both equal the
     task's. That is deliberately STRICTER than
     mergegate_check.ledger_entry_allowed_for_task, which also accepts a
     goal-scoped ledger with an empty task_id; reusing the looser bound would let
     a padded ledger_evidence inherit the .shiki paths a foreign or goal-level
     ledger references.

The suite is dependency free: standard-library ``unittest`` only.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_loop
from shiki_loop import _evidence_relatives_for_task, _sync_state_to_branch, execute_action
from shiki_process import ShikiError
from shiki_runtime import sync_contract_into_worktree

TASK = "T-20260729T065622770911Z-a8db8b69"
GOAL = "G-20260729T065622761503Z-4479b0d3"
OTHER_TASK = "T-20260101T000000000000Z-sibling0"
OTHER_GOAL = "G-20260101T000000000000Z-foreign00"


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class ContractSyncTests(unittest.TestCase):
    """Gap (a): the coordinator's current contract lands in the worktree."""

    def _coordinator(self, tmp: Path, *, locks) -> Path:
        target = tmp / "repo"
        _write(
            target / ".shiki" / "tasks" / f"{TASK}.json",
            {"id": TASK, "goal_id": GOAL, "status": "repair-needed", "locks": locks},
        )
        _write(target / ".shiki" / "goals" / f"{GOAL}.json", {"id": GOAL, "status": "planned"})
        _write(
            target / ".shiki" / "locks" / f"{TASK}.json",
            {"task_id": TASK, "goal_id": GOAL, "state": "active", "locks": locks},
        )
        return target

    def _stale_worktree(self, tmp: Path, *, locks) -> Path:
        wt = tmp / "wt"
        # A worktree cut before the amendment carries the STALE contract.
        _write(
            wt / ".shiki" / "tasks" / f"{TASK}.json",
            {"id": TASK, "goal_id": GOAL, "status": "running", "locks": locks},
        )
        return wt

    def test_amended_contract_lands_in_worktree(self):
        # Acceptance 1: a repair dispatched after a contract amendment sees the
        # amended contract — the task file it reads declares the amended locks.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            amended = ["path:scripts/a.py", "path:scripts/b.py", "path:scripts/c.py"]
            target = self._coordinator(tmp, locks=amended)
            wt = self._stale_worktree(tmp, locks=["path:scripts/a.py"])  # stale 1-lock
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, wt)

            wt_task = _read(wt / ".shiki" / "tasks" / f"{TASK}.json")
            self.assertEqual(wt_task["locks"], amended)
            self.assertIn(f".shiki/tasks/{TASK}.json", synced)

    def test_goal_and_lock_records_are_carried(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            locks = ["path:scripts/x.py"]
            target = self._coordinator(tmp, locks=locks)
            wt = self._stale_worktree(tmp, locks=["path:old.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, wt)

            self.assertIn(f".shiki/goals/{GOAL}.json", synced)
            self.assertIn(f".shiki/locks/{TASK}.json", synced)
            self.assertEqual(
                _read(wt / ".shiki" / "goals" / f"{GOAL}.json"),
                _read(target / ".shiki" / "goals" / f"{GOAL}.json"),
            )
            self.assertEqual(_read(wt / ".shiki" / "locks" / f"{TASK}.json")["locks"], locks)

    def test_foreign_goal_file_is_not_carried(self):
        # Only this task's own goal rides along; an unrelated goal's mirror state
        # must never leak into the worktree.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            target = self._coordinator(tmp, locks=["path:scripts/x.py"])
            _write(target / ".shiki" / "goals" / f"{OTHER_GOAL}.json", {"id": OTHER_GOAL})
            wt = self._stale_worktree(tmp, locks=["path:old.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, wt)

            self.assertNotIn(f".shiki/goals/{OTHER_GOAL}.json", synced)
            self.assertFalse((wt / ".shiki" / "goals" / f"{OTHER_GOAL}.json").exists())

    def test_idempotent_no_duplicate_or_conflicting_records(self):
        # Acceptance 5: re-running a dispatch is idempotent — the id-named mirror
        # files are overwritten in place, never duplicated or left conflicting.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            locks = ["path:scripts/x.py", "path:scripts/y.py"]
            target = self._coordinator(tmp, locks=locks)
            wt = self._stale_worktree(tmp, locks=["path:old.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            first = sync_contract_into_worktree(target, task, wt)
            second = sync_contract_into_worktree(target, task, wt)

            self.assertEqual(first, second)
            # Exactly one file per id-named path; content equals the coordinator's.
            self.assertEqual(len(list((wt / ".shiki" / "tasks").glob("*.json"))), 1)
            self.assertEqual(len(list((wt / ".shiki" / "goals").glob("*.json"))), 1)
            self.assertEqual(len(list((wt / ".shiki" / "locks").glob("*.json"))), 1)
            self.assertEqual(
                _read(wt / ".shiki" / "tasks" / f"{TASK}.json"),
                _read(target / ".shiki" / "tasks" / f"{TASK}.json"),
            )

    def test_missing_coordinator_files_are_skipped(self):
        # A coordinator with no lock record must not crash the sync nor fabricate
        # a lock file in the worktree.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            target = self._coordinator(tmp, locks=["path:scripts/x.py"])
            (target / ".shiki" / "locks" / f"{TASK}.json").unlink()
            wt = self._stale_worktree(tmp, locks=["path:old.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, wt)

            self.assertNotIn(f".shiki/locks/{TASK}.json", synced)
            self.assertFalse((wt / ".shiki" / "locks" / f"{TASK}.json").exists())
            self.assertIn(f".shiki/tasks/{TASK}.json", synced)  # the rest still rode

    def test_no_op_when_worktree_is_the_target_checkout(self):
        # Headless dispatch requires an isolated worktree; syncing onto the target
        # itself would be a no-op self-copy, so it is refused.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            target = self._coordinator(tmp, locks=["path:scripts/x.py"])
            task = _read(target / ".shiki" / "tasks" / f"{TASK}.json")

            synced = sync_contract_into_worktree(target, task, target)

            self.assertEqual(synced, [])


class EvidenceOwnershipBoundTests(unittest.TestCase):
    """Gap (b): a ledger's transitive evidence rides on the branch only when the
    ledger is unambiguously THIS task's own (goal_id AND task_id both match)."""

    def _seed_task(self, target: Path, ledger_ids) -> dict:
        task = {
            "id": TASK,
            "goal_id": GOAL,
            "status": "review",
            "expected_branch": "b",
            "ledger_evidence": list(ledger_ids),
        }
        _write(target / ".shiki" / "tasks" / f"{TASK}.json", task)
        _write(target / ".shiki" / "worktrees" / f"{TASK}.json", {"path": "x", "branch": "b"})
        return task

    def _check_ledger(self, target: Path, ledger_id: str, *, goal_id, task_id, exec_name):
        _write(
            target / ".shiki" / "ledger" / f"{ledger_id}.json",
            {
                "id": ledger_id,
                "type": "check",
                "goal_id": goal_id,
                "task_id": task_id,
                "evidence": [f".shiki/runner/{exec_name}.json"],
            },
        )
        _write(target / ".shiki" / "runner" / f"{exec_name}.json", {"id": exec_name})

    def test_own_ledger_carries_its_exec_record(self):
        # Acceptance 2: a check ledger owned by this task carries its EXEC record
        # onto the branch, so MergeGate sees no dangling evidence.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            self._check_ledger(target, "L-OWN", goal_id=GOAL, task_id=TASK, exec_name="EXEC-own")
            task = self._seed_task(target, ["L-OWN"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertIn(".shiki/ledger/L-OWN.json", rel)
            self.assertIn(".shiki/runner/EXEC-own.json", rel)

    def test_foreign_goal_ledger_does_not_pull_refs(self):
        # Acceptance 3: a ledger of ANOTHER goal contributes nothing transitively.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            self._check_ledger(target, "L-FG", goal_id=OTHER_GOAL, task_id=TASK, exec_name="EXEC-fg")
            task = self._seed_task(target, ["L-FG"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertNotIn(".shiki/runner/EXEC-fg.json", rel)

    def test_foreign_task_ledger_does_not_pull_refs(self):
        # Acceptance 3: a ledger owned by a DIFFERENT task of the same goal is the
        # anti-padding case — its referenced paths are not pulled in.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            self._check_ledger(target, "L-FT", goal_id=GOAL, task_id=OTHER_TASK, exec_name="EXEC-ft")
            task = self._seed_task(target, ["L-FT"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertNotIn(".shiki/runner/EXEC-ft.json", rel)

    def test_goal_scoped_ledger_does_not_pull_refs(self):
        # The strict-bound differentiator: a goal-level ledger (empty task_id,
        # matching goal) is accepted by mergegate_check.ledger_entry_allowed_for_task
        # but is NOT expanded here — reusing that looser bound would defeat the
        # anti-padding property this sync exists to enforce.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            _write(
                target / ".shiki" / "ledger" / "L-GS.json",
                {
                    "id": "L-GS",
                    "type": "completion",
                    "goal_id": GOAL,
                    "task_id": "",
                    "evidence": [".shiki/reports/R-gs.json"],
                },
            )
            _write(target / ".shiki" / "reports" / "R-gs.json", {"id": "R-gs"})
            task = self._seed_task(target, ["L-GS"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertNotIn(".shiki/reports/R-gs.json", rel)

    def test_bound_is_per_ledger_not_all_or_nothing(self):
        # Own and foreign ledgers side by side: the own ledger's EXEC rides along,
        # the foreign ledger's does not.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            self._check_ledger(target, "L-OWN", goal_id=GOAL, task_id=TASK, exec_name="EXEC-own")
            self._check_ledger(target, "L-FT", goal_id=GOAL, task_id=OTHER_TASK, exec_name="EXEC-ft")
            task = self._seed_task(target, ["L-OWN", "L-FT"])
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertIn(".shiki/runner/EXEC-own.json", rel)
            self.assertNotIn(".shiki/runner/EXEC-ft.json", rel)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


class CoordinatorStagingAfterSyncTests(unittest.TestCase):
    """After a state sync the coordinator must leave NO mirror file untracked
    whose path also exists on the default branch.

    The loop writes .shiki JSON straight into the coordinator working tree and
    never stages it there. Once the same byte-identical file lands on the default
    branch (this task's PR merges), an untracked-vs-incoming collision makes a
    later ``git merge origin/main`` abort. The sync stages the coordinator's own
    copies (no cross-task/cross-goal carry); the loop's covering point then COMMITS
    them, so that — non-fast-forward — merge absorbs the returning files cleanly
    instead of aborting.
    """

    def _coordinator_with_branch(self, tmp: Path):
        """A real coordinator checkout with a bare origin and a task-branch
        worktree, returned as ``(target, remote, branch, wt)``."""
        remote = tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        target = tmp / "repo"
        target.mkdir()
        _git(target, "init", "-b", "main")
        _git(target, "config", "user.email", "t@t")
        _git(target, "config", "user.name", "t")
        (target / "README.md").write_text("x")
        _git(target, "add", "-A")
        _git(target, "commit", "-m", "init")
        _git(target, "remote", "add", "origin", str(remote))
        _git(target, "push", "-u", "origin", "main")

        branch = "shiki/task"
        wt = tmp / "wt"
        _git(target, "worktree", "add", "-b", branch, str(wt))
        _git(wt, "config", "user.email", "t@t")
        _git(wt, "config", "user.name", "t")
        return target, remote, branch, wt

    def _seed_own_evidence(self, target: Path, wt: Path, branch: str) -> None:
        """This task's own evidence, written into the coordinator unstaged, with
        the branch's upstream established (the loop's commit+push step)."""
        _write(target / ".shiki" / "ledger" / "L-A.json",
               {"id": "L-A", "type": "check", "goal_id": GOAL, "task_id": TASK,
                "evidence": [".shiki/runner/EXEC-1.json"]})
        _write(target / ".shiki" / "runner" / "EXEC-1.json", {"id": "EXEC-1"})
        _write(target / ".shiki" / "worktrees" / f"{TASK}.json",
               {"path": str(wt), "branch": branch})
        _write(target / ".shiki" / "tasks" / f"{TASK}.json",
               {"id": TASK, "goal_id": GOAL, "status": "review",
                "expected_branch": branch, "ledger_evidence": ["L-A"]})
        (wt / "seed.txt").write_text("seed")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "seed")
        _git(wt, "push", "-u", "origin", branch)

    def test_no_untracked_file_shared_with_default_branch_after_sync(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            target, remote, branch, wt = self._coordinator_with_branch(tmp)
            self._seed_own_evidence(target, wt, branch)

            _sync_state_to_branch(target, TASK, "L-A")

            # The task PR merges: origin/main fast-forwards to the branch tip, so
            # the delivered mirror paths now exist on the default branch too.
            _git(remote, "update-ref", "refs/heads/main", f"refs/heads/{branch}")
            _git(target, "fetch", "origin")

            default_paths = set(subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "origin/main"],
                cwd=str(target), capture_output=True, text=True, check=True,
            ).stdout.split())
            untracked = set(subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=str(target), capture_output=True, text=True, check=True,
            ).stdout.split())

            collisions = untracked & default_paths
            self.assertEqual(
                collisions, set(),
                f"untracked coordinator files collide with the default branch: {sorted(collisions)}",
            )
            # And the delivered evidence really is on the default branch (the
            # scenario is genuine, not vacuously collision-free).
            self.assertIn(f".shiki/tasks/{TASK}.json", default_paths)
            self.assertIn(".shiki/ledger/L-A.json", default_paths)

    def test_undelivered_foreign_ledger_is_never_carried_and_does_not_collide(self):
        """No cross-task / cross-goal carry — and none is needed.

        ``ledger_entry_allowed_for_task`` (mergegate_check.py:2408-2413) admits a
        ledger onto a task PR only when ``entry.task_id == task_id``, or when the
        entry has no task_id and its ``goal_id`` matches. A completed task's
        merge / mark_done bookkeeping ledger carries THAT task's own task_id, so
        carrying it onto a different task's branch would make
        ``enforce_untrusted_shiki_mutations`` block with "PR changes ledger … not
        listed in current task ledger_evidence" and fail every subsequent PR.

        It never has to be carried: an undelivered bookkeeping ledger is not on
        the default branch, so it never collides on ``git merge origin/main``.
        Only the delivered-and-returning files collide, and the sync removes those
        by staging THIS task's own evidence — never a foreign one.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            target, remote, branch, wt = self._coordinator_with_branch(tmp)
            # A completed OTHER task's post-merge bookkeeping ledger the loop left
            # untracked in the coordinator — it was never delivered anywhere.
            _write(target / ".shiki" / "ledger" / "L-FOREIGN.json",
                   {"id": "L-FOREIGN", "type": "merge", "goal_id": OTHER_GOAL,
                    "task_id": OTHER_TASK, "evidence": []})
            self._seed_own_evidence(target, wt, branch)

            _sync_state_to_branch(target, TASK, "L-A")

            # The foreign bookkeeping ledger was NOT carried onto this task's
            # branch (no cross-goal carry) and was NOT staged in the coordinator —
            # it is left exactly as the loop wrote it, untracked and on disk.
            branch_tree = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", branch],
                cwd=str(target), capture_output=True, text=True, check=True,
            ).stdout
            self.assertNotIn(".shiki/ledger/L-FOREIGN.json", branch_tree)
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=str(target), capture_output=True, text=True, check=True,
            ).stdout.split()
            self.assertNotIn(".shiki/ledger/L-FOREIGN.json", staged)
            self.assertIn(".shiki/ledger/L-A.json", staged)  # own evidence IS staged

            # In the real loop, execute_action's covering point then COMMITS the
            # coordinator's whole .shiki surface (including the untracked L-FOREIGN),
            # so the coordinator carries its own commit and the returning merge is
            # NON-fast-forward — the case that used to abort. Reproduce that commit so
            # this merge assertion is divergent, not vacuously fast-forward.
            shiki_loop._commit_coordinator_mirror(target)

            # The task PR merges: origin/main advances to the branch tip.
            _git(remote, "update-ref", "refs/heads/main", f"refs/heads/{branch}")
            _git(target, "fetch", "origin")
            # Genuinely divergent (non-fast-forward): neither ref is an ancestor of
            # the other — the coordinator committed L-FOREIGN, the branch never had it.
            self.assertNotEqual(subprocess.run(
                ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=str(target)).returncode, 0)
            self.assertNotEqual(subprocess.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=str(target)).returncode, 0)
            # The foreign ledger is absent from the default branch, so — being
            # undelivered — it never collides; the delivered evidence is byte-identical
            # on both sides. The merge that used to abort now succeeds cleanly, and the
            # foreign ledger is untouched.
            merge = subprocess.run(
                ["git", "merge", "origin/main"],
                cwd=str(target), capture_output=True, text=True,
            )
            self.assertEqual(merge.returncode, 0, f"{merge.stdout}{merge.stderr}")
            self.assertNotIn("would be overwritten by merge", merge.stderr)
            self.assertTrue((target / ".shiki" / "ledger" / "L-FOREIGN.json").exists())


class _FakeGh:
    """Stand in for ``shiki_loop._gh`` so ``merge`` / ``create_closeout_pr`` don't
    hit GitHub: ``pr list`` finds no prior closeout PR, ``pr create`` returns a
    fresh URL, everything else returns success. Real git (worktree add, commit,
    push) still runs against the bare origin."""

    def __init__(self, first: int = 100):
        self.next = first

    def __call__(self, target, args, check=True):
        args = list(args)
        if args[:2] == ["pr", "list"]:
            return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")
        if args[:2] == ["pr", "create"]:
            self.next += 1
            return types.SimpleNamespace(
                returncode=0, stdout=f"https://github.com/o/r/pull/{self.next}\n", stderr=""
            )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


@contextlib.contextmanager
def _patched_gh():
    orig = shiki_loop._gh
    shiki_loop._gh = _FakeGh()
    try:
        yield
    finally:
        shiki_loop._gh = orig


def _init_repo_with_origin(tmp: Path):
    """A real coordinator checkout with a bare origin, returned as ``(target,
    remote)``. ``origin/main`` is established so the closeout/goal_complete
    effectors can fetch and check out from it."""
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    target = tmp / "repo"
    target.mkdir()
    _git(target, "init", "-b", "main")
    _git(target, "config", "user.email", "t@t")
    _git(target, "config", "user.name", "t")
    (target / "README.md").write_text("x\n")
    _git(target, "add", "-A")
    _git(target, "commit", "-m", "init")
    _git(target, "remote", "add", "origin", str(remote))
    _git(target, "push", "-u", "origin", "main")
    return target, remote


def _use_github_fetch_url(target: Path, remote: Path) -> None:
    # Mirror the production runtime: a GitHub fetch URL (so `git fetch origin main`
    # is a harmless no-op) with the bare repo as the push URL (so real pushes work).
    _git(target, "remote", "set-url", "origin", "https://github.com/o/r.git")
    _git(target, "remote", "set-url", "--push", "origin", str(remote))


def _rev_parse(target: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(target), capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit_push_main(target: Path) -> None:
    _git(target, "add", "-A")
    _git(target, "commit", "-m", "seed")
    _git(target, "push", "origin", "main")
    _git(target, "update-ref", "refs/remotes/origin/main", "HEAD")


def _advance_origin_main(target: Path, mutate, *, n: int) -> None:
    """Build a commit on top of origin/main in a scratch worktree and repoint the
    coordinator's ``refs/remotes/origin/main`` at it — mirroring a merged closeout
    PR advancing main without touching the coordinator's HEAD/working tree."""
    wt = target.parent / f"mainwt-{n}"
    _git(target, "worktree", "add", "--force", "--detach", str(wt), "origin/main")
    mutate(wt)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "advance origin/main")
    sha = _rev_parse(wt)
    _git(target, "update-ref", "refs/remotes/origin/main", sha)
    _git(target, "worktree", "remove", "--force", str(wt))


def _seed_goal(target: Path, *, task_status="review", expected_pr=None) -> None:
    _write(target / ".shiki" / "goals" / f"{GOAL}.json",
           {"id": GOAL, "status": "in-progress", "title": "g", "outcome": "o",
            "risk_level": "low", "ledger_evidence": []})
    _write(target / ".shiki" / "dag" / f"{GOAL}.json",
           {"goal_id": GOAL, "nodes": [TASK], "edges": []})
    _write(target / ".shiki" / "tasks" / f"{TASK}.json",
           {"id": TASK, "goal_id": GOAL, "status": task_status, "title": "t", "scope": "s",
            "risk_level": "low", "assigned_runtime": "claude-code",
            "expected_branch": f"shiki/{TASK.lower()}-slice", "expected_pr": expected_pr,
            "locks": ["path:.shiki/**"], "required_skills": ["tdd"],
            "acceptance_checks": ["done"], "ledger_evidence": []})
    _write(target / ".shiki" / "locks" / f"{TASK}.json",
           {"task_id": TASK, "goal_id": GOAL, "state": "active", "owner": "shiki-run",
            "locks": ["path:.shiki/**"]})


class EveryEffectorCommitsCoordinatorMirrorTests(unittest.TestCase):
    """Widen the property from the three sync paths to EVERY effector, and COMMIT
    the coordinator mirror rather than merely staging it.

    The first fix staged inside ``_sync_state_to_branch`` (only ``create_pr`` /
    ``dispatch_repair`` / ``rerun_cca`` reach it); the second widened staging to a
    SINGLE covering point in ``execute_action``'s ``finally``. But MEASUREMENT showed
    staging clears only a FAST-FORWARD ``git merge origin/main``: the coordinator
    always carries its own commits, so its merge is always NON-fast-forward, where a
    merely-staged returning path still aborts — now with "Your local changes to the
    following files would be overwritten by merge". The covering point therefore
    COMMITS the coordinator's whole ``.shiki`` surface. HEAD advances (the coordinator
    branch is a disposable execution surface, never merged anywhere, so advancing it
    costs nothing — the previous contract's unmeasured "the local branch must not
    advance" non-goal is removed), nothing outside ``.shiki`` is committed, and no
    mirror record is deleted."""

    def _untracked_shiki(self, target: Path):
        out = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(target), capture_output=True, text=True, check=True,
        ).stdout.split()
        return sorted(p for p in out if p.startswith(".shiki/"))

    def _committed(self, target: Path, before: str):
        """(status, path) for every change committed between ``before`` and HEAD."""
        out = subprocess.run(
            ["git", "diff", "--name-status", before, "HEAD"],
            cwd=str(target), capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        return [(line.split("\t")[0], line.split("\t")[-1]) for line in out if line.strip()]

    def _assert_committed_mirror_only(self, target: Path, head_before: str) -> None:
        # The covering point COMMITS (not merely stages): HEAD advances, the commit
        # touches ONLY .shiki, deletes no append-only record, and leaves no untracked
        # .shiki behind.
        self.assertNotEqual(
            _rev_parse(target), head_before,
            "the covering point must COMMIT the coordinator mirror (HEAD advances)",
        )
        rows = self._committed(target, head_before)
        self.assertTrue(rows, "the covering point committed nothing")
        self.assertEqual([p for s, p in rows if not p.startswith(".shiki/")], [],
                         "nothing outside .shiki may be committed")
        self.assertEqual([p for s, p in rows if s.startswith("D")], [],
                         "the append-only mirror commit must delete no record")
        self.assertEqual(self._untracked_shiki(target), [],
                         "no .shiki file may be left untracked after the covering point")

    def test_merge_effector_commits_coordinator_mirror(self):
        with tempfile.TemporaryDirectory() as d:
            target, remote = _init_repo_with_origin(Path(d))
            _seed_goal(target, task_status="review", expected_pr=12)
            _commit_push_main(target)
            _use_github_fetch_url(target, remote)
            head = _rev_parse(target)
            with _patched_gh():
                result = execute_action(target, GOAL, {"action": "merge", "task_id": TASK}, repair_limit=3)
            self.assertEqual(result.get("action"), "merge", result)  # not stop_blocked
            self._assert_committed_mirror_only(target, head)

    def test_mark_done_effector_commits_coordinator_mirror(self):
        # mark_done writes a `done` task + a fresh check ledger and stages NOTHING
        # of its own — so a committed .shiki here PROVES the coverage comes from the
        # shared covering point, not effector-local staging (by construction).
        with tempfile.TemporaryDirectory() as d:
            target, _ = _init_repo_with_origin(Path(d))
            _seed_goal(target, task_status="review")  # left uncommitted: all .shiki is untracked
            head = _rev_parse(target)
            with _patched_gh():
                result = execute_action(target, GOAL, {"action": "mark_done", "task_id": TASK}, repair_limit=3)
            self.assertEqual(result.get("status"), "done", result)
            self._assert_committed_mirror_only(target, head)

    def test_goal_complete_effector_commits_coordinator_mirror(self):
        with tempfile.TemporaryDirectory() as d:
            target, remote = _init_repo_with_origin(Path(d))
            _seed_goal(target, task_status="review")
            _commit_push_main(target)

            def mutate(wt: Path) -> None:
                _write(wt / ".shiki" / "goals" / f"{GOAL}.json",
                       {"id": GOAL, "status": "complete", "title": "g", "outcome": "o",
                        "risk_level": "low", "ledger_evidence": ["L-CMP"]})
                _write(wt / ".shiki" / "tasks" / f"{TASK}.json",
                       {"id": TASK, "goal_id": GOAL, "status": "done", "title": "t", "scope": "s",
                        "risk_level": "low", "assigned_runtime": "claude-code",
                        "expected_branch": f"shiki/{TASK.lower()}-slice", "expected_pr": 12,
                        "locks": ["path:.shiki/**"], "required_skills": ["tdd"],
                        "acceptance_checks": ["done"], "ledger_evidence": []})
                _write(wt / ".shiki" / "locks" / f"{TASK}.json",
                       {"task_id": TASK, "goal_id": GOAL, "state": "released", "owner": "shiki-run",
                        "locks": ["path:.shiki/**"]})
                _write(wt / ".shiki" / "reports" / "R-CMP.json",
                       {"id": "R-CMP", "goal_id": GOAL, "status": "complete", "scorecard": {"goal_id": GOAL}})
                _write(wt / ".shiki" / "ledger" / "L-CMP.json",
                       {"id": "L-CMP", "goal_id": GOAL, "type": "completion", "summary": "done",
                        "evidence": [".shiki/reports/R-CMP.json"]})
            _advance_origin_main(target, mutate, n=1)
            _use_github_fetch_url(target, remote)
            head = _rev_parse(target)
            with _patched_gh():
                result = execute_action(target, GOAL, {"action": "goal_complete"}, repair_limit=3)
            self.assertEqual(result.get("goal_status"), "complete", result)
            self._assert_committed_mirror_only(target, head)

    def test_create_closeout_pr_effector_commits_coordinator_mirror(self):
        with tempfile.TemporaryDirectory() as d:
            target, remote = _init_repo_with_origin(Path(d))
            _seed_goal(target, task_status="review", expected_pr=12)
            _commit_push_main(target)
            _use_github_fetch_url(target, remote)
            head = _rev_parse(target)
            with _patched_gh():
                result = execute_action(
                    target, GOAL, {"action": "create_closeout_pr", "task_id": TASK}, repair_limit=3)
            self.assertEqual(result.get("action"), "create_closeout_pr", result)
            self._assert_committed_mirror_only(target, head)

    def test_unhandled_effector_is_covered_by_construction(self):
        # The covering point runs in a `finally`, so a FUTURE effector needs no
        # `git add`/`git commit` of its own: even an unhandled action COMMITS the
        # coordinator's untracked `.shiki` records before the error propagates.
        with tempfile.TemporaryDirectory() as d:
            target, _ = _init_repo_with_origin(Path(d))
            _seed_goal(target, task_status="review")
            _write(target / ".shiki" / "ledger" / "L-NEW.json",
                   {"id": "L-NEW", "goal_id": GOAL, "task_id": TASK, "type": "check", "evidence": []})
            head = _rev_parse(target)
            with self.assertRaises(ShikiError):
                execute_action(target, GOAL, {"action": "totally-unknown", "task_id": TASK}, repair_limit=3)
            self._assert_committed_mirror_only(target, head)
            self.assertIn(".shiki/ledger/L-NEW.json", [p for s, p in self._committed(target, head)])

    def test_covering_point_never_deletes_a_mirror_record(self):
        # A tracked (already committed) mirror record removed from the working tree
        # must NOT be recorded as a deletion by the covering point: `git add
        # --ignore-removal` never stages the removal and the plain index commit keeps
        # the append-only file — a `git commit -- .shiki` would instead re-read the
        # working tree and record the removal, emptying the append-only mirror.
        with tempfile.TemporaryDirectory() as d:
            target, _ = _init_repo_with_origin(Path(d))
            _seed_goal(target, task_status="review")
            keep = target / ".shiki" / "ledger" / "L-KEEP.json"
            _write(keep, {"id": "L-KEEP", "goal_id": GOAL, "task_id": TASK, "type": "check", "evidence": []})
            _commit_push_main(target)  # L-KEEP is now tracked on HEAD
            keep.unlink()  # the working tree loses the append-only record
            with _patched_gh():
                execute_action(target, GOAL, {"action": "mark_done", "task_id": TASK}, repair_limit=3)
            tree = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=str(target), capture_output=True, text=True, check=True,
            ).stdout
            self.assertIn(".shiki/ledger/L-KEEP.json", tree,
                          "the covering point must never delete a mirror record")


if __name__ == "__main__":
    unittest.main()
