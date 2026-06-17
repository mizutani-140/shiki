"""Goal-loop branch plumbing (PRD 0002 T1, gaps #1/#2).

Before create_pr the loop must land the implementer's work AND the full evidence
set on the task branch: (a) commit+push the runner's worktree implementation
(_commit_and_push_implementation), and (b) sync EVERY file referenced by the
task's ledger_evidence — each ledger plus the .shiki paths those ledgers
reference (runner/EXEC, reports/R) — not just a hardcoded subset
(_sync_state_to_branch via _evidence_relatives_for_task).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_loop import (
    _commit_and_push_implementation,
    _evidence_relatives_for_task,
    _sync_state_to_branch,
)
from shiki_process import ensure_control_dirs

TASK = "T-20260617T031753971551Z-24f8c42f"
GOAL = "G-20260617T031753970001Z-1de3b322"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _seed_evidence(target: Path):
    """Task referencing two ledgers; one ledger references EXEC + report files."""
    _write(target / ".shiki" / "ledger" / "L-A.json",
           {"id": "L-A", "type": "check", "goal_id": GOAL, "task_id": TASK,
            "evidence": [".shiki/runner/EXEC-1.json", ".shiki/reports/R-1.json"]})
    _write(target / ".shiki" / "ledger" / "L-B.json",
           {"id": "L-B", "type": "handoff", "goal_id": GOAL, "task_id": TASK, "evidence": []})
    _write(target / ".shiki" / "runner" / "EXEC-1.json", {"id": "EXEC-1"})
    _write(target / ".shiki" / "reports" / "R-1.json", {"id": "R-1"})
    _write(target / ".shiki" / "worktrees" / f"{TASK}.json", {"path": "x", "branch": "b"})
    task = {"id": TASK, "goal_id": GOAL, "status": "review", "expected_branch": "b",
            "ledger_evidence": ["L-A", "L-B"]}
    _write(target / ".shiki" / "tasks" / f"{TASK}.json", task)
    return task


class EvidenceRelativesTests(unittest.TestCase):
    def test_includes_task_worktree_all_ledgers_and_their_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            task = _seed_evidence(target)
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertIn(f".shiki/tasks/{TASK}.json", rel)
            self.assertIn(f".shiki/worktrees/{TASK}.json", rel)
            self.assertIn(".shiki/ledger/L-A.json", rel)
            self.assertIn(".shiki/ledger/L-B.json", rel)
            self.assertIn(".shiki/runner/EXEC-1.json", rel)   # referenced evidence
            self.assertIn(".shiki/reports/R-1.json", rel)

    def test_excludes_missing_and_non_shiki_refs(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            task = _seed_evidence(target)
            # add a ledger that references a missing file + a non-.shiki path
            _write(target / ".shiki" / "ledger" / "L-C.json",
                   {"id": "L-C", "type": "check", "goal_id": GOAL, "task_id": TASK,
                    "evidence": [".shiki/runner/EXEC-missing.json", "https://example/pr/1"]})
            task["ledger_evidence"].append("L-C")
            _write(target / ".shiki" / "tasks" / f"{TASK}.json", task)
            rel = set(_evidence_relatives_for_task(target, task))
            self.assertIn(".shiki/ledger/L-C.json", rel)            # the ledger file itself exists
            self.assertNotIn(".shiki/runner/EXEC-missing.json", rel)  # missing -> excluded
            self.assertNotIn("https://example/pr/1", rel)            # non-.shiki -> excluded

    def test_non_dict_ledger_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            task = _seed_evidence(target)
            # a ledger whose JSON is a list, not an object: read_json raises ShikiError
            (target / ".shiki" / "ledger" / "L-D.json").write_text("[1, 2, 3]", encoding="utf-8")
            task["ledger_evidence"].append("L-D")
            _write(target / ".shiki" / "tasks" / f"{TASK}.json", task)
            rel = set(_evidence_relatives_for_task(target, task))  # must not raise
            self.assertIn(".shiki/ledger/L-D.json", rel)


class _GitEnv:
    """A temp git repo with a bare origin and a task-branch worktree."""

    def __init__(self, tmp: Path):
        self.remote = tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.target = tmp / "repo"
        self.target.mkdir()
        _git(self.target, "init", "-b", "main")
        _git(self.target, "config", "user.email", "t@t")
        _git(self.target, "config", "user.name", "t")
        (self.target / "README.md").write_text("x")
        _git(self.target, "add", "-A")
        _git(self.target, "commit", "-m", "init")
        _git(self.target, "remote", "add", "origin", str(self.remote))
        _git(self.target, "push", "-u", "origin", "main")
        ensure_control_dirs(self.target)
        self.branch = "shiki/test-task"
        self.wt = tmp / "wt"
        _git(self.target, "worktree", "add", "-b", self.branch, str(self.wt))
        _git(self.wt, "config", "user.email", "t@t")
        _git(self.wt, "config", "user.name", "t")
        _write(self.target / ".shiki" / "worktrees" / f"{TASK}.json",
               {"path": str(self.wt), "branch": self.branch})

    def remote_has(self, relpath: str) -> bool:
        out = subprocess.run(["git", "cat-file", "-e", f"{self.branch}:{relpath}"],
                             cwd=str(self.remote), capture_output=True)
        return out.returncode == 0


class CommitAndPushImplementationTests(unittest.TestCase):
    def test_commits_and_pushes_the_runner_implementation(self):
        with tempfile.TemporaryDirectory() as d:
            env = _GitEnv(Path(d))
            _write(env.target / ".shiki" / "tasks" / f"{TASK}.json",
                   {"id": TASK, "goal_id": GOAL, "status": "review",
                    "expected_branch": env.branch, "title": "t", "ledger_evidence": []})
            # the "runner" left an uncommitted source change in the worktree
            (env.wt / "tests").mkdir(exist_ok=True)
            (env.wt / "tests" / "test_new.py").write_text("def test_x():\n    assert True\n")
            status = _commit_and_push_implementation(env.target, TASK)
            self.assertIn("pushed", status)
            self.assertTrue(env.remote_has("tests/test_new.py"))

    def test_no_changes_returns_clean_without_crash(self):
        with tempfile.TemporaryDirectory() as d:
            env = _GitEnv(Path(d))
            _write(env.target / ".shiki" / "tasks" / f"{TASK}.json",
                   {"id": TASK, "goal_id": GOAL, "status": "review",
                    "expected_branch": env.branch, "title": "t", "ledger_evidence": []})
            # no uncommitted work in the worktree, branch == main
            status = _commit_and_push_implementation(env.target, TASK)
            self.assertEqual(status, "no implementation changes to commit")

    def test_pushes_work_the_runner_already_committed(self):
        with tempfile.TemporaryDirectory() as d:
            env = _GitEnv(Path(d))
            _write(env.target / ".shiki" / "tasks" / f"{TASK}.json",
                   {"id": TASK, "goal_id": GOAL, "status": "review",
                    "expected_branch": env.branch, "title": "t", "ledger_evidence": []})
            # the runner committed its own work but did not push
            (env.wt / "impl.py").write_text("x = 1\n")
            _git(env.wt, "add", "-A")
            _git(env.wt, "commit", "-m", "runner commit")
            status = _commit_and_push_implementation(env.target, TASK)
            self.assertIn("pushed", status)
            self.assertTrue(env.remote_has("impl.py"))


class SyncStateFullEvidenceTests(unittest.TestCase):
    def test_syncs_full_ledger_evidence_set_to_branch(self):
        with tempfile.TemporaryDirectory() as d:
            env = _GitEnv(Path(d))
            task = _seed_evidence(env.target)
            task["expected_branch"] = env.branch
            _write(env.target / ".shiki" / "tasks" / f"{TASK}.json", task)
            _write(env.target / ".shiki" / "worktrees" / f"{TASK}.json",
                   {"path": str(env.wt), "branch": env.branch})
            # establish upstream first (the loop's commit+push step does this)
            (env.wt / "seed.txt").write_text("seed")
            _git(env.wt, "add", "-A"); _git(env.wt, "commit", "-m", "seed")
            _git(env.wt, "push", "-u", "origin", env.branch)
            _sync_state_to_branch(env.target, TASK, "L-A")
            for rel in (f".shiki/tasks/{TASK}.json", ".shiki/ledger/L-A.json",
                        ".shiki/ledger/L-B.json", ".shiki/runner/EXEC-1.json",
                        ".shiki/reports/R-1.json"):
                self.assertTrue(env.remote_has(rel), f"{rel} not synced to branch")


if __name__ == "__main__":
    unittest.main()
