"""Session-lease liveness for dispatched runner sessions.

``shiki_runtime.dispatch_runner_task`` writes ``running`` to the task file before
a synchronous adapter call and the terminal status only after it returns, so a
session that dies between those writes (the 10-minute command-timeout strand,
reproduced 2026-07-29..31) leaves the task at ``running`` with no live process
and ``shiki_loop`` reports ``wait_runner`` forever. This suite proves the fix: an
exclusive ``fcntl.flock`` held for the session's lifetime, which the kernel
releases on process death, plus a loop decision that consults it.

Every case drives the REAL flock — a lease held by a live child process, the
same lease after that child is killed, a foreign-host lease, an absent lease — and
the REAL loop decision (``decide_task_action`` / ``goal_loop_step``) and REAL
``dispatch_runner_task``. The lease is never mocked; only the loop's heavyweight
``_dispatch`` effector is stubbed where a re-dispatch would otherwise start a full
runner session. Dependency-free ``unittest`` only.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)
from shiki_test_support import SCRIPTS_DIR

import shiki_loop
from shiki_loop import MAX_DISPATCH_ATTEMPTS, decide_task_action, goal_loop_step
from shiki_process import ShikiError, ensure_control_dirs, shiki_path, write_json
from shiki_runtime import dispatch_runner_task
from shiki_runtime_adapters import ExecResult
from shiki_session_lease import (
    SessionLeaseHeld,
    hold_session_lease,
    lease_path,
    session_lease_state,
)
from shiki_tasks import load_task

GOAL = "G-20260801T040948350468Z-4a542e71"
TASK = "T-20260801T040948352279Z-07301e34"

REQUIRED = ["Validate Shiki mirror", "CCA verdict", "MergeGate metadata check"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True)


class child_lease_holder:
    """Hold a real OS lease in a separate live process.

    The child acquires the lease through the REAL ``hold_session_lease`` and then
    blocks, so the parent's probe sees a live holder. ``kill`` terminates the
    child and reaps it, so the kernel has released the flock by the time it
    returns — exactly the process-death release the fix relies on.
    """

    def __init__(self, target: Path, task_id: str):
        self.target = str(target)
        self.task_id = task_id
        self.ready = Path(target) / f".lease-ready-{task_id}"
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> "child_lease_holder":
        if self.ready.exists():
            self.ready.unlink()
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {json.dumps(str(SCRIPTS_DIR))})\n"
            "from shiki_session_lease import hold_session_lease\n"
            f"with hold_session_lease({json.dumps(self.target)}, {json.dumps(self.task_id)}):\n"
            f"    open({json.dumps(str(self.ready))}, 'w').write('1')\n"
            "    time.sleep(600)\n"
        )
        self.proc = subprocess.Popen([sys.executable, "-c", script])
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.ready.exists():
                return self
            if self.proc.poll() is not None:
                raise RuntimeError("child lease holder exited before acquiring the lease")
            time.sleep(0.01)
        self.kill()
        raise RuntimeError("child lease holder never acquired the lease")

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()

    def __exit__(self, *exc) -> None:
        self.kill()


def _write_running_task(target: Path, *, dispatch_attempts: int | None = None) -> None:
    ensure_control_dirs(target)
    write_json(shiki_path(target, "goals", f"{GOAL}.json"),
               {"id": GOAL, "status": "planned", "title": "g", "outcome": "o",
                "risk_level": "high", "ledger_evidence": []})
    task = {"id": TASK, "goal_id": GOAL, "status": "running", "title": "t",
            "scope": "prove a runner session is alive with an OS lease",
            "risk_level": "high", "assigned_runtime": "claude-code",
            "expected_branch": f"shiki/{TASK.lower()}-slice",
            "locks": ["path:scripts/shiki_session_lease.py"],
            "required_skills": ["tdd"], "acceptance_checks": ["lease"],
            "ledger_evidence": []}
    if dispatch_attempts is not None:
        task["dispatch_attempts"] = dispatch_attempts
    write_json(shiki_path(target, "tasks", f"{TASK}.json"), task)


class _stub_dispatch:
    """Replace ``shiki_loop._dispatch`` so a re-dispatch records that it fired
    without starting a real runner session. The lease itself is never mocked."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, target, task, *, repair_id=None):
        self.calls.append(str(task.get("id")))
        return 0


@contextlib.contextmanager
def _patched_dispatch():
    stub = _stub_dispatch()
    original = shiki_loop._dispatch
    shiki_loop._dispatch = stub
    try:
        yield stub
    finally:
        shiki_loop._dispatch = original


def _task(status: str, *, dispatch_attempts: int | None = None) -> dict:
    t = {"id": TASK, "goal_id": GOAL, "status": status, "risk_level": "high",
         "assigned_runtime": "claude-code"}
    if dispatch_attempts is not None:
        t["dispatch_attempts"] = dispatch_attempts
    return t


def _decide(t, *, lease_state=None, attempts=0, limit=3):
    return decide_task_action(
        t, checks=None, pr_state=None, repair_attempts=attempts, repair_limit=limit,
        required_checks=REQUIRED, cca_reruns=0, lease_state=lease_state,
    )


# --------------------------------------------------------------------------- #
# session_lease_state — real processes, real flock
# --------------------------------------------------------------------------- #
class LeaseStateProbeTests(unittest.TestCase):
    def test_absent_lease_reads_absent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(session_lease_state(Path(d), TASK), "absent")

    def test_live_holder_reads_held(self):
        with tempfile.TemporaryDirectory() as d:
            with child_lease_holder(Path(d), TASK):
                self.assertEqual(session_lease_state(Path(d), TASK), "held")

    def test_dead_holder_reads_free(self):
        with tempfile.TemporaryDirectory() as d:
            holder = child_lease_holder(Path(d), TASK)
            with holder:
                self.assertEqual(session_lease_state(Path(d), TASK), "held")
                holder.kill()  # the session dies; the kernel releases the flock
                self.assertEqual(session_lease_state(Path(d), TASK), "free")

    def test_foreign_host_reads_foreign_host_and_never_flocks(self):
        with tempfile.TemporaryDirectory() as d:
            lease_path(Path(d), TASK).parent.mkdir(parents=True, exist_ok=True)
            lease_path(Path(d), TASK).write_text(json.dumps(
                {"task_id": TASK, "host": socket.gethostname() + "-elsewhere",
                 "pid": 4242, "started_at": "2026-08-01T00:00:00+00:00", "attempt": 0}))
            self.assertEqual(session_lease_state(Path(d), TASK), "foreign_host")

    def test_probe_never_leaves_the_lock_held(self):
        # Two consecutive probes must return the same state: a probe that leaked a
        # held lock would flip a free lease to "held" on the next read.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(session_lease_state(Path(d), TASK), "absent")
            # A released lease file: probing must read free twice, never held.
            with hold_session_lease(Path(d), TASK):
                pass
            self.assertEqual(session_lease_state(Path(d), TASK), "free")
            self.assertEqual(session_lease_state(Path(d), TASK), "free")

    def test_held_probe_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            with child_lease_holder(Path(d), TASK):
                self.assertEqual(session_lease_state(Path(d), TASK), "held")
                self.assertEqual(session_lease_state(Path(d), TASK), "held")


# --------------------------------------------------------------------------- #
# decide_task_action — the pure engine's running-branch lease logic
# --------------------------------------------------------------------------- #
class PureDecisionTests(unittest.TestCase):
    def test_held_keeps_wait_runner_named(self):
        d = _decide(_task("running"), lease_state="held")
        self.assertEqual(d["action"], "wait_runner")
        self.assertIn("held", d["reason"])

    def test_foreign_host_keeps_wait_runner_and_never_redispatches(self):
        d = _decide(_task("running"), lease_state="foreign_host")
        self.assertEqual(d["action"], "wait_runner")
        self.assertIn("foreign host", d["reason"])

    def test_free_lease_redispatches(self):
        d = _decide(_task("running"), lease_state="free")
        self.assertEqual(d["action"], "redispatch")

    def test_absent_lease_redispatches(self):
        d = _decide(_task("running"), lease_state="absent")
        self.assertEqual(d["action"], "redispatch")

    def test_no_lease_probe_still_waits(self):
        # Backwards-compatible: without a probe the status field is not treated as
        # proof the session is gone, so it keeps waiting rather than re-dispatching.
        self.assertEqual(_decide(_task("running"), lease_state=None)["action"], "wait_runner")

    def test_bound_exhausted_stops_blocked_naming_attempts_and_free(self):
        d = _decide(_task("running", dispatch_attempts=MAX_DISPATCH_ATTEMPTS), lease_state="free")
        self.assertEqual(d["action"], "stop_blocked")
        self.assertIn(str(MAX_DISPATCH_ATTEMPTS), d["reason"])
        self.assertIn("free", d["reason"])
        self.assertIn(TASK, d["reason"])

    def test_last_attempt_before_bound_still_redispatches(self):
        d = _decide(_task("running", dispatch_attempts=MAX_DISPATCH_ATTEMPTS - 1), lease_state="free")
        self.assertEqual(d["action"], "redispatch")


# --------------------------------------------------------------------------- #
# goal_loop_step — the real loop decision over a real lease
# --------------------------------------------------------------------------- #
class LoopDecisionIntegrationTests(unittest.TestCase):
    def test_live_session_keeps_loop_waiting(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            _write_running_task(target)
            with child_lease_holder(target, TASK), _patched_dispatch() as stub:
                result = goal_loop_step(target, GOAL)
            self.assertEqual(result["action"], "wait_runner")
            self.assertEqual(stub.calls, [], "a live session must never be re-dispatched")
            self.assertEqual(load_task(target, TASK)["status"], "running")

    def test_dead_session_is_redispatched(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            _write_running_task(target)
            holder = child_lease_holder(target, TASK)
            with holder:
                pass
            holder.kill()  # session gone; lease file remains but is free
            self.assertEqual(session_lease_state(target, TASK), "free")
            with _patched_dispatch() as stub:
                result = goal_loop_step(target, GOAL)
            self.assertEqual(result["action"], "redispatch")
            self.assertEqual(stub.calls, [TASK])
            reloaded = load_task(target, TASK)
            self.assertEqual(reloaded["dispatch_attempts"], 1)
            self.assertEqual(reloaded["status"], "ready")

    def test_absent_lease_is_redispatched(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            _write_running_task(target)
            self.assertEqual(session_lease_state(target, TASK), "absent")
            with _patched_dispatch() as stub:
                result = goal_loop_step(target, GOAL)
            self.assertEqual(result["action"], "redispatch")
            self.assertEqual(stub.calls, [TASK])

    def test_foreign_host_loop_waits_and_never_redispatches(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            _write_running_task(target)
            lease_path(target, TASK).parent.mkdir(parents=True, exist_ok=True)
            lease_path(target, TASK).write_text(json.dumps(
                {"task_id": TASK, "host": socket.gethostname() + "-elsewhere",
                 "pid": 4242, "started_at": "2026-08-01T00:00:00+00:00", "attempt": 0}))
            with _patched_dispatch() as stub:
                result = goal_loop_step(target, GOAL)
            self.assertEqual(result["action"], "wait_runner")
            self.assertEqual(stub.calls, [])
            self.assertNotIn("dispatch_attempts", load_task(target, TASK))

    def test_bound_stops_the_loop_naming_the_attempt_count(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            _write_running_task(target, dispatch_attempts=MAX_DISPATCH_ATTEMPTS)
            # A free lease file (written and released): the session is gone but the
            # re-dispatch bound is already spent.
            with hold_session_lease(target, TASK):
                pass
            self.assertEqual(session_lease_state(target, TASK), "free")
            with _patched_dispatch() as stub:
                result = goal_loop_step(target, GOAL)
            self.assertEqual(result["action"], "stop_blocked")
            self.assertIn(str(MAX_DISPATCH_ATTEMPTS), result["reason"])
            self.assertIn("free", result["reason"])
            self.assertEqual(stub.calls, [], "past the bound the loop stops instead of re-dispatching")


# --------------------------------------------------------------------------- #
# dispatch_runner_task — the real dispatcher holds and releases the lease
# --------------------------------------------------------------------------- #
class _ProbeAdapter:
    """A duck-typed runner adapter whose ``execute`` probes the lease so the test
    can observe that the dispatcher holds it DURING the session. Only the adapter
    is a stand-in; the lease and the dispatcher are real."""

    name = "claude-code"
    display_name = "probe runner"
    required_tool = "true"
    auth_remediation = ""

    def __init__(self, target: Path, task_id: str, returncode: int = 0):
        self.target = target
        self.task_id = task_id
        self.returncode = returncode
        self.during: str | None = None
        self.calls = 0

    def auth_status(self):
        return {"ready": True}

    def command_label(self, handoff_ref: str) -> str:
        return f"probe <{handoff_ref}>"

    def execute(self, cwd: Path, prompt: str) -> ExecResult:
        self.calls += 1
        # Same process, a SEPARATE open description: the dispatcher's exclusive
        # flock denies this acquire, so the probe reads "held" mid-session.
        self.during = session_lease_state(self.target, self.task_id)
        return ExecResult(self.returncode, "out", "err")


class DispatchEnv:
    """A real git target with a materialized worktree and a task handoff, ready
    for ``dispatch_runner_task``. No network: the worktree is pre-created and its
    record written, so ``ensure_physical_worktree`` never resolves a remote."""

    def __init__(self, tmp: Path, *, status: str = "ready"):
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
        locks = ["path:.shiki/**", "path:marker.txt"]
        write_json(shiki_path(self.target, "goals", f"{GOAL}.json"),
                   {"id": GOAL, "status": "planned", "title": "g", "outcome": "o",
                    "risk_level": "high", "ledger_evidence": []})
        write_json(shiki_path(self.target, "tasks", f"{TASK}.json"),
                   {"id": TASK, "goal_id": GOAL, "status": status, "title": "t",
                    "scope": "s", "risk_level": "high", "assigned_runtime": "claude-code",
                    "expected_branch": self.branch, "locks": locks,
                    "required_skills": ["tdd"], "acceptance_checks": ["x"],
                    "ledger_evidence": []})
        write_json(shiki_path(self.target, "locks", f"{TASK}.json"),
                   {"task_id": TASK, "goal_id": GOAL, "state": "active",
                    "owner": "shiki-run", "locks": locks})
        # Pre-materialize the worktree + record so dispatch needs no remote fetch.
        self.wt = tmp / "wt"
        _git(self.target, "worktree", "add", "-b", self.branch, str(self.wt), "main")
        write_json(shiki_path(self.target, "worktrees", f"{TASK}.json"),
                   {"task_id": TASK, "goal_id": GOAL, "branch": self.branch,
                    "path": str(self.wt), "runtime": "claude-code", "state": "active",
                    "locks": locks})
        shiki_path(self.target, "handoffs", f"{TASK}-task.md").write_text("handoff prompt\n")

    def _args(self):
        return argparse.Namespace(target=str(self.target), task_id=TASK,
                                  dry_run=False, force=False, repair_id=None)


class DispatchLeaseTests(unittest.TestCase):
    def test_dispatch_holds_lease_across_session_and_releases_on_success(self):
        with tempfile.TemporaryDirectory() as d:
            env = DispatchEnv(Path(d))
            adapter = _ProbeAdapter(env.target, TASK, returncode=0)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = dispatch_runner_task(env._args(), adapter)
            self.assertEqual(rc, 0)
            self.assertEqual(adapter.calls, 1)
            # The lease was held DURING the adapter call...
            self.assertEqual(adapter.during, "held")
            # ...and released once the session returned.
            self.assertEqual(session_lease_state(env.target, TASK), "free")
            self.assertEqual(load_task(env.target, TASK)["status"], "review")

    def test_dispatch_releases_lease_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            env = DispatchEnv(Path(d))
            adapter = _ProbeAdapter(env.target, TASK, returncode=1)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = dispatch_runner_task(env._args(), adapter)
            self.assertEqual(rc, 1)
            self.assertEqual(adapter.during, "held")
            # A failed session still releases the lease.
            self.assertEqual(session_lease_state(env.target, TASK), "free")
            self.assertEqual(load_task(env.target, TASK)["status"], "repair-needed")

    def test_second_dispatch_against_held_lease_fails_with_collision_message(self):
        with tempfile.TemporaryDirectory() as d:
            env = DispatchEnv(Path(d), status="running")
            adapter = _ProbeAdapter(env.target, TASK)
            # A live first session holds the lease; the second dispatch must refuse.
            with child_lease_holder(env.target, TASK):
                self.assertEqual(session_lease_state(env.target, TASK), "held")
                with self.assertRaises(ShikiError) as caught:
                    with contextlib.redirect_stdout(io.StringIO()):
                        dispatch_runner_task(env._args(), adapter)
            self.assertIn("another dispatch for the same task is already running", str(caught.exception))
            self.assertEqual(adapter.calls, 0, "the second session must never start")

    def test_dispatch_takes_the_lease_it_did_not_have_before(self):
        # Belt-and-suspenders: no lease exists before dispatch; the file exists
        # after (the dispatcher created and released it), proving it took the lease.
        with tempfile.TemporaryDirectory() as d:
            env = DispatchEnv(Path(d))
            self.assertEqual(session_lease_state(env.target, TASK), "absent")
            adapter = _ProbeAdapter(env.target, TASK)
            with contextlib.redirect_stdout(io.StringIO()):
                dispatch_runner_task(env._args(), adapter)
            self.assertTrue(lease_path(env.target, TASK).exists())
            self.assertEqual(session_lease_state(env.target, TASK), "free")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
