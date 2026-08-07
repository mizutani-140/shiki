"""Repair delivery: the goal loop commits and pushes a completed repair.

Before this, ``execute_action``'s ``dispatch_repair`` branch dispatched the repair
runner but delivered nothing: the runner wrote its fix into the worktree and the
PR never moved, so a coordinator had to ``git commit && git push`` the loop's own
work by hand (repro: RP-20260729T051350511459Z-50b5c0d7 on PR #181).

The ``dispatch_repair`` branch now calls ``_commit_and_push_implementation`` after
the runner returns — exactly as the ``create_pr`` branch already does — and fails
closed to ``stop_blocked`` (naming the failure) when the push does not happen. The
repair packet and its rendered handoff carry the same loop-owns-commit/push
prohibition the task handoff carries, plus a loop-owned push evidence line, so the
runner's delivery contract is unambiguous in both directions.

These tests import the real ``scripts/`` modules and assert observed behavior.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_loop
from shiki_config import configured_required_checks
from shiki_contracts import DEFAULT_REQUIRED_CHECKS
from shiki_loop import POLICY_GATE, execute_action
from shiki_process import ensure_control_dirs, shiki_path, write_json
from shiki_runtime import dispatch_runner_task, resolve_default_branch_ref
from shiki_runtime_adapters import RunnerAdapter
from shiki_tasks import (
    LOOP_OWNS_DELIVERY_PROHIBITION,
    cmd_handoff_repair,
    create_repair_packet,
    load_repair,
    load_task,
    write_task_handoff,
)

TASK = "T-20260729T065622764822Z-bac7ef27"
GOAL = "G-20260729T065622761503Z-4479b0d3"
IMPL_PR = 11
FIX_FILE = "repair-fix.txt"


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True
    )


def _rev_parse(repo, ref: str) -> str:
    out = _git(repo, "rev-parse", ref, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


# --------------------------------------------------------------------------- #
# A real git target: repo + bare origin, a single-task goal registered on main,
# and the impl already committed AND pushed to origin as the impl PR's branch.
# --------------------------------------------------------------------------- #
class RepairEnv:
    """The repository state the loop sees at the moment a repair is due.

    The task is ``review`` with an existing impl PR whose branch head does NOT
    carry the fix sentinel, so a head-dependent check is red — the exact state
    that routes ``decide_task_action`` to ``dispatch_repair``.
    """

    def __init__(self, tmp: Path, *, risk: str = "low"):
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
        _git(self.target, "remote", "add", "origin", str(self.remote))
        _git(self.target, "push", "-u", "origin", "main")
        # Pin origin/HEAD -> origin/main (as a real clone would) so the shared
        # default-branch resolver names `main` deterministically, independent of
        # the operator's ~/.shiki/config.json.
        _git(self.target, "remote", "set-head", "origin", "main")

        ensure_control_dirs(self.target)
        self.branch = f"shiki/{TASK.lower()}-slice"
        self._register_goal_on_main(risk=risk)

        # Push URL is the bare remote (so the loop's real git push works); the
        # fetch URL is GitHub-shaped so require_github_first_target passes and the
        # closeout's `git fetch origin main` is a harmless no-op (the fake `pr
        # merge` advances the origin/main tracking ref instead).
        _git(self.target, "remote", "set-url", "origin", "https://github.com/o/r.git")
        _git(self.target, "remote", "set-url", "--push", "origin", str(self.remote))

        # The task-branch worktree with the impl commit, pushed as the PR branch.
        self.wt = tmp / "wt"
        _git(self.target, "worktree", "add", "-b", self.branch, str(self.wt), "main")
        _git(self.wt, "config", "user.email", "t@t")
        _git(self.wt, "config", "user.name", "t")
        (self.wt / "slice-one.txt").write_text("impl\n")
        _git(self.wt, "add", "-A")
        _git(self.wt, "commit", "-m", "impl")
        _git(self.wt, "push", "-u", "origin", self.branch)

        write_json(
            shiki_path(self.target, "worktrees", f"{TASK}.json"),
            {"task_id": TASK, "goal_id": GOAL, "branch": self.branch,
             "path": str(self.wt), "runtime": "claude-code", "state": "active",
             "locks": ["path:.shiki/**", "path:slice-one.txt", f"path:{FIX_FILE}"]},
        )

    def _register_goal_on_main(self, *, risk: str):
        write_json(shiki_path(self.target, "goals", f"{GOAL}.json"),
                   {"id": GOAL, "status": "planned", "title": "g", "outcome": "o",
                    "risk_level": risk, "ledger_evidence": []})
        write_json(shiki_path(self.target, "dag", f"{GOAL}.json"),
                   {"goal_id": GOAL, "nodes": [TASK], "edges": []})
        locks = ["path:.shiki/**", "path:slice-one.txt", f"path:{FIX_FILE}"]
        write_json(shiki_path(self.target, "tasks", f"{TASK}.json"),
                   {"id": TASK, "goal_id": GOAL, "status": "review", "title": "t",
                    "scope": "deliver the repair to the PR",
                    "risk_level": risk, "assigned_runtime": "claude-code",
                    "expected_branch": self.branch, "expected_pr": IMPL_PR,
                    "locks": locks, "required_skills": ["tdd"],
                    "acceptance_checks": ["fix delivered"], "ledger_evidence": []})
        write_json(shiki_path(self.target, "locks", f"{TASK}.json"),
                   {"task_id": TASK, "goal_id": GOAL, "state": "active",
                    "owner": "shiki-run", "locks": locks})
        _git(self.target, "add", "-A")
        _git(self.target, "commit", "-m", "register goal+task")
        _git(self.target, "push")

    def origin_tip(self, branch: str) -> str:
        return _rev_parse(self.remote, f"refs/heads/{branch}")

    def origin_has(self, branch: str, relpath: str) -> bool:
        out = _git(self.remote, "cat-file", "-e", f"refs/heads/{branch}:{relpath}", check=False)
        return out.returncode == 0

    def worktree_commits_beyond_main(self) -> int:
        # Mirror _commit_and_push_implementation's ahead-count exactly: resolve the
        # default branch from origin (shiki_runtime.resolve_default_branch_ref, the
        # same call the loop makes) rather than a bare local `main`, so this harness
        # and the loop cannot drift apart on which base they count against.
        base = resolve_default_branch_ref(self.target)
        out = _git(self.wt, "rev-list", "--count", f"{base}..HEAD", check=False)
        try:
            return int((out.stdout or "0").strip())
        except ValueError:
            return 0


class _LoopGh:
    """A ``shiki_loop._gh`` stand-in whose check status is a real function of the
    PR head: a PR is green iff its branch head in the origin carries the fix
    sentinel. ``pr merge`` faithfully advances origin/main (and the coordinator's
    origin/main tracking ref) so the SADR-0012 closeout can cut from it."""

    def __init__(self, env: RepairEnv, required: list[str], failing: str):
        self.env = env
        self.required = list(required)
        self.failing = failing
        self.branch_by_pr = {IMPL_PR: env.branch}
        self.merged: set[int] = set()
        self.next_pr = IMPL_PR + 1
        self.calls: list[list[str]] = []

    def _head_has_fix(self, branch: str) -> bool:
        return bool(branch) and self.env.origin_has(branch, FIX_FILE)

    def _checks_payload(self, branch: str) -> str:
        green = self._head_has_fix(branch)
        rows = []
        for name in self.required:
            bucket = "pass" if (green or name != self.failing) else "fail"
            rows.append({"name": name, "bucket": bucket, "startedAt": "2026-07-29T00:00:01Z"})
        return json.dumps(rows)

    def __call__(self, target, args, check=True):
        args = list(args)
        self.calls.append(args)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        head = args[:2]
        if head == ["pr", "view"]:
            num = int(args[2])
            branch = self.branch_by_pr.get(num, "")
            sha = self.env.origin_tip(branch)
            merged = num in self.merged
            r.stdout = json.dumps({
                "state": "MERGED" if merged else "OPEN",
                "mergedAt": "2026-07-29T00:00:00Z" if merged else None,
                "headRefOid": sha,
            })
        elif head == ["pr", "checks"]:
            r.stdout = self._checks_payload(self.branch_by_pr.get(int(args[2]), ""))
        elif head == ["pr", "merge"]:
            num = int(args[2])
            branch = self.branch_by_pr.get(num, "")
            self.merged.add(num)
            sha = self.env.origin_tip(branch)
            if sha:
                _git(self.env.remote, "update-ref", "refs/heads/main", sha, check=False)
                _git(self.env.target, "update-ref", "refs/remotes/origin/main", sha, check=False)
        elif head == ["pr", "list"]:
            r.stdout = "[]"
        elif head == ["pr", "create"]:
            branch = ""
            for i, token in enumerate(args):
                if token == "--head" and i + 1 < len(args):
                    branch = args[i + 1]
            num = self.next_pr
            self.next_pr += 1
            self.branch_by_pr[num] = branch
            r.stdout = f"https://github.com/o/r/pull/{num}\n"
        elif head == ["run", "list"]:
            r.stdout = "[]"
        return r


def _fake_repair_dispatch(target, task, *, repair_id=None):
    """Stand in for the repair runner: write the fix into the worktree and mark
    the task ``review`` (runner exited 0). It never commits or pushes — that is
    the delivery step under test."""
    from shiki_tasks import worktree_record

    record = worktree_record(target, task["id"])
    (Path(record["path"]) / FIX_FILE).write_text("fix\n")
    live = load_task(target, task["id"])
    live["status"] = "review"
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), live)
    return 0


def _fake_repair_dispatch_noop(target, task, *, repair_id=None):
    """A repair runner that fixes NOTHING: it exits 0 but writes no change, so the
    worktree — and therefore the pushed PR head — is unchanged."""
    live = load_task(target, task["id"])
    live["status"] = "review"
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), live)
    return 0


def _worktree_task_file(target, task_id):
    from shiki_tasks import worktree_record

    record = worktree_record(target, task_id)
    return Path(record["path"]) / ".shiki" / "tasks" / f"{task_id}.json"


def _fake_repair_dispatch_bookkeeping_only(target, task, *, repair_id=None):
    """Reproduce the measured PR #310 no-op shape: the ONLY change is the task's own
    mirror record in the WORKTREE — the loop-authored bookkeeping a real
    ``sync_contract_into_worktree`` writes (``status`` / ``cca_rerun_count``) — and
    NO source. ``_commit_and_push_implementation`` ``git add -A``s that lone
    ``.shiki/tasks/<id>.json`` change, so the PR head advances although the repairer
    fixed nothing (the exact ``.shiki/tasks/<id>.json | 5 +++--`` measured shape)."""
    wt_task = _worktree_task_file(target, task["id"])
    live = json.loads(wt_task.read_text())
    live["cca_rerun_count"] = int(live.get("cca_rerun_count") or 0) + 1
    live["status"] = "review"
    write_json(wt_task, live)
    # Keep the coordinator status consistent, as the real runner dispatch does.
    coord = load_task(target, task["id"])
    coord["status"] = "review"
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), coord)
    return 0


def _fake_repair_dispatch_source_and_bookkeeping(target, task, *, repair_id=None):
    """A repair that changes BOTH a source file inside the locks AND the task's own
    mirror record in the worktree. Delivery must proceed on the source change; the
    loop-authored bookkeeping alongside it does not disqualify it."""
    from shiki_tasks import worktree_record

    wt = Path(worktree_record(target, task["id"])["path"])
    (wt / FIX_FILE).write_text("fix\n")
    wt_task = _worktree_task_file(target, task["id"])
    live = json.loads(wt_task.read_text())
    live["status"] = "review"
    write_json(wt_task, live)
    coord = load_task(target, task["id"])
    coord["status"] = "review"
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), coord)
    return 0


class RepairPacketContractTests(unittest.TestCase):
    """Acceptance #4: the generated repair packet + its rendered handoff carry the
    loop-owned push evidence and the same commit/push prohibition the task handoff
    carries."""

    def test_packet_and_handoff_state_the_delivery_contract(self):
        with tempfile.TemporaryDirectory() as d:
            env = RepairEnv(Path(d))
            orig = shiki_loop._dispatch
            shiki_loop._dispatch = _fake_repair_dispatch
            try:
                info = shiki_loop._dispatch_repair(
                    env.target, load_task(env.target, TASK),
                    ["Validate Shiki mirror"], attempt=1,
                )
            finally:
                shiki_loop._dispatch = orig

            packet = load_repair(env.target, info["repair_id"])
            # evidence_required names the push, attributed to the loop.
            self.assertTrue(
                any("push" in item.lower() and "task branch" in item.lower()
                    for item in packet["evidence_required"]),
                packet["evidence_required"],
            )
            # A prohibited line matching the task handoff's commit/push prohibition.
            self.assertIn(LOOP_OWNS_DELIVERY_PROHIBITION, packet["prohibited_changes"])

            # The rendered repair handoff has an Evidence Required section carrying
            # the push instruction AND the shared prohibition.
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_handoff_repair(argparse.Namespace(
                    target=str(env.target), repair_id=info["repair_id"]))
            handoff = (env.target / ".shiki" / "handoffs"
                       / f"{info['repair_id']}-repair.md").read_text()
            self.assertIn("## Evidence Required", handoff)
            self.assertIn("pushes it", handoff)
            self.assertIn(LOOP_OWNS_DELIVERY_PROHIBITION, handoff)

            # "Matching the task handoff" — the identical line rides on both.
            task_handoff = write_task_handoff(env.target, TASK)[0].read_text()
            self.assertIn(LOOP_OWNS_DELIVERY_PROHIBITION, task_handoff)


class RepairPushFailureTests(unittest.TestCase):
    """Acceptance #3: a failed delivery stops the loop with a NAMED reason instead
    of silently letting it spin and burn further repair attempts."""

    def test_push_failure_stops_blocked_and_does_not_multiply_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            env = RepairEnv(Path(d))
            orig_dispatch = shiki_loop._dispatch
            orig_push = shiki_loop._commit_and_push_implementation
            shiki_loop._dispatch = _fake_repair_dispatch
            shiki_loop._commit_and_push_implementation = (
                lambda target, task_id: "implementation committed; push failed — push the task branch manually"
            )
            try:
                result = execute_action(
                    env.target, GOAL,
                    {"action": "dispatch_repair", "task_id": TASK,
                     "failed_checks": ["Validate Shiki mirror"]},
                    repair_limit=3,
                )
            finally:
                shiki_loop._dispatch = orig_dispatch
                shiki_loop._commit_and_push_implementation = orig_push

            self.assertEqual(result["action"], "stop_blocked")
            self.assertIn("push failed", result["reason"].lower() + result.get("impl_commit", "").lower())
            self.assertIn("did not move", result["reason"])
            # Exactly one repair packet was created for this attempt — the failure
            # was surfaced, not swallowed into another silent attempt.
            packets = list((env.target / ".shiki" / "repairs").glob("RP-*.json"))
            self.assertEqual(len(packets), 1, packets)

    def test_noop_repair_does_not_silently_consume_an_attempt(self):
        # A repair that produces NO change pushes cleanly ("Everything up-to-date",
        # returncode 0), so `_commit_and_push_implementation` reports success — but
        # the PR head never moved and the checks can never turn green. The guard
        # must catch the unmoved head and stop_blocked, NOT let the loop spin and
        # burn further repair attempts against an unchanged PR (the repro this task
        # removes). This is the fail-closed guarantee the push return string alone
        # cannot provide.
        with tempfile.TemporaryDirectory() as d:
            env = RepairEnv(Path(d))
            head_before = env.origin_tip(env.branch)
            orig = shiki_loop._dispatch
            shiki_loop._dispatch = _fake_repair_dispatch_noop
            try:
                result = execute_action(
                    env.target, GOAL,
                    {"action": "dispatch_repair", "task_id": TASK,
                     "failed_checks": ["Validate Shiki mirror"]},
                    repair_limit=3,
                )
            finally:
                shiki_loop._dispatch = orig

            # The real commit/push ran and reported a (no-op) success...
            self.assertIn("pushed to the task branch", result.get("impl_commit", ""))
            # ...yet the PR head did not move, so the loop stops closed, named.
            self.assertEqual(result["action"], "stop_blocked")
            self.assertIn("did not advance", result["reason"])
            self.assertEqual(env.origin_tip(env.branch), head_before)
            packets = list((env.target / ".shiki" / "repairs").glob("RP-*.json"))
            self.assertEqual(len(packets), 1, packets)


class RepairImplementerChangeDiscriminatorTests(unittest.TestCase):
    """The delivery discriminator is what the IMPLEMENTER changed, not head movement.

    ``_commit_and_push_implementation`` ``git add -A``s the whole worktree, so the
    runner dispatch's own bookkeeping (the task's mirror record, synced/rewritten
    each cycle) advances the PR head with zero source. The old guard read that as
    delivery and burned the whole repair budget to ``stop_guardian`` (PR #310,
    2026-08-07: three attempts, three ``.shiki/tasks/<id>.json | 5 +++--`` commits).
    """

    def _dispatch_repair(self, env):
        return execute_action(
            env.target, GOAL,
            {"action": "dispatch_repair", "task_id": TASK,
             "failed_checks": ["Validate Shiki mirror"]},
            repair_limit=3,
        )

    def test_bookkeeping_only_repair_stops_at_this_attempt(self):
        # Acceptance #1/#6: the ONLY change is the task's own mirror record — the
        # measured PR #310 shape. The push reports success and the head DID advance,
        # yet the repair delivered nothing the implementer owns, so the loop stops
        # HERE with a named reason instead of consuming further silent attempts.
        with tempfile.TemporaryDirectory() as d:
            env = RepairEnv(Path(d))
            head_before = env.origin_tip(env.branch)
            orig = shiki_loop._dispatch
            shiki_loop._dispatch = _fake_repair_dispatch_bookkeeping_only
            try:
                result = self._dispatch_repair(env)
            finally:
                shiki_loop._dispatch = orig

            # The commit/push ran and the head advanced (so the head-movement guard
            # cannot catch it)...
            self.assertIn("pushed to the task branch", result.get("impl_commit", ""))
            self.assertNotEqual(env.origin_tip(env.branch), head_before)
            # ...but the pushed commit is the measured shape — exactly the task's own
            # mirror record, zero source — so the discriminator stops the loop, named.
            self.assertEqual(result["action"], "stop_blocked")
            self.assertIn("no implementer change", result["reason"])
            changed = _git(
                env.wt, "diff", "--name-only", f"{head_before}..HEAD"
            ).stdout.split()
            self.assertEqual(changed, [f".shiki/tasks/{TASK}.json"])
            # Exactly one repair packet — surfaced at THIS attempt, not swallowed
            # into further attempts that would only end at stop_guardian.
            packets = list((env.target / ".shiki" / "repairs").glob("RP-*.json"))
            self.assertEqual(len(packets), 1, packets)

    def test_source_only_repair_proceeds_exactly_as_today(self):
        # Acceptance #2: a source file inside the locks changed, so delivery proceeds
        # exactly as a normal repair does today.
        with tempfile.TemporaryDirectory() as d:
            env = RepairEnv(Path(d))
            head_before = env.origin_tip(env.branch)
            orig = shiki_loop._dispatch
            shiki_loop._dispatch = _fake_repair_dispatch
            try:
                result = self._dispatch_repair(env)
            finally:
                shiki_loop._dispatch = orig

            self.assertEqual(result["action"], "dispatch_repair")
            self.assertIn("pushed to the task branch", result["impl_commit"])
            self.assertNotEqual(env.origin_tip(env.branch), head_before)
            self.assertTrue(env.origin_has(env.branch, FIX_FILE))

    def test_source_and_bookkeeping_repair_proceeds(self):
        # Acceptance #3: a repair that changes BOTH a source file and the task's own
        # mirror record proceeds — the bookkeeping alongside a real fix is not
        # disqualifying.
        with tempfile.TemporaryDirectory() as d:
            env = RepairEnv(Path(d))
            head_before = env.origin_tip(env.branch)
            orig = shiki_loop._dispatch
            shiki_loop._dispatch = _fake_repair_dispatch_source_and_bookkeeping
            try:
                result = self._dispatch_repair(env)
            finally:
                shiki_loop._dispatch = orig

            self.assertEqual(result["action"], "dispatch_repair")
            self.assertIn("pushed to the task branch", result["impl_commit"])
            self.assertNotEqual(env.origin_tip(env.branch), head_before)
            self.assertTrue(env.origin_has(env.branch, FIX_FILE))
            # Both files rode the commit; delivery keyed off the source change.
            changed = set(
                _git(env.wt, "diff", "--name-only", f"{head_before}..HEAD").stdout.split()
            )
            self.assertIn(FIX_FILE, changed)
            self.assertIn(f".shiki/tasks/{TASK}.json", changed)


class RepairDeliveryIntegrationTests(unittest.TestCase):
    """Acceptance #1/#2/#6: a repair that genuinely fixes a head-dependent check
    drives dispatch_repair -> merge -> ... -> goal complete (never stop_guardian),
    moving the origin branch tip with no operator git command."""

    def _required(self, target):
        return list(configured_required_checks(target, DEFAULT_REQUIRED_CHECKS))

    def test_completed_repair_drives_to_complete_not_guardian(self):
        with tempfile.TemporaryDirectory() as d:
            env = RepairEnv(Path(d))
            required = self._required(env.target)
            failing = next((c for c in required if c not in (POLICY_GATE, "CCA verdict")), required[0])
            fake_gh = _LoopGh(env, required, failing)

            orig_gh = shiki_loop._gh
            orig_dispatch = shiki_loop._dispatch
            shiki_loop._gh = fake_gh
            shiki_loop._dispatch = _fake_repair_dispatch
            try:
                origin_before = env.origin_tip(env.branch)

                # Step 1 is the repair: the runner writes the fix, the loop commits
                # and pushes it to the PR head (#6 — no operator git command).
                first = shiki_loop.goal_loop_step(env.target, GOAL)
                self.assertEqual(first["action"], "dispatch_repair")
                self.assertIn("pushed to the task branch", first["impl_commit"])

                # #2: the origin branch tip moved and the repair worktree has commits
                # beyond its base (impl + repair).
                self.assertNotEqual(env.origin_tip(env.branch), origin_before)
                self.assertTrue(env.origin_has(env.branch, FIX_FILE))
                self.assertGreaterEqual(env.worktree_commits_beyond_main(), 2)

                actions = [first["action"]]
                for _ in range(11):
                    result = shiki_loop.goal_loop_step(env.target, GOAL)
                    actions.append(result["action"])
                    if result["action"] == "goal_complete":
                        self.assertEqual(result.get("goal_status"), "complete")
                        break
                    if result["action"] in {"stop_guardian", "stop_blocked"}:
                        break
            finally:
                shiki_loop._gh = orig_gh
                shiki_loop._dispatch = orig_dispatch

            # #1: history is [dispatch_repair, ..., merge] ending in goal complete,
            # NOT stop_guardian (the delivered fix turned the check green).
            self.assertEqual(actions[0], "dispatch_repair")
            self.assertIn("merge", actions)
            self.assertEqual(actions[-1], "goal_complete")
            self.assertNotIn("stop_guardian", actions)
            self.assertNotIn("stop_blocked", actions)
            # The merge came after the repair delivered its fix.
            self.assertLess(actions.index("dispatch_repair"), actions.index("merge"))


class DispatchRunnerRepairStatusTests(unittest.TestCase):
    """The status transition the dispatch_repair delivery depends on, driven
    through the REAL ``dispatch_runner_task`` (not the loop's ``_dispatch`` stub):
    a clean repair-runner exit returns the task to ``review`` so the next loop
    cycle re-snapshots the pushed head; a non-zero exit keeps it ``repair-needed``.
    ``true``/``false`` stand in for the runner so the exit code is deterministic."""

    def _dispatch_repair_runner(self, tmp: str, *, tool: str):
        env = RepairEnv(Path(tmp))
        repair_id, _, _ = create_repair_packet(
            env.target, task_id=TASK, pr=IMPL_PR, attempt=1,
            failing_items=["x"], failing_acceptance_criteria=[], minimal_changes=["x"],
            prohibited_changes=[], required_skill="diagnose",
            verification_commands=["true"], evidence_required=["x"], stop_condition="stop",
        )  # also flips task status to repair-needed
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_handoff_repair(argparse.Namespace(target=str(env.target), repair_id=repair_id))
        adapter = RunnerAdapter(
            name="claude-code", display_name="fake runner", required_tool=tool,
            exec_argv=(tool,), auth_status=lambda: {"ready": True}, auth_remediation="",
        )
        args = argparse.Namespace(
            target=str(env.target), task_id=TASK, dry_run=False, force=False, repair_id=repair_id,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            rc = dispatch_runner_task(args, adapter)
        return env, rc

    def test_clean_repair_exit_returns_task_to_review(self):
        with tempfile.TemporaryDirectory() as d:
            env, rc = self._dispatch_repair_runner(d, tool="true")
            self.assertEqual(rc, 0)
            self.assertEqual(load_task(env.target, TASK)["status"], "review")

    def test_failed_repair_exit_keeps_task_repair_needed(self):
        with tempfile.TemporaryDirectory() as d:
            env, rc = self._dispatch_repair_runner(d, tool="false")
            self.assertNotEqual(rc, 0)
            self.assertEqual(load_task(env.target, TASK)["status"], "repair-needed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
