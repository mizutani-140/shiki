"""Verdict-aware repair gating + post-PR evidence sync (T-20260801T085710529159Z-575d4080).

Two defects, driven against the REAL ``scripts/shiki_loop.py`` decision engine and
effectors — no reimplementation of the logic under test:

DEFECT A — verdict-aware repair gating. When the ``CCA verdict`` required check is
red, the loop resolves the verdict's VALUE read-only from durable CI evidence (the
``shiki-cca-evidence`` artifact's ``cca-verdict.json``) and branches on it:
``needs_guardian`` -> stop_guardian, ``blocked`` -> stop_blocked (neither a repair,
neither consumes a repair attempt); ``repair_required`` / ``insufficient_evidence``
-> a bounded repair exactly as before (still increments); ``complete`` against a red
check -> a distinct stop; an unresolvable value -> a distinct fail-closed stop. The
CCA verdict check is NEVER named as a repair item.

DEFECT B — every post-PR loop action that appends to ``ledger_evidence`` (rerun_cca,
dispatch_repair) syncs the branch afterwards, so the PR head carries every ledger it
cites (and the transitive ``.shiki`` evidence those ledgers reference).

The suite is dependency free: standard-library ``unittest`` only.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_loop
from shiki_loop import (
    CCA_VERDICT_CHECK,
    CCA_VERDICT_UNRESOLVED,
    POLICY_GATE,
    _resolve_cca_verdict,
    decide_task_action,
    execute_action,
    goal_loop_step,
    repair_attempts_for,
)
from shiki_process import ensure_control_dirs, read_json, shiki_path, write_json
from shiki_tasks import load_task

TASK = "T-20260801T085710529159Z-575d4080"
GOAL = "G-20260801T085710527889Z-5ec41eba"
FIX_FILE = "verdict-fix.txt"
LOCKS = ["path:.shiki/**", "path:slice-one.txt", f"path:{FIX_FILE}"]

REQUIRED = list(("Validate Shiki mirror", CCA_VERDICT_CHECK, "MergeGate metadata check", POLICY_GATE))


def cca_red(**overrides: str) -> dict[str, str]:
    """All required checks green except the CCA verdict (the DEFECT A trigger)."""
    checks = {name: "pass" for name in REQUIRED}
    checks[CCA_VERDICT_CHECK] = "fail"
    checks.update(overrides)
    return checks


def review_task(*, risk: str = "high", pr: int = 7, reruns: int = 0) -> dict:
    return {
        "id": TASK,
        "goal_id": GOAL,
        "status": "review",
        "risk_level": risk,
        "expected_pr": pr,
        "assigned_runtime": "claude-code",
        "cca_rerun_count": reruns,
    }


def decide(task: dict, *, checks: dict, cca_verdict=None, attempts: int = 0, limit: int = 3, reruns: int = 0) -> dict:
    return decide_task_action(
        task,
        checks=checks,
        pr_state={"merged": False, "head_sha": "deadbeef"},
        repair_attempts=attempts,
        repair_limit=limit,
        required_checks=REQUIRED,
        cca_reruns=reruns,
        cca_verdict=cca_verdict,
    )


# --------------------------------------------------------------------------- #
# DEFECT A — pure engine: every verdict branch through the REAL decide_task_action
# --------------------------------------------------------------------------- #
class VerdictGateEngineTests(unittest.TestCase):
    """Reruns are exhausted (``reruns=2``) so the same-head CCA race is settled and
    the decision reaches the verdict gate — the exact state PR #240 hit."""

    def _decide_sole_cca(self, cca_verdict, *, attempts=0, limit=3):
        # reruns=2 exhausts the same-head CCA race so the decision reaches the gate.
        return decide(review_task(reruns=2), checks=cca_red(), cca_verdict=cca_verdict,
                      attempts=attempts, limit=limit, reruns=2)

    def test_needs_guardian_stops_for_guardian_naming_the_verdict(self):
        d = self._decide_sole_cca("needs_guardian")
        self.assertEqual(d["action"], "stop_guardian")
        self.assertIn("needs_guardian", d["reason"])
        # Never a repair, whatever the failed-check field would have carried.
        self.assertNotIn("failed_checks", d)

    def test_blocked_stops_blocked_naming_the_verdict(self):
        d = self._decide_sole_cca("blocked")
        self.assertEqual(d["action"], "stop_blocked")
        self.assertIn("blocked", d["reason"])
        self.assertNotIn("failed_checks", d)

    def test_repair_required_dispatches_and_strips_the_cca_check(self):
        d = self._decide_sole_cca("repair_required")
        self.assertEqual(d["action"], "dispatch_repair")
        # The CCA verdict check itself is NEVER a repair item.
        self.assertEqual(d["failed_checks"], [])
        self.assertNotIn(CCA_VERDICT_CHECK, d["failed_checks"])
        self.assertEqual(d["cca_verdict"], "repair_required")

    def test_insufficient_evidence_dispatches_a_repair(self):
        d = self._decide_sole_cca("insufficient_evidence")
        self.assertEqual(d["action"], "dispatch_repair")
        self.assertEqual(d["failed_checks"], [])
        self.assertEqual(d["cca_verdict"], "insufficient_evidence")

    def test_complete_against_red_check_stops_with_a_distinct_reason(self):
        d = self._decide_sole_cca("complete")
        self.assertNotEqual(d["action"], "dispatch_repair")
        self.assertIn(d["action"], {"stop_blocked", "stop_guardian"})
        self.assertIn("complete", d["reason"])
        self.assertIn("disagree", d["reason"])

    def test_unresolvable_verdict_never_repairs_and_stops_distinctly(self):
        d = self._decide_sole_cca(CCA_VERDICT_UNRESOLVED)
        self.assertNotEqual(d["action"], "dispatch_repair")
        self.assertIn(d["action"], {"stop_blocked", "stop_guardian"})
        self.assertIn("could not be read", d["reason"])

    def test_each_terminal_verdict_reason_is_distinct(self):
        reasons = {
            v: self._decide_sole_cca(v)["reason"]
            for v in ("needs_guardian", "blocked", "complete", CCA_VERDICT_UNRESOLVED)
        }
        self.assertEqual(len(set(reasons.values())), len(reasons), reasons)

    def test_repair_limit_still_stops_for_guardian_on_a_repairable_verdict(self):
        # At the repair budget, a repair_required verdict still fails over to the
        # Guardian stop (the pre-existing attempt-limit behaviour), not a dispatch.
        d = self._decide_sole_cca("repair_required", attempts=3, limit=3)
        self.assertEqual(d["action"], "stop_guardian")
        self.assertIn("repair attempt limit reached", d["reason"])

    def test_mixed_failure_needs_guardian_still_stops_not_repairs(self):
        # CCA red AND a genuine repairable check red: a needs_guardian verdict is a
        # HITL gate that fails closed over the other failure — never auto-repaired.
        d = decide(review_task(reruns=2), checks=cca_red(**{"Validate Shiki mirror": "fail"}),
                   cca_verdict="needs_guardian", reruns=2)
        self.assertEqual(d["action"], "stop_guardian")
        self.assertIn("needs_guardian", d["reason"])

    def test_mixed_failure_repair_required_repairs_the_other_check_only(self):
        # CCA red + Validate red + repair_required: the repair names the genuine
        # check, and the CCA verdict check is stripped (never a repair item).
        d = decide(review_task(reruns=2), checks=cca_red(**{"Validate Shiki mirror": "fail"}),
                   cca_verdict="repair_required", reruns=2)
        self.assertEqual(d["action"], "dispatch_repair")
        self.assertEqual(d["failed_checks"], ["Validate Shiki mirror"])
        self.assertNotIn(CCA_VERDICT_CHECK, d["failed_checks"])


# --------------------------------------------------------------------------- #
# POLICY_GATE handling is byte-for-byte unchanged — asserted directly.
# --------------------------------------------------------------------------- #
class PolicyGateUnchangedTests(unittest.TestCase):
    LONE_POLICY_REASON = (
        "the MergeGate policy Guardian gate is failing with all other checks green; "
        "a recorded authority must resolve it (never auto-repaired)"
    )

    def _lone_policy_checks(self):
        checks = {name: "pass" for name in REQUIRED}
        checks[POLICY_GATE] = "fail"
        return checks

    def test_lone_policy_gate_failure_reason_is_unchanged(self):
        for verdict in (None, "needs_guardian", "repair_required"):
            d = decide(review_task(risk="high"), checks=self._lone_policy_checks(), cca_verdict=verdict)
            self.assertEqual(d["action"], "stop_guardian")
            # CCA is green here, so the resolved verdict is irrelevant — the policy
            # gate reason is identical whatever value is threaded through.
            self.assertEqual(d["reason"], self.LONE_POLICY_REASON, f"verdict={verdict}")

    def test_policy_gate_never_enters_a_repair_packet(self):
        checks = cca_red(**{POLICY_GATE: "fail", "Validate Shiki mirror": "fail"})
        # None path (pure engine, no resolved verdict): matches the pre-existing
        # engine contract — repair both genuine failures, never the policy gate.
        d = decide(review_task(risk="critical"), checks=checks, cca_verdict=None)
        self.assertEqual(d["action"], "dispatch_repair")
        self.assertNotIn(POLICY_GATE, d["failed_checks"])
        self.assertIn("Validate Shiki mirror", d["failed_checks"])
        self.assertIn(CCA_VERDICT_CHECK, d["failed_checks"])
        # Resolved-verdict path: still strips the policy gate (unchanged), plus the
        # CCA verdict check (DEFECT A) — the genuine failure remains repairable.
        d2 = decide(review_task(risk="critical"), checks=checks, cca_verdict="repair_required")
        self.assertEqual(d2["action"], "dispatch_repair")
        self.assertNotIn(POLICY_GATE, d2["failed_checks"])
        self.assertEqual(d2["failed_checks"], ["Validate Shiki mirror"])


# --------------------------------------------------------------------------- #
# A real git target: repo + bare origin, a single-task goal registered on main,
# and the impl committed AND pushed to origin as the PR branch.
# --------------------------------------------------------------------------- #
def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True)


class LoopEnv:
    def __init__(self, tmp: Path, *, risk: str = "high", reruns: int = 2, status: str = "review"):
        self.pr = 77
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

        ensure_control_dirs(self.target)
        self.branch = f"shiki/{TASK.lower()}-slice"
        self._register(risk=risk, reruns=reruns, status=status)

        _git(self.target, "remote", "set-url", "origin", "https://github.com/o/r.git")
        _git(self.target, "remote", "set-url", "--push", "origin", str(self.remote))

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
            {"task_id": TASK, "goal_id": GOAL, "branch": self.branch, "path": str(self.wt),
             "runtime": "claude-code", "state": "active", "locks": LOCKS},
        )

    def _register(self, *, risk: str, reruns: int, status: str) -> None:
        write_json(shiki_path(self.target, "goals", f"{GOAL}.json"),
                   {"id": GOAL, "status": "planned", "title": "g", "outcome": "o",
                    "risk_level": risk, "ledger_evidence": []})
        write_json(shiki_path(self.target, "dag", f"{GOAL}.json"),
                   {"goal_id": GOAL, "nodes": [TASK], "edges": []})
        write_json(shiki_path(self.target, "tasks", f"{TASK}.json"),
                   {"id": TASK, "goal_id": GOAL, "status": status, "title": "t",
                    "scope": "deliver", "risk_level": risk, "assigned_runtime": "claude-code",
                    "expected_branch": self.branch, "expected_pr": self.pr, "locks": LOCKS,
                    "required_skills": ["tdd"], "acceptance_checks": ["done"],
                    "ledger_evidence": [], "cca_rerun_count": reruns})
        write_json(shiki_path(self.target, "locks", f"{TASK}.json"),
                   {"task_id": TASK, "goal_id": GOAL, "state": "active",
                    "owner": "shiki-run", "locks": LOCKS})
        _git(self.target, "add", "-A")
        _git(self.target, "commit", "-m", "register goal+task")
        _git(self.target, "push")

    def origin_tip(self, branch: str) -> str:
        out = _git(self.remote, "rev-parse", f"refs/heads/{branch}", check=False)
        return out.stdout.strip() if out.returncode == 0 else ""

    def origin_has(self, branch: str, rel: str) -> bool:
        out = _git(self.remote, "cat-file", "-e", f"refs/heads/{branch}:{rel}", check=False)
        return out.returncode == 0

    def seed_evidence_ledger(self, ledger_id: str, exec_name: str) -> None:
        """A check ledger this task owns, whose evidence points at an EXEC record —
        the transitive ``.shiki`` reference DEFECT B must also carry onto the branch."""
        write_json(
            shiki_path(self.target, "ledger", f"{ledger_id}.json"),
            {"id": ledger_id, "type": "check", "goal_id": GOAL, "task_id": TASK,
             "evidence": [f".shiki/runner/{exec_name}.json"]},
        )
        write_json(shiki_path(self.target, "runner", f"{exec_name}.json"), {"id": exec_name})
        task = load_task(self.target, TASK)
        task.setdefault("ledger_evidence", []).append(ledger_id)
        write_json(shiki_path(self.target, "tasks", f"{TASK}.json"), task)


class FakeGh:
    """A ``shiki_loop._gh`` stand-in serving the read-only PR + CI-evidence surfaces
    the loop reads: PR view/checks, the CCA workflow run list, and — the DEFECT A
    seam — the ``shiki-cca-evidence`` artifact download that carries the verdict."""

    def __init__(self, env: LoopEnv, *, checks: dict, verdict=None,
                 serve_run: bool = True, serve_download: bool = True, malformed: bool = False):
        self.env = env
        self.checks = checks
        self.verdict = verdict
        self.serve_run = serve_run
        self.serve_download = serve_download
        self.malformed = malformed
        self.calls: list[list[str]] = []

    def __call__(self, target, args, check=True):
        args = list(args)
        self.calls.append(args)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        head = args[:2]
        sha = self.env.origin_tip(self.env.branch)
        if head == ["pr", "view"]:
            r.stdout = json.dumps({"state": "OPEN", "mergedAt": None, "headRefOid": sha})
        elif head == ["pr", "checks"]:
            rows = [{"name": n, "bucket": b, "startedAt": "2026-08-01T00:00:01Z"} for n, b in self.checks.items()]
            r.stdout = json.dumps(rows)
        elif head == ["run", "list"]:
            if self.serve_run:
                r.stdout = json.dumps([{"databaseId": 555, "conclusion": "failure", "headSha": sha, "status": "completed"}])
            else:
                r.stdout = "[]"
        elif head == ["run", "download"]:
            target_dir = None
            for i, token in enumerate(args):
                if token == "--dir" and i + 1 < len(args):
                    target_dir = args[i + 1]
            if not (self.serve_download and target_dir):
                r.returncode = 1
                return r
            out = Path(target_dir)
            out.mkdir(parents=True, exist_ok=True)
            if self.malformed:
                (out / "cca-verdict.json").write_text("{not json", encoding="utf-8")
            elif self.verdict is not None:
                (out / "cca-verdict.json").write_text(
                    json.dumps({"verdict": self.verdict, "pr": self.env.pr, "head_sha": sha,
                                "task_id": TASK, "goal_id": GOAL, "summary": "x"}),
                    encoding="utf-8",
                )
            else:
                r.returncode = 1
        elif head == ["run", "rerun"]:
            pass
        return r


def _fake_repair_dispatch(target, task, *, repair_id=None):
    """Stand in for the repair runner: write the fix into the worktree and return
    the task to ``review`` (runner exited 0). It never commits/pushes — the loop
    owns delivery."""
    from shiki_tasks import worktree_record

    record = worktree_record(target, task["id"])
    (Path(record["path"]) / FIX_FILE).write_text("fix\n")
    live = load_task(target, task["id"])
    live["status"] = "review"
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), live)
    return 0


# --------------------------------------------------------------------------- #
# DEFECT A — real resolution + real effectors, driven end to end.
# --------------------------------------------------------------------------- #
class VerdictResolutionTests(unittest.TestCase):
    """``_resolve_cca_verdict`` reads the verdict from the artifact and fails closed
    on any inability to read it — read-only, never raising into the loop."""

    def _resolve(self, **fake_kwargs):
        with tempfile.TemporaryDirectory() as d:
            env = LoopEnv(Path(d))
            fake = FakeGh(env, checks=cca_red(), **fake_kwargs)
            orig = shiki_loop._gh
            shiki_loop._gh = fake
            try:
                pr_state = {"merged": False, "head_sha": env.origin_tip(env.branch)}
                return _resolve_cca_verdict(env.target, load_task(env.target, TASK), pr_state)
            finally:
                shiki_loop._gh = orig

    def test_reads_the_verdict_from_the_artifact(self):
        for verdict in ("needs_guardian", "blocked", "repair_required", "insufficient_evidence", "complete"):
            self.assertEqual(self._resolve(verdict=verdict), verdict)

    def test_no_cca_run_is_unresolvable(self):
        self.assertEqual(self._resolve(verdict="needs_guardian", serve_run=False), CCA_VERDICT_UNRESOLVED)

    def test_missing_artifact_is_unresolvable(self):
        self.assertEqual(self._resolve(verdict="needs_guardian", serve_download=False), CCA_VERDICT_UNRESOLVED)

    def test_malformed_verdict_is_unresolvable(self):
        self.assertEqual(self._resolve(verdict="needs_guardian", malformed=True), CCA_VERDICT_UNRESOLVED)

    def test_unknown_verdict_string_is_unresolvable(self):
        self.assertEqual(self._resolve(verdict="approved_by_me"), CCA_VERDICT_UNRESOLVED)

    def test_gh_raising_is_unresolvable_never_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            env = LoopEnv(Path(d))

            def boom(target, args, check=True):
                raise OSError("network down")

            orig = shiki_loop._gh
            shiki_loop._gh = boom
            try:
                got = _resolve_cca_verdict(env.target, load_task(env.target, TASK), {"head_sha": "x"})
            finally:
                shiki_loop._gh = orig
            self.assertEqual(got, CCA_VERDICT_UNRESOLVED)


class VerdictGatingEffectorTests(unittest.TestCase):
    """``goal_loop_step`` resolves the verdict and the real effectors act on it."""

    def _run(self, verdict, *, serve_run=True, serve_download=True):
        with tempfile.TemporaryDirectory() as d:
            env = LoopEnv(Path(d))
            fake = FakeGh(env, checks=cca_red(), verdict=verdict, serve_run=serve_run, serve_download=serve_download)
            orig_gh = shiki_loop._gh
            orig_dispatch = shiki_loop._dispatch
            shiki_loop._gh = fake
            shiki_loop._dispatch = _fake_repair_dispatch
            try:
                before = repair_attempts_for(env.target, TASK)
                result = goal_loop_step(env.target, GOAL)
                after = repair_attempts_for(env.target, TASK)
                packets = sorted((env.target / ".shiki" / "repairs").glob("RP-*.json"))
                packet = read_json(packets[0]) if packets else None
                return result, before, after, packet
            finally:
                shiki_loop._gh = orig_gh
                shiki_loop._dispatch = orig_dispatch

    def test_needs_guardian_stops_and_leaves_the_counter_unchanged(self):
        result, before, after, packet = self._run("needs_guardian")
        self.assertEqual(result["action"], "stop_guardian")
        self.assertIn("needs_guardian", result["reason"])
        self.assertEqual(before, after)  # no repair attempt consumed
        self.assertIsNone(packet)

    def test_blocked_stops_and_leaves_the_counter_unchanged(self):
        result, before, after, packet = self._run("blocked")
        self.assertEqual(result["action"], "stop_blocked")
        self.assertIn("blocked", result["reason"])
        self.assertEqual(before, after)
        self.assertIsNone(packet)

    def test_repair_required_dispatches_increments_and_never_names_the_cca_check(self):
        result, before, after, packet = self._run("repair_required")
        self.assertEqual(result["action"], "dispatch_repair")
        self.assertEqual(after, before + 1)  # the attempt WAS consumed
        self.assertIsNotNone(packet)
        # The emitted packet never names the CCA verdict check as its only item.
        self.assertNotEqual(packet["failing_checklist_items"], [f"required check failed: {CCA_VERDICT_CHECK}"])
        self.assertNotIn(f"required check failed: {CCA_VERDICT_CHECK}", packet["failing_checklist_items"])
        self.assertTrue(any("repair_required" in item for item in packet["failing_checklist_items"]), packet)

    def test_insufficient_evidence_dispatches_a_repair(self):
        result, before, after, packet = self._run("insufficient_evidence")
        self.assertEqual(result["action"], "dispatch_repair")
        self.assertEqual(after, before + 1)
        self.assertIsNotNone(packet)

    def test_complete_against_red_check_stops_without_repairing(self):
        result, before, after, packet = self._run("complete")
        self.assertIn(result["action"], {"stop_blocked", "stop_guardian"})
        self.assertNotEqual(result["action"], "dispatch_repair")
        self.assertEqual(before, after)
        self.assertIsNone(packet)

    def test_unresolvable_verdict_stops_and_never_repairs(self):
        result, before, after, packet = self._run("needs_guardian", serve_run=False)
        self.assertIn(result["action"], {"stop_blocked", "stop_guardian"})
        self.assertNotEqual(result["action"], "dispatch_repair")
        self.assertIn("could not be read", result["reason"])
        self.assertEqual(before, after)
        self.assertIsNone(packet)


# --------------------------------------------------------------------------- #
# DEFECT B — post-PR loop actions carry the evidence they cite onto the branch.
# --------------------------------------------------------------------------- #
class PostPrEvidenceSyncTests(unittest.TestCase):
    def _assert_ledger_evidence_on_branch(self, env: LoopEnv) -> None:
        task = load_task(env.target, TASK)
        ledger_ids = task.get("ledger_evidence") or []
        self.assertTrue(ledger_ids, "expected the action to append ledger evidence")
        for ledger_id in ledger_ids:
            self.assertTrue(
                env.origin_has(env.branch, f".shiki/ledger/{ledger_id}.json"),
                f"ledger {ledger_id} does not resolve on the task branch",
            )
            entry = read_json(env.target / ".shiki" / "ledger" / f"{ledger_id}.json")
            for ref in entry.get("evidence") or []:
                if isinstance(ref, str) and ref.startswith(".shiki/"):
                    self.assertTrue(
                        env.origin_has(env.branch, ref),
                        f"transitive evidence {ref} cited by {ledger_id} does not resolve on the branch",
                    )

    def test_rerun_cca_carries_its_ledger_and_transitive_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            env = LoopEnv(Path(d), reruns=0)
            env.seed_evidence_ledger("L-SEED-TDD", "EXEC-SEED")
            fake = FakeGh(env, checks=cca_red())
            orig_gh = shiki_loop._gh
            shiki_loop._gh = fake
            try:
                result = execute_action(env.target, GOAL, {"action": "rerun_cca", "task_id": TASK}, repair_limit=3)
            finally:
                shiki_loop._gh = orig_gh
            self.assertIn("pushed to the task branch", result.get("state_sync", ""))
            # The seeded TDD ledger + its EXEC AND the fresh rerun ledger all ride.
            self.assertTrue(env.origin_has(env.branch, ".shiki/runner/EXEC-SEED.json"))
            self._assert_ledger_evidence_on_branch(env)

    def test_dispatch_repair_carries_its_ledger_and_transitive_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            env = LoopEnv(Path(d))
            env.seed_evidence_ledger("L-SEED-TDD", "EXEC-SEED")
            fake = FakeGh(env, checks=cca_red())
            orig_gh = shiki_loop._gh
            orig_dispatch = shiki_loop._dispatch
            shiki_loop._gh = fake
            shiki_loop._dispatch = _fake_repair_dispatch
            try:
                result = execute_action(
                    env.target, GOAL,
                    {"action": "dispatch_repair", "task_id": TASK,
                     "failed_checks": ["Validate Shiki mirror"]},
                    repair_limit=3,
                )
            finally:
                shiki_loop._gh = orig_gh
                shiki_loop._dispatch = orig_dispatch
            self.assertEqual(result["action"], "dispatch_repair")
            self.assertIn("pushed to the task branch", result.get("state_sync", ""))
            # The repair ledger cites the repair packet (.shiki/repairs/RP-*.json);
            # both it and the seeded TDD evidence resolve on the branch.
            self.assertTrue(env.origin_has(env.branch, ".shiki/runner/EXEC-SEED.json"))
            self._assert_ledger_evidence_on_branch(env)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
