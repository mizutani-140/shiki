"""T-20260805T034218808409Z-0f0d9640 — a granted lock is a precondition of dispatch.

Reproduced live 2026-08-04: ``shiki lock acquire`` conflicted (a superseded task
still held the paths), returned non-zero and wrote NO grant, but the visible
output was piped through ``tail -1`` so ``"locks_granted": false`` scrolled past.
``handoff task``, ``task status --status ready`` and ``runner claude`` all
proceeded, because dispatch never read ``.shiki/locks/``. The task was
implemented, reviewed and merged with no grant; the only signal appeared three
steps downstream as a closeout that unexpectedly demanded Guardian approval (the
SADR-0017 bookkeeping exemption needs the lock to move ``active -> released`` and
there was no lock to move).

This suite drives the REAL functions:

* ``evaluate_lock_grant`` — the pure, side-effect-free precondition predicate.
* ``dispatch_runner_task`` — refuses an ungranted task before any runner starts.
* ``dispatchable_task_ids`` — excludes any task the dispatcher would refuse.
* ``cmd_task_status`` — refuses to move an ungranted task into ``ready``.
* ``cmd_lock_acquire`` — the conflicted-acquire → dispatch sequence measured live.

Failure direction matters: a wrong predicate that refused every dispatch would
halt all autonomous work, which is worse than the defect. Hence the explicit
vacuity rule for empty ``locks`` and reasons that always name the task and the
specific locks. Dependency-free ``unittest`` only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_tasks
from shiki_process import ShikiError, ensure_control_dirs, read_json, shiki_path, write_json
from shiki_runtime import dispatch_runner_task, dispatchable_task_ids
from shiki_runtime_adapters import ExecResult
from shiki_tasks import cmd_lock_acquire, cmd_task_status, evaluate_lock_grant, load_task

GOAL = "G-20260805T034218806676Z-4459a890"
TASK = "T-20260805T034218808409Z-0f0d9640"
OTHER = "T-20260805T000000000000Z-superxxx"

MARKER = "path:marker.txt"
SHIKI = "path:.shiki/**"
DECLARED = [SHIKI, MARKER]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True)


def _task(task_id: str = TASK, *, status: str = "ready", locks=tuple(DECLARED)) -> dict:
    return {
        "id": task_id,
        "goal_id": GOAL,
        "title": "t",
        "scope": "s",
        "non_goals": [],
        "dependencies": [],
        "locks": list(locks),
        "assigned_runtime": "claude-code",
        "risk_level": "low",
        "required_skills": ["tdd"],
        "acceptance_checks": ["a"],
        "expected_branch": f"shiki/{task_id.lower()}",
        "expected_pr": None,
        "ledger_evidence": [],
        "status": status,
    }


class _Base(unittest.TestCase):
    """A bare ``.shiki`` mirror — enough for the predicate and its lighter gates."""

    def setUp(self) -> None:
        # cmd_* commands preflight a GitHub-first target; the lock precondition is
        # independent of that preflight, so stub it out for the light unit tests.
        self._orig_preflight = shiki_tasks.require_github_first_target
        shiki_tasks.require_github_first_target = lambda *_a, **_k: None
        self.addCleanup(setattr, shiki_tasks, "require_github_first_target", self._orig_preflight)

    def _target(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ensure_control_dirs(tmp)
        return tmp

    def _write_task(self, tmp: Path, task: dict) -> None:
        write_json(shiki_path(tmp, "tasks", f"{task['id']}.json"), task)

    def _write_grant(self, tmp: Path, task_id: str, locks, *, state: str = "active") -> None:
        write_json(
            shiki_path(tmp, "locks", f"{task_id}.json"),
            {
                "task_id": task_id,
                "goal_id": GOAL,
                "locks": list(locks),
                "state": state,
                "owner": "shiki-cli",
                "created_at": "2026-08-05T00:00:00+00:00",
            },
        )

    def _write_worktree(self, tmp: Path, task_id: str) -> None:
        write_json(
            shiki_path(tmp, "worktrees", f"{task_id}.json"),
            {
                "task_id": task_id,
                "goal_id": GOAL,
                "branch": f"shiki/{task_id.lower()}",
                "path": str(tmp / "wt" / task_id),
                "runtime": "claude-code",
                "state": "registered",
                "locks": list(DECLARED),
                "created_by": "shiki-cli",
                "created_at": "2026-08-05T00:00:00+00:00",
                "pr": None,
            },
        )


class EvaluateLockGrantPredicateTests(_Base):
    """The pure predicate: exists AND readable AND active AND covers, else refuse."""

    def test_empty_locks_is_granted_vacuously(self) -> None:
        # A task that declares nothing has nothing to grant; treating an empty list
        # as ungranted would refuse every lock-free task and halt autonomous work.
        tmp = self._target()
        self._write_task(tmp, _task(locks=[]))
        self.assertEqual(evaluate_lock_grant(tmp, TASK), (True, None))

    def test_active_covering_record_is_granted(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task())
        self._write_grant(tmp, TASK, DECLARED)
        self.assertEqual(evaluate_lock_grant(tmp, TASK), (True, None))

    def test_missing_record_refuses_naming_task_and_locks(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task())  # no lock record at all
        granted, reason = evaluate_lock_grant(tmp, TASK)
        self.assertFalse(granted)
        self.assertIn(TASK, reason)
        self.assertIn(MARKER, reason)
        self.assertIn(SHIKI, reason)

    def test_state_not_active_refuses(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task())
        self._write_grant(tmp, TASK, DECLARED, state="released")
        granted, reason = evaluate_lock_grant(tmp, TASK)
        self.assertFalse(granted)
        self.assertIn(TASK, reason)

    def test_partial_grant_refuses_naming_uncovered(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task())
        self._write_grant(tmp, TASK, [SHIKI])  # covers .shiki but not the marker
        granted, reason = evaluate_lock_grant(tmp, TASK)
        self.assertFalse(granted)
        self.assertIn(MARKER, reason)  # the uncovered lock is named

    def test_unreadable_or_malformed_record_refuses(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task())
        lock_file = shiki_path(tmp, "locks", f"{TASK}.json")
        lock_file.write_text("{ this is not json", encoding="utf-8")
        granted, reason = evaluate_lock_grant(tmp, TASK)  # must not raise
        self.assertFalse(granted)
        self.assertIn(TASK, reason)

    def test_predicate_has_no_side_effect(self) -> None:
        # Evaluating an ungranted task must never create or mutate a lock record.
        tmp = self._target()
        self._write_task(tmp, _task())
        evaluate_lock_grant(tmp, TASK)
        self.assertFalse(shiki_path(tmp, "locks", f"{TASK}.json").exists())


class DispatchableTaskIdsTests(_Base):
    """A task the dispatcher would refuse is not advertised as dispatchable."""

    def test_excludes_ungranted_includes_granted(self) -> None:
        tmp = self._target()
        granted_id = TASK
        ungranted_id = OTHER
        for task_id in (granted_id, ungranted_id):
            self._write_task(tmp, _task(task_id, status="ready"))
            self._write_worktree(tmp, task_id)
        self._write_grant(tmp, granted_id, DECLARED)  # only this one is granted
        ids = dispatchable_task_ids(tmp)
        self.assertIn(granted_id, ids)
        self.assertNotIn(ungranted_id, ids)


class CmdTaskStatusReadyGateTests(_Base):
    """`task status --status ready` refuses an ungranted task, succeeds once granted."""

    def _run(self, **kwargs) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_task_status(argparse.Namespace(**kwargs))

    def test_refuses_ready_when_ungranted_then_succeeds_when_granted(self) -> None:
        tmp = self._target()
        self._write_task(tmp, _task(status="planned"))
        with self.assertRaises(ShikiError) as caught:
            self._run(target=str(tmp), task_id=TASK, status="ready")
        self.assertIn(TASK, str(caught.exception))
        self.assertIn(MARKER, str(caught.exception))
        # The refusal never acquires the lock as a side effect.
        self.assertFalse(shiki_path(tmp, "locks", f"{TASK}.json").exists())
        self.assertEqual(load_task(tmp, TASK)["status"], "planned")

        # Grant the lock and the same transition succeeds.
        self._write_grant(tmp, TASK, DECLARED)
        self._run(target=str(tmp), task_id=TASK, status="ready")
        self.assertEqual(load_task(tmp, TASK)["status"], "ready")

    def test_other_transitions_are_unaffected_by_the_gate(self) -> None:
        # The gate is scoped to `ready`; a task with no grant may still be moved to
        # other statuses (e.g. an operator marking it blocked).
        tmp = self._target()
        self._write_task(tmp, _task(status="running"))
        self._run(target=str(tmp), task_id=TASK, status="blocked")
        self.assertEqual(load_task(tmp, TASK)["status"], "blocked")


# --------------------------------------------------------------------------- #
# Real dispatch: dispatch_runner_task refuses an ungranted task before any runner
# session starts. A live git target with a materialized worktree and handoff so
# the happy path needs no network (mirrors tests/test_session_lease.DispatchEnv).
# --------------------------------------------------------------------------- #
class _StubAdapter:
    """A duck-typed runner adapter whose ``execute`` merely records that it ran.

    Only the adapter is a stand-in; the dispatcher and the lock precondition are
    real. ``calls == 0`` after a refusal proves no runner session ever started."""

    name = "claude-code"
    display_name = "stub runner"
    required_tool = "true"
    auth_remediation = ""

    def __init__(self) -> None:
        self.calls = 0

    def auth_status(self) -> dict:
        return {"ready": True}

    def command_label(self, handoff_ref: str) -> str:
        return f"stub <{handoff_ref}>"

    def execute(self, cwd: Path, prompt: str) -> ExecResult:
        self.calls += 1
        return ExecResult(0, "out", "err")


class _DispatchEnv:
    def __init__(self, tmp: Path) -> None:
        self.target = tmp / "repo"
        self.target.mkdir()
        _git(self.target, "init", "-b", "main")
        _git(self.target, "config", "user.email", "t@t")
        _git(self.target, "config", "user.name", "t")
        (self.target / "README.md").write_text("x\n")
        _git(self.target, "add", "-A")
        _git(self.target, "commit", "-m", "init")
        _git(self.target, "remote", "add", "origin", "https://github.com/o/r.git")
        ensure_control_dirs(self.target)
        self.branch = f"shiki/{TASK.lower()}-slice"
        write_json(shiki_path(self.target, "goals", f"{GOAL}.json"),
                   {"id": GOAL, "status": "planned", "title": "g", "outcome": "o",
                    "risk_level": "low", "ledger_evidence": []})
        write_json(shiki_path(self.target, "tasks", f"{TASK}.json"),
                   {"id": TASK, "goal_id": GOAL, "status": "ready", "title": "t",
                    "scope": "s", "risk_level": "low", "assigned_runtime": "claude-code",
                    "expected_branch": self.branch, "locks": list(DECLARED),
                    "required_skills": ["tdd"], "acceptance_checks": ["x"],
                    "ledger_evidence": []})
        self.write_grant(list(DECLARED))
        self.wt = tmp / "wt"
        _git(self.target, "worktree", "add", "-b", self.branch, str(self.wt), "main")
        write_json(shiki_path(self.target, "worktrees", f"{TASK}.json"),
                   {"task_id": TASK, "goal_id": GOAL, "branch": self.branch,
                    "path": str(self.wt), "runtime": "claude-code", "state": "active",
                    "locks": list(DECLARED)})
        shiki_path(self.target, "handoffs", f"{TASK}-task.md").write_text("handoff prompt\n")

    def write_grant(self, locks) -> None:
        write_json(shiki_path(self.target, "locks", f"{TASK}.json"),
                   {"task_id": TASK, "goal_id": GOAL, "state": "active",
                    "owner": "shiki-run", "locks": list(locks)})

    def delete_grant(self) -> None:
        shiki_path(self.target, "locks", f"{TASK}.json").unlink()

    def add_foreign_lock(self, owner: str, locks) -> None:
        write_json(shiki_path(self.target, "locks", f"{owner}.json"),
                   {"task_id": owner, "goal_id": "G-other", "state": "active",
                    "owner": "other", "locks": list(locks),
                    "created_at": "2026-08-04T00:00:00+00:00"})

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(target=str(self.target), task_id=TASK,
                                  dry_run=False, force=False, repair_id=None)


class DispatchRunnerTaskGateTests(unittest.TestCase):
    def test_active_covering_record_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = _DispatchEnv(Path(d))
            adapter = _StubAdapter()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = dispatch_runner_task(env.args(), adapter)
            self.assertEqual(rc, 0)
            self.assertEqual(adapter.calls, 1)  # dispatches exactly as today

    def test_missing_record_refuses_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            env = _DispatchEnv(Path(d))
            env.delete_grant()
            adapter = _StubAdapter()
            with self.assertRaises(ShikiError) as caught:
                with contextlib.redirect_stdout(io.StringIO()):
                    dispatch_runner_task(env.args(), adapter)
            self.assertIn(TASK, str(caught.exception))
            self.assertIn(MARKER, str(caught.exception))
            self.assertEqual(adapter.calls, 0, "no runner session may start for an ungranted task")
            # The refusal never acquires the lock as a side effect.
            self.assertFalse(shiki_path(env.target, "locks", f"{TASK}.json").exists())

    def test_conflicted_acquire_then_dispatch_is_refused_naming_conflicting_task(self) -> None:
        # The exact 2026-08-04 sequence: a superseded task still holds the paths, so
        # `lock acquire` conflicts (returns non-zero, writes NO grant), and the
        # dispatch that followed it must now be refused naming that task.
        with tempfile.TemporaryDirectory() as d:
            env = _DispatchEnv(Path(d))
            env.delete_grant()
            env.add_foreign_lock(OTHER, [MARKER])

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cmd_lock_acquire(argparse.Namespace(
                    target=str(env.target), task_id=TASK, owner="shiki-cli"))
            self.assertEqual(code, 1, "a conflicted acquire must return non-zero")
            acquire_result = json.loads(buffer.getvalue())
            self.assertFalse(acquire_result["locks_granted"])
            # A conflicted acquire records no grant — the ungranted state is legible.
            self.assertFalse(shiki_path(env.target, "locks", f"{TASK}.json").exists())

            adapter = _StubAdapter()
            with self.assertRaises(ShikiError) as caught:
                with contextlib.redirect_stdout(io.StringIO()):
                    dispatch_runner_task(env.args(), adapter)
            self.assertIn(OTHER, str(caught.exception))  # names the conflicting task
            self.assertEqual(adapter.calls, 0)


if __name__ == "__main__":
    unittest.main()
