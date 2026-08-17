"""Goal-completion scorecard and distillation suggestions (proposal 0001 v2 3.6).

The scorecard is computed ONLY from ledger and task state — raw memories are
never a counting source (avoids the failure->memory->scorecard cycle). It lives
inside the report file (never stdout, to keep json_get_last compatibility).
Distillation suggestions are advisory: they never change memory status or create
a distilled rule.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_memory import compute_scorecard, distillation_suggestions, capture_failure
from shiki_process import ensure_control_dirs

GOAL_ID = "G-20260613T000000000000Z-0000aaaa"


def _seed_goal(root: Path, *, status: str = "running") -> None:
    ensure_control_dirs(root)
    (root / ".shiki" / "goals" / f"{GOAL_ID}.json").write_text(
        json.dumps({"id": GOAL_ID, "status": status, "title": "t", "outcome": "o"}), encoding="utf-8")


def _ledger(root: Path, lid: str, ltype: str, *, ts: str = "2026-06-13T00:00:00+00:00") -> None:
    (root / ".shiki" / "ledger" / f"{lid}.json").write_text(
        json.dumps({"id": lid, "goal_id": GOAL_ID, "task_id": None, "type": ltype,
                    "actor": "x", "timestamp": ts, "summary": "s", "evidence": []}),
        encoding="utf-8")


def _task(root: Path, tid: str, *, status: str, cca_reruns: int = 0) -> dict:
    t = {"id": tid, "goal_id": GOAL_ID, "status": status, "risk_level": "low", "cca_rerun_count": cca_reruns}
    (root / ".shiki" / "tasks" / f"{tid}.json").write_text(json.dumps(t), encoding="utf-8")
    return t


class ScorecardTests(unittest.TestCase):
    def test_counts_from_ledger_and_task_state_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_goal(root)
            _ledger(root, "L-20260613T000000000000Z-00000001", "repair", ts="2026-06-13T00:00:00+00:00")
            _ledger(root, "L-20260613T000000000000Z-00000002", "repair", ts="2026-06-13T00:05:00+00:00")
            _ledger(root, "L-20260613T000000000000Z-00000003", "lock")
            tasks = [
                _task(root, "T-20260613T000000000000Z-0000c001", status="done", cca_reruns=2),
                _task(root, "T-20260613T000000000000Z-0000c002", status="repair-needed", cca_reruns=1),
            ]
            sc = compute_scorecard(root, GOAL_ID, tasks=tasks)
        self.assertEqual(sc["repairs"]["total"], 2)
        self.assertEqual(sc["lock_amendments"]["total"], 1)
        self.assertEqual(sc["cca_reruns"]["total"], 3)
        self.assertEqual(sc["tasks"], {"total": 2, "completed": 1, "failed": 1})
        self.assertGreaterEqual(sc["duration_ms"], 300000)  # 5 minutes between ledger events

    def test_raw_memory_is_not_a_counting_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_goal(root)
            # A captured raw memory writes a memory-transition ledger; it must NOT
            # be counted as a repair/loop_stop in the scorecard.
            capture_failure(root, source_kind="repair", area="loop", claim="repair captured",
                            goal_id=GOAL_ID, task_id=None, evidence_refs=[])
            sc = compute_scorecard(root, GOAL_ID, tasks=[])
        self.assertEqual(sc["repairs"]["total"], 0)  # memory-transition ledger is not a repair ledger
        self.assertEqual(sc["loop_stops"]["total"], 0)

    def test_missing_categories_are_zero_with_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_goal(root)
            sc = compute_scorecard(root, GOAL_ID, tasks=[])
        self.assertEqual(sc["repairs"]["total"], 0)
        self.assertEqual(sc["loop_stops"]["total"], 0)
        self.assertEqual(sc["cca_reruns"]["total"], 0)
        self.assertTrue(any("loop_stops" in w for w in sc["warnings"]))


class SuggestionTests(unittest.TestCase):
    def test_suggestion_from_recurring_raw_memories_includes_mem_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_goal(root)
            ids = []
            for _ in range(2):
                r = capture_failure(root, source_kind="runner_fail", area="runner",
                                    claim="runner failed again", goal_id=GOAL_ID, task_id=None, evidence_refs=[])
                ids.append(r.memory_id)
            suggestions = distillation_suggestions(root, GOAL_ID)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(sorted(suggestions[0]["from_memories"]), sorted(ids))
        self.assertIn("distill", suggestions[0]["note"])

    def test_single_memory_does_not_yield_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_goal(root)
            capture_failure(root, source_kind="runner_fail", area="runner",
                            claim="one off failure", goal_id=GOAL_ID, task_id=None, evidence_refs=[])
            self.assertEqual(distillation_suggestions(root, GOAL_ID), [])

    def test_suggestion_does_not_change_memory_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_goal(root)
            for _ in range(2):
                capture_failure(root, source_kind="cca_fail", area="cca",
                                claim="cca failed twice", goal_id=GOAL_ID, task_id=None, evidence_refs=[])
            distillation_suggestions(root, GOAL_ID)
            for path in (root / ".shiki" / "memories").glob("MEM-*.json"):
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "raw")


class GoalCompleteContractTests(unittest.TestCase):
    def test_scorecard_in_report_file_and_not_on_stdout(self) -> None:
        import argparse
        import subprocess
        from shiki_tasks import cmd_goal_complete
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_goal(root)
            _task(root, "T-20260613T000000000000Z-0000c001", status="done")
            # cmd_goal_complete requires a github-first target.
            subprocess.run(["git", "init", "-b", "main"], cwd=root, capture_output=True, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/example/x.git"],
                           cwd=root, capture_output=True, check=True)
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_goal_complete(argparse.Namespace(target=str(root), goal_id=GOAL_ID, summary=None))
            stdout = buf.getvalue()
            # stdout must be exactly one JSON object (json_get_last compatible) with no scorecard.
            parsed = json.loads(stdout)
            self.assertNotIn("scorecard", parsed)
            report = json.loads(Path(parsed["report_file"]).read_text(encoding="utf-8"))
            self.assertIn("scorecard", report)
            self.assertEqual(report["scorecard"]["goal_id"], GOAL_ID)

    def test_report_uses_strongest_goal_or_task_risk(self) -> None:
        import argparse
        import subprocess
        from shiki_tasks import cmd_goal_complete

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_goal(root)
            goal_path = root / ".shiki" / "goals" / f"{GOAL_ID}.json"
            goal = json.loads(goal_path.read_text(encoding="utf-8"))
            goal["risk_level"] = "medium"
            goal_path.write_text(json.dumps(goal), encoding="utf-8")
            task_id = "T-20260613T000000000000Z-0000c002"
            task = _task(root, task_id, status="done")
            task["risk_level"] = "critical"
            (root / ".shiki" / "tasks" / f"{task_id}.json").write_text(
                json.dumps(task), encoding="utf-8"
            )
            subprocess.run(["git", "init", "-b", "main"], cwd=root, capture_output=True, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/example/x.git"],
                cwd=root,
                capture_output=True,
                check=True,
            )
            output = io.StringIO()
            with redirect_stdout(output):
                cmd_goal_complete(argparse.Namespace(target=str(root), goal_id=GOAL_ID, summary=None))
            report_path = Path(json.loads(output.getvalue())["report_file"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["mergegate"]["risk"], "critical")


if __name__ == "__main__":
    unittest.main()
