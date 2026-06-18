"""Loop-observed TDD evidence + per-task test command (PRD 0002 T2, ADR 0011).

Before it opens the PR, the goal loop must itself OBSERVE the task's tests pass
in the worktree — never trust the implementer's self-attestation (ADR 0011:
quality-gate evidence is a deterministic observable fact plus an independent
verifier, here the loop). The step:

  * runs the task's structured ``test_command`` (default
    ``python3 -m unittest discover -s tests``; ``acceptance_checks`` is free-form
    prose and is NEVER exec'd) in the worktree;
  * mirrors ``record_runner_result``'s EXEC pattern — writes
    ``.shiki/runner/EXEC-*.json`` and a ``type:"check"`` ledger naming skill
    ``tdd`` whose evidence points at that EXEC record, and appends the ledger id
    to ``task.ledger_evidence``;
  * is FAIL-CLOSED: a RED run does NOT open the PR (``execute_action`` returns
    ``stop_blocked`` with no ``gh pr create``);
  * threads a ``## TDD evidence (loop-observed)`` line into ``github_pr_body``.

These tests never touch GitHub: ``create_github_pr_for_task`` /
``_commit_and_push_implementation`` / ``_sync_state_to_branch`` are stubbed so
only the loop-owned TDD step and its branch decision are under test.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_loop
from shiki_loop import (
    _run_task_tests_in_worktree,
    execute_action,
    task_test_command,
)
from shiki_github import github_pr_body
from shiki_process import ensure_control_dirs, read_json

TASK = "T-20260617T090739479224Z-cfdbfa06"
GOAL = "G-20260617T031753970001Z-1de3b322"


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _seed(target: Path, worktree: Path, *, test_command: str | None) -> dict:
    ensure_control_dirs(target)
    worktree.mkdir(parents=True, exist_ok=True)
    task = {
        "id": TASK,
        "goal_id": GOAL,
        "status": "review",
        "title": "Loop-observed TDD evidence",
        "scope": "scope",
        "non_goals": [],
        "risk_level": "low",
        "expected_branch": "shiki/t2",
        "ledger_evidence": ["L-SEED"],
        "acceptance_checks": ["rm -rf / should never be exec'd"],
        "locks": [],
    }
    if test_command is not None:
        task["test_command"] = test_command
    _write(target / ".shiki" / "tasks" / f"{TASK}.json", task)
    _write(target / ".shiki" / "ledger" / "L-SEED.json",
           {"id": "L-SEED", "type": "handoff", "goal_id": GOAL, "task_id": TASK, "evidence": []})
    _write(target / ".shiki" / "worktrees" / f"{TASK}.json",
           {"task_id": TASK, "path": str(worktree), "branch": "shiki/t2"})
    return task


class TestCommandFieldTests(unittest.TestCase):
    def test_default_is_unittest_discover(self) -> None:
        self.assertEqual(
            task_test_command({"id": TASK}),
            "python3 -m unittest discover -s tests",
        )

    def test_override_is_honored(self) -> None:
        self.assertEqual(
            task_test_command({"id": TASK, "test_command": "pytest -q"}),
            "pytest -q",
        )

    def test_blank_override_falls_back_to_default(self) -> None:
        self.assertEqual(
            task_test_command({"id": TASK, "test_command": "   "}),
            "python3 -m unittest discover -s tests",
        )

    def test_acceptance_checks_are_not_used_as_command(self) -> None:
        # acceptance_checks is free-form prose+commands; it must never become the
        # command the loop exec's.
        cmd = task_test_command({"id": TASK, "acceptance_checks": ["rm -rf /"]})
        self.assertNotIn("rm -rf", cmd)


class RunTaskTestsTests(unittest.TestCase):
    def test_green_records_tdd_ledger_and_exec_record(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            worktree = Path(d) / "wt"
            _seed(target, worktree, test_command="true")
            ok, ledger_id, exec_rel, _summary = _run_task_tests_in_worktree(target, TASK)
            self.assertTrue(ok)
            # EXEC record written under .shiki/runner and referenced by the ledger.
            self.assertTrue(exec_rel.startswith(".shiki/runner/EXEC-"))
            self.assertTrue((target / exec_rel).is_file())
            exec_record = read_json(target / exec_rel)
            self.assertEqual(exec_record["returncode"], 0)
            self.assertEqual(exec_record["command"], "true")
            # The ledger is a type:check naming skill tdd, evidence -> EXEC ref.
            ledger = read_json(target / ".shiki" / "ledger" / f"{ledger_id}.json")
            self.assertEqual(ledger["type"], "check")
            self.assertIn("tdd", json.dumps(ledger).lower())
            self.assertIn(exec_rel, ledger["evidence"])
            # The ledger id is appended to the task's ledger_evidence.
            task = read_json(target / ".shiki" / "tasks" / f"{TASK}.json")
            self.assertIn(ledger_id, task["ledger_evidence"])

    def test_red_run_reports_not_ok_and_still_records_exec(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            worktree = Path(d) / "wt"
            _seed(target, worktree, test_command="false")
            ok, ledger_id, exec_rel, _summary = _run_task_tests_in_worktree(target, TASK)
            self.assertFalse(ok)
            # Even on red, the EXEC record + ledger are durable evidence of the run.
            self.assertTrue((target / exec_rel).is_file())
            self.assertEqual(read_json(target / exec_rel)["returncode"], 1)
            task = read_json(target / ".shiki" / "tasks" / f"{TASK}.json")
            self.assertIn(ledger_id, task["ledger_evidence"])

    def test_runs_in_the_worktree_not_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            worktree = Path(d) / "wt"
            _seed(target, worktree, test_command="test -f marker.txt")
            (worktree / "marker.txt").write_text("x")  # only present in the worktree
            ok, _ledger_id, _exec_rel, _summary = _run_task_tests_in_worktree(target, TASK)
            self.assertTrue(ok)

    def test_default_command_is_used_when_field_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            worktree = Path(d) / "wt"
            _seed(target, worktree, test_command=None)
            # A worktree with a passing tests/ package -> default discover passes.
            (worktree / "tests").mkdir()
            (worktree / "tests" / "__init__.py").write_text("")
            (worktree / "tests" / "test_ok.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n"
            )
            ok, _ledger_id, exec_rel, _summary = _run_task_tests_in_worktree(target, TASK)
            self.assertTrue(ok)
            self.assertEqual(
                read_json(target / exec_rel)["command"],
                "python3 -m unittest discover -s tests",
            )

    def test_never_raises_when_worktree_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            ensure_control_dirs(target)
            _write(target / ".shiki" / "tasks" / f"{TASK}.json",
                   {"id": TASK, "goal_id": GOAL, "status": "review",
                    "expected_branch": "b", "ledger_evidence": ["L-SEED"],
                    "test_command": "true"})
            _write(target / ".shiki" / "ledger" / "L-SEED.json",
                   {"id": "L-SEED", "type": "handoff", "goal_id": GOAL, "task_id": TASK, "evidence": []})
            # no worktree record at all
            ok, ledger_id, exec_rel, summary = _run_task_tests_in_worktree(target, TASK)
            self.assertFalse(ok)            # fail-closed: cannot prove green
            self.assertIsNone(ledger_id)
            self.assertIsNone(exec_rel)
            self.assertIn("worktree", summary)


class _Stubs:
    """Stub out everything in create_pr after the loop-owned TDD step."""

    def __init__(self) -> None:
        self.pr_created = False

    def commit(self, target, task_id):
        return "implementation committed and pushed to the task branch"

    def create_pr(self, target, task_id, *, base, head=None):
        self.pr_created = True
        return {"task_id": task_id, "pr": 123, "url": "u", "ledger_id": "L-PR"}

    def sync(self, target, task_id, ledger_id):
        return "synced"


class CreatePrBranchTests(unittest.TestCase):
    def _patch(self, stubs):
        self._orig = (
            shiki_loop._commit_and_push_implementation,
            shiki_loop.create_github_pr_for_task,
            shiki_loop._sync_state_to_branch,
            shiki_loop._run_pre_pr_code_review,
        )
        shiki_loop._commit_and_push_implementation = stubs.commit
        shiki_loop.create_github_pr_for_task = stubs.create_pr
        shiki_loop._sync_state_to_branch = stubs.sync
        # T3's pre-PR code-review gate now runs first in create_pr; these tests
        # target the TDD gate, so stub the upstream review to a clean verdict.
        shiki_loop._run_pre_pr_code_review = lambda target, task_id: {"status": "clean"}

    def tearDown(self) -> None:
        if hasattr(self, "_orig"):
            (
                shiki_loop._commit_and_push_implementation,
                shiki_loop.create_github_pr_for_task,
                shiki_loop._sync_state_to_branch,
                shiki_loop._run_pre_pr_code_review,
            ) = self._orig

    def test_green_tests_record_tdd_ledger_then_open_the_pr(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            worktree = Path(d) / "wt"
            _seed(target, worktree, test_command="true")
            stubs = _Stubs()
            self._patch(stubs)
            decision = {"action": "create_pr", "task_id": TASK, "reason": "review, no PR"}
            result = execute_action(target, GOAL, decision, repair_limit=3)
            self.assertEqual(result["action"], "create_pr")
            self.assertTrue(stubs.pr_created)
            self.assertEqual(result["pr"], 123)
            # The loop-observed TDD ledger rode along before the PR opened.
            tdd_ledger = result.get("tdd_ledger_id")
            self.assertIsNotNone(tdd_ledger)
            ledger = read_json(target / ".shiki" / "ledger" / f"{tdd_ledger}.json")
            self.assertEqual(ledger["type"], "check")
            self.assertIn("tdd", json.dumps(ledger).lower())

    def test_red_tests_block_the_pr_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            worktree = Path(d) / "wt"
            _seed(target, worktree, test_command="false")
            stubs = _Stubs()
            self._patch(stubs)
            decision = {"action": "create_pr", "task_id": TASK, "reason": "review, no PR"}
            result = execute_action(target, GOAL, decision, repair_limit=3)
            self.assertEqual(result["action"], "stop_blocked")
            self.assertFalse(stubs.pr_created)   # RED -> no PR opened
            self.assertIn("tdd", result["reason"].lower())
            # An EXEC record proving the red run is still durable evidence.
            self.assertTrue(str(result.get("tdd_exec", "")).startswith(".shiki/runner/EXEC-"))

    def test_acceptance_checks_prose_is_never_exec_d(self) -> None:
        # The task's acceptance_checks holds a dangerous string; the loop must run
        # the safe default test_command, not the prose.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            worktree = Path(d) / "wt"
            task = _seed(target, worktree, test_command=None)
            task["acceptance_checks"] = ["touch PWNED && false"]
            _write(target / ".shiki" / "tasks" / f"{TASK}.json", task)
            (worktree / "tests").mkdir()
            (worktree / "tests" / "__init__.py").write_text("")
            (worktree / "tests" / "test_ok.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertTrue(True)\n"
            )
            stubs = _Stubs()
            self._patch(stubs)
            decision = {"action": "create_pr", "task_id": TASK, "reason": "review, no PR"}
            result = execute_action(target, GOAL, decision, repair_limit=3)
            self.assertEqual(result["action"], "create_pr")
            self.assertFalse((worktree / "PWNED").exists())


class PrBodyTddSectionTests(unittest.TestCase):
    def test_pr_body_carries_loop_observed_tdd_section(self) -> None:
        body = github_pr_body({
            "goal_id": GOAL,
            "id": TASK,
            "scope": "scope",
            "non_goals": [],
            "acceptance_checks": ["x"],
            "ledger_evidence": ["L-1"],
            "locks": [],
            "risk_level": "low",
        })
        self.assertIn("## TDD evidence (loop-observed)", body)


if __name__ == "__main__":
    unittest.main()
