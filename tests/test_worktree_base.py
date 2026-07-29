"""A task worktree is cut from the default branch, never from HEAD.

`ensure_physical_worktree` runs `git worktree add` from the coordinator's
checkout. Cutting a NEW task branch with no start-point (`git worktree add -b
<branch> <path>`) uses HEAD, so a task dispatched while the coordinator sits on
a feature branch inherits that branch's tree. On 2026-07-27/29 that dragged 19
foreign task files, 2 foreign goal files and 33 unlisted ledgers into a task
PR's diff (56 MergeGate blocking reasons), while shiki_loop.py asserts the
opposite: a task branch "is cut from main". These tests pin the fix — the new
branch is cut from the resolved default branch, and an unresolvable default
fails with a named reason instead of silently cutting from HEAD.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_process
import shiki_runtime
from shiki_process import ShikiError, ensure_control_dirs
from shiki_runtime import ensure_physical_worktree, resolve_default_branch_ref

TASK = "T-20260729T065622769632Z-9ee6b8e0"
GOAL = "G-20260729T065622761503Z-4479b0d3"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _rev(cwd: Path, ref: str) -> str:
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _repo_with_remote_ahead_of_local(tmp: Path) -> tuple[Path, str, str]:
    """A repo whose local default branch EXISTS but lags its remote.

    Models the real drift: after another goal merges to origin/main, a
    coordinator's local ``main`` and its ``origin/main`` tracking ref can both
    sit behind the remote tip until a fetch. Returns ``(target, stale_tip,
    remote_tip)`` with the coordinator parked on a feature branch, local ``main``
    and the tracking ref both pinned to ``stale_tip`` while the remote's ``main``
    is at ``remote_tip``.
    """
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    target = tmp / "repo"
    target.mkdir()
    _git(target, "init", "-b", "main")
    _git(target, "config", "user.email", "t@t")
    _git(target, "config", "user.name", "t")
    (target / "README.md").write_text("base\n")
    _git(target, "add", "-A")
    _git(target, "commit", "-m", "init on main")
    _git(target, "remote", "add", "origin", str(remote))
    _git(target, "push", "-u", "origin", "main")
    stale_tip = _rev(target, "main")

    # Advance the remote's main past the local branch, then pin the local branch
    # and its tracking ref back to the stale tip to model pre-fetch drift.
    (target / "README.md").write_text("advanced\n")
    _git(target, "commit", "-am", "second on main")
    _git(target, "push", "origin", "main")
    remote_tip = _rev(target, "main")

    _git(target, "checkout", "-b", "feature")
    _git(target, "branch", "-f", "main", stale_tip)
    _git(target, "update-ref", "refs/remotes/origin/main", stale_tip)
    return target, stale_tip, remote_tip


class _Coordinator:
    """A git repo whose default branch is `main`, currently on a feature branch.

    The feature branch carries a committed foreign mirror file, mirroring the
    coordinator "sitting on a branch carrying other goals' mirror state".
    """

    def __init__(self, tmp: Path, *, task_branch: str = "shiki/task-under-test"):
        self.target = tmp / "repo"
        self.target.mkdir()
        _git(self.target, "init", "-b", "main")
        _git(self.target, "config", "user.email", "t@t")
        _git(self.target, "config", "user.name", "t")
        (self.target / "README.md").write_text("base\n")
        _git(self.target, "add", "-A")
        _git(self.target, "commit", "-m", "init on main")
        self.main_tip = _rev(self.target, "main")

        # Coordinator moves onto a feature branch carrying a foreign goal's file.
        _git(self.target, "checkout", "-b", "coordinator-feature")
        foreign = self.target / ".shiki" / "goals" / "G-FOREIGN0000.json"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text('{"id": "G-FOREIGN0000"}\n', encoding="utf-8")
        _git(self.target, "add", "-A")
        _git(self.target, "commit", "-m", "foreign goal state")

        ensure_control_dirs(self.target)
        self.task_branch = task_branch
        self.wt = tmp / "wt"
        _write(
            self.target / ".shiki" / "worktrees" / f"{TASK}.json",
            {
                "task_id": TASK,
                "goal_id": GOAL,
                "branch": self.task_branch,
                "path": str(self.wt),
                "state": "registered",
                "locks": [],
            },
        )

    def task(self) -> dict:
        return {
            "id": TASK,
            "goal_id": GOAL,
            "assigned_runtime": "claude-code",
            "expected_branch": self.task_branch,
            "locks": [],
        }


class _IsolatedConfig:
    """Point shiki config at a controllable path so tests are hermetic.

    ``_default_branch_name`` consults ``load_default_config`` (which reads
    ``shiki_process.LOCAL_CONFIG``). Tests must not depend on the operator's real
    ~/.shiki/config.json, so redirect it to a temp path — empty by default,
    optionally seeded with a ``default_branch``.
    """

    def __init__(self, tmp: Path, *, default_branch: str | None = None):
        self.path = tmp / "config.json"
        if default_branch is not None:
            self.path.write_text(
                json.dumps({"default_branch": default_branch}), encoding="utf-8"
            )
        self._original = shiki_process.LOCAL_CONFIG

    def __enter__(self):
        shiki_process.LOCAL_CONFIG = self.path
        return self

    def __exit__(self, *exc):
        shiki_process.LOCAL_CONFIG = self._original


class EnsurePhysicalWorktreeBaseTests(unittest.TestCase):
    def test_cuts_new_branch_from_default_not_head(self):
        with tempfile.TemporaryDirectory() as d, _IsolatedConfig(Path(d)):
            env = _Coordinator(Path(d))
            record = ensure_physical_worktree(env.target, env.task())
            wt = Path(record["path"])

            # The new branch's tip equals the default branch tip...
            self.assertEqual(_rev(wt, "HEAD"), env.main_tip)
            self.assertEqual(_rev(env.target, env.task_branch), env.main_tip)
            # ...and the diff against the default branch is empty at creation.
            diff = subprocess.run(
                ["git", "diff", f"main...{env.task_branch}"],
                cwd=str(env.target),
                capture_output=True,
                text=True,
            )
            self.assertEqual(diff.stdout.strip(), "")
            # No mirror file belonging to another goal rode into the worktree.
            self.assertFalse((wt / ".shiki" / "goals" / "G-FOREIGN0000.json").exists())

    def test_cuts_from_remote_tip_when_local_default_lags(self):
        with tempfile.TemporaryDirectory() as d, _IsolatedConfig(Path(d)):
            target, stale_tip, remote_tip = _repo_with_remote_ahead_of_local(Path(d))
            ensure_control_dirs(target)
            task_branch = "shiki/task-under-test"
            wt = Path(d) / "wt"
            _write(
                target / ".shiki" / "worktrees" / f"{TASK}.json",
                {"task_id": TASK, "goal_id": GOAL, "branch": task_branch,
                 "path": str(wt), "state": "registered", "locks": []},
            )
            task = {"id": TASK, "goal_id": GOAL, "assigned_runtime": "claude-code",
                    "expected_branch": task_branch, "locks": []}

            record = ensure_physical_worktree(target, task)
            wt = Path(record["path"])
            # The new branch is cut from the refreshed remote tip, not the stale
            # local `main` — and the local branch itself is left untouched.
            self.assertEqual(_rev(wt, "HEAD"), remote_tip)
            self.assertNotEqual(_rev(wt, "HEAD"), stale_tip)
            self.assertEqual(_rev(target, "main"), stale_tip)

    def test_existing_worktree_at_recorded_path_is_reused_unchanged(self):
        with tempfile.TemporaryDirectory() as d, _IsolatedConfig(Path(d)):
            env = _Coordinator(Path(d))
            first = ensure_physical_worktree(env.target, env.task())
            wt = Path(first["path"])
            sentinel = wt / "runner-progress.txt"
            sentinel.write_text("work in progress\n", encoding="utf-8")
            head_before = _rev(wt, "HEAD")

            # A second call must reuse the worktree without recreating it.
            second = ensure_physical_worktree(env.target, env.task())
            self.assertEqual(Path(second["path"]), wt)
            self.assertTrue(sentinel.exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "work in progress\n")
            self.assertEqual(_rev(wt, "HEAD"), head_before)

    def test_unresolvable_default_fails_with_named_reason(self):
        with tempfile.TemporaryDirectory() as d, _IsolatedConfig(Path(d)):
            # A repo with no `main` and no origin/main: the default cannot resolve.
            target = Path(d) / "repo"
            target.mkdir()
            _git(target, "init", "-b", "work")
            _git(target, "config", "user.email", "t@t")
            _git(target, "config", "user.name", "t")
            (target / "README.md").write_text("x\n")
            _git(target, "add", "-A")
            _git(target, "commit", "-m", "init")
            ensure_control_dirs(target)
            _write(
                target / ".shiki" / "worktrees" / f"{TASK}.json",
                {"task_id": TASK, "goal_id": GOAL, "branch": "shiki/x",
                 "path": str(Path(d) / "wt"), "state": "registered", "locks": []},
            )
            task = {"id": TASK, "goal_id": GOAL, "assigned_runtime": "claude-code",
                    "expected_branch": "shiki/x", "locks": []}
            with self.assertRaises(ShikiError) as ctx:
                ensure_physical_worktree(target, task)
            self.assertIn("main", str(ctx.exception))
            self.assertIn("default branch", str(ctx.exception))


class ResolveDefaultBranchRefTests(unittest.TestCase):
    def _repo(self, tmp: Path, branch: str) -> Path:
        target = tmp / "repo"
        target.mkdir()
        _git(target, "init", "-b", branch)
        _git(target, "config", "user.email", "t@t")
        _git(target, "config", "user.name", "t")
        (target / "README.md").write_text("x\n")
        _git(target, "add", "-A")
        _git(target, "commit", "-m", "init")
        return target

    def test_resolves_local_default_when_no_remote_tracking_ref(self):
        with tempfile.TemporaryDirectory() as d, _IsolatedConfig(Path(d)):
            # No origin at all: the remote-tracking ref cannot exist, so the
            # local default branch is the fallback (dispatch works offline).
            target = self._repo(Path(d), "main")
            _git(target, "checkout", "-b", "feature")
            self.assertEqual(resolve_default_branch_ref(target), "main")

    def test_honors_configured_default_branch_name(self):
        with tempfile.TemporaryDirectory() as d, _IsolatedConfig(Path(d), default_branch="trunk"):
            target = self._repo(Path(d), "trunk")
            _git(target, "checkout", "-b", "feature")
            self.assertEqual(resolve_default_branch_ref(target), "trunk")

    def test_prefers_remote_tracking_ref_over_stale_local(self):
        with tempfile.TemporaryDirectory() as d, _IsolatedConfig(Path(d)):
            target, stale_tip, remote_tip = _repo_with_remote_ahead_of_local(Path(d))
            # The local `main` still exists but lags origin. The resolved base
            # must be the remote-tracking ref, refreshed to the remote tip — not
            # the stale local branch that would drag already-merged state back.
            self.assertEqual(resolve_default_branch_ref(target), "origin/main")
            self.assertEqual(_rev(target, "origin/main"), remote_tip)
            self.assertEqual(_rev(target, "main"), stale_tip)

    def test_falls_back_to_remote_tracking_ref(self):
        with tempfile.TemporaryDirectory() as d, _IsolatedConfig(Path(d)):
            remote = Path(d) / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            target = self._repo(Path(d), "main")
            _git(target, "remote", "add", "origin", str(remote))
            _git(target, "push", "-u", "origin", "main")
            # Leave only the remote-tracking ref: drop the local `main` branch.
            _git(target, "checkout", "-b", "feature")
            _git(target, "branch", "-D", "main")
            self.assertEqual(resolve_default_branch_ref(target), "origin/main")

    def test_unresolvable_raises_named_reason(self):
        with tempfile.TemporaryDirectory() as d, _IsolatedConfig(Path(d)):
            target = self._repo(Path(d), "work")  # no `main`, no origin
            with self.assertRaises(ShikiError) as ctx:
                resolve_default_branch_ref(target)
            self.assertIn("main", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
