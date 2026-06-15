"""The four auto-capture hooks (proposal 0001 v2 section 3.3).

Each hook calls capture_failure and must never raise into the loop, must store
only structured evidence references (never command-output bodies), and must fail
open (no write) when the goal anchor is missing — never via a sentinel goal_id.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shiki_test_support import synthetic_secrets  # noqa: F401  (path bootstrap + secrets)

from shiki_memory import capture_failure
from shiki_process import ensure_control_dirs
from shiki_runtime import record_runner_result
from shiki_tasks import create_repair_packet

GOAL_ID = "G-20260613T000000000000Z-0000aaaa"
TASK_ID = "T-20260613T000000000000Z-0000cccc"


def _seed(root: Path, *, with_goal: bool = True) -> dict:
    ensure_control_dirs(root)
    if with_goal:
        (root / ".shiki" / "goals" / f"{GOAL_ID}.json").write_text(
            json.dumps({"id": GOAL_ID, "status": "running", "title": "t", "outcome": "o"}),
            encoding="utf-8",
        )
    task = {"id": TASK_ID, "goal_id": GOAL_ID, "status": "running", "risk_level": "low"}
    (root / ".shiki" / "tasks" / f"{TASK_ID}.json").write_text(json.dumps(task), encoding="utf-8")
    return task


def _memories(root: Path) -> list[dict]:
    mem_dir = root / ".shiki" / "memories"
    if not mem_dir.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in mem_dir.glob("MEM-*.json")]


def _all_memory_text(root: Path) -> str:
    mem_dir = root / ".shiki" / "memories"
    if not mem_dir.exists():
        return ""
    return "\n".join(p.read_text(encoding="utf-8") for p in mem_dir.glob("MEM-*.json"))


class RunnerHookTests(unittest.TestCase):
    def test_nonzero_return_captures_runner_fail_with_exec_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _seed(root)
            record_runner_result(root, task, "claude --run", 1, "some output", "some error")
            mems = _memories(root)
            self.assertEqual(len(mems), 1)
            mem = mems[0]
            self.assertEqual(mem["source"]["kind"], "runner_fail")
            self.assertEqual(mem["status"], "raw")
            self.assertEqual([e["kind"] for e in mem["evidence"]], ["exec"])
            self.assertTrue(mem["evidence"][0]["path"].startswith(".shiki/runner/EXEC-"))

    def test_zero_return_captures_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _seed(root)
            record_runner_result(root, task, "claude --run", 0, "ok", "")
            self.assertEqual(_memories(root), [])

    def test_runner_stdout_secret_never_reaches_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _seed(root)
            secret = synthetic_secrets()["github_pat"]
            record_runner_result(root, task, "deploy", 2, f"token {secret} leaked to stdout", secret)
            self.assertEqual(len(_memories(root)), 1)
            self.assertNotIn(secret, _all_memory_text(root))  # body stays in EXEC record, never the memory

    def test_missing_goal_does_not_write_and_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_control_dirs(root)
            task = {"id": TASK_ID, "goal_id": GOAL_ID, "status": "running"}  # goal file absent
            # record_runner_result itself appends a ledger which requires the goal;
            # the capture hook must independently fail open. Seed only the task.
            (root / ".shiki" / "goals" / f"{GOAL_ID}.json").unlink(missing_ok=True)
            try:
                # The capture hook must not raise even if the goal anchor is gone.
                capture_failure(root, source_kind="runner_fail", area="runner",
                                claim="runner failed", goal_id=GOAL_ID, task_id=TASK_ID, evidence_refs=[])
            except Exception as error:  # noqa: BLE001
                self.fail(f"capture hook raised: {error}")
            self.assertEqual(_memories(root), [])


class RepairHookTests(unittest.TestCase):
    def test_repair_packet_captures_referencing_ledger_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            create_repair_packet(
                root, task_id=TASK_ID, pr=7, attempt=1,
                failing_items=["required check failed: Validate Shiki mirror"],
                failing_acceptance_criteria=[], minimal_changes=["fix it"],
                prohibited_changes=[], required_skill="tdd",
                verification_commands=["python3 scripts/validate_shiki.py"],
                evidence_required=[], stop_condition="checks green",
            )
            mems = [m for m in _memories(root) if m["source"]["kind"] == "repair"]
            self.assertEqual(len(mems), 1)
            self.assertEqual([e["kind"] for e in mems[0]["evidence"]], ["ledger"])
            # the failing item text / packet body must not be the evidence body
            self.assertTrue(mems[0]["evidence"][0]["path"].startswith(".shiki/ledger/L-"))


class GoalIdBoundaryTests(unittest.TestCase):
    def test_missing_goal_id_is_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_control_dirs(root)
            result = capture_failure(root, source_kind="loop_stop", area="loop",
                                     claim="stopped", goal_id=None, task_id=None, evidence_refs=[])
            self.assertFalse(result.written)
            self.assertEqual(_memories(root), [])

    def test_sentinel_goal_id_is_rejected(self) -> None:
        # A T1 lesson: capture must never accept a sentinel like "memory" as a
        # goal anchor. Only a real ^G- id that resolves to a goal file is valid.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_control_dirs(root)
            for sentinel in ("memory", "loop", "G-does-not-exist"):
                result = capture_failure(root, source_kind="loop_stop", area="loop",
                                         claim="stopped", goal_id=sentinel, task_id=None, evidence_refs=[])
                self.assertFalse(result.written, f"sentinel {sentinel!r} must not write")
            self.assertEqual(_memories(root), [])


if __name__ == "__main__":
    unittest.main()
