"""goal_reconcile metadata mode (platform-fix for the multi-task goal deadlock).

A goal_reconcile PR may register planned task files that exist in the goal's
spec-frozen plan and restore the goal's DAG — deny by default for everything
else. It is a narrow, frozen-plan-bound registration mode, never a general
multi-file scope bypass.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from mergegate_check import ChangedFile, enforce_goal_reconcile

GOAL = "G-20260613T000000000000Z-0000aaaa"
PLAN = "P-20260613T000000000000Z-0000pppp"
T_A = "T-20260613T000000000000Z-0000a001"
T_B = "T-20260613T000000000000Z-0000b002"
LEDGER = "L-20260613T000000000000Z-0000l003"
TITLE_A = "Capture hooks and scorecard"
TITLE_B = "Consult injection and docs"


def _seed(root: Path, *, frozen: bool = True) -> None:
    for d in ("goals", "plans", "tasks", "dag", "ledger"):
        (root / ".shiki" / d).mkdir(parents=True, exist_ok=True)
    (root / ".shiki" / "goals" / f"{GOAL}.json").write_text(
        json.dumps({"id": GOAL, "status": "planned", "source_plan": PLAN, "title": "g"}), encoding="utf-8")
    (root / ".shiki" / "plans" / f"{PLAN}.json").write_text(
        json.dumps({"id": PLAN, "spec_freeze": {"status": "frozen" if frozen else "draft"},
                    "tasks": [{"title": TITLE_A, "scope": "x", "acceptance_checks": ["x"]},
                              {"title": TITLE_B, "scope": "x", "acceptance_checks": ["x"]}]}),
        encoding="utf-8")


def _write_task(root: Path, tid: str, *, title: str, status: str = "planned", goal: str = GOAL) -> None:
    (root / ".shiki" / "tasks" / f"{tid}.json").write_text(
        json.dumps({"id": tid, "goal_id": goal, "title": title, "status": status, "risk_level": "high"}),
        encoding="utf-8")


def _write_ledger(root: Path, lid: str, *, goal: str = GOAL) -> None:
    (root / ".shiki" / "ledger" / f"{lid}.json").write_text(
        json.dumps({"id": lid, "goal_id": goal, "task_id": None, "type": "check", "actor": "x",
                    "timestamp": "2026-06-13T00:00:00+00:00", "summary": "reconcile", "evidence": []}),
        encoding="utf-8")


def _run(root: Path, changes: list[ChangedFile], *, goal: str = GOAL) -> list[str]:
    blocking: list[str] = []
    enforce_goal_reconcile(target=root, goal_id=goal, changed_files_status=changes,
                           blocking=blocking, warnings=[])
    return blocking


class GoalReconcileTests(unittest.TestCase):
    def test_valid_planned_registration_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title=TITLE_A)
            _write_task(root, T_B, title=TITLE_B)
            _write_ledger(root, LEDGER)
            (root / ".shiki" / "dag" / f"{GOAL}.json").write_text(
                json.dumps({"goal_id": GOAL, "nodes": [T_A, T_B], "edges": []}), encoding="utf-8")
            blocking = _run(root, [
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/tasks/{T_B}.json"),
                ChangedFile("A", f".shiki/dag/{GOAL}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertEqual(blocking, [])

    def test_code_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title=TITLE_A)
            _write_ledger(root, LEDGER)
            blocking = _run(root, [
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
                ChangedFile("M", "scripts/shiki_loop.py"),
            ])
        self.assertTrue(any("non-Shiki" in b for b in blocking))

    def test_task_not_in_frozen_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title="Some unfrozen task")
            _write_ledger(root, LEDGER)
            blocking = _run(root, [
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("not in the goal's frozen plan" in b for b in blocking))

    def test_done_status_registration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title=TITLE_A, status="done")
            _write_ledger(root, LEDGER)
            blocking = _run(root, [
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("status=planned" in b for b in blocking))

    def test_modifying_existing_task_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title=TITLE_A)
            _write_ledger(root, LEDGER)
            blocking = _run(root, [
                ChangedFile("M", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("only ADD planned task files" in b for b in blocking))

    def test_forbidden_surfaces_are_rejected(self) -> None:
        for forbidden in (".shiki/memories/MEM-20260613T000000000000Z-0000m001.json",
                          ".shiki/guardian-policy.json",
                          ".shiki/reports/R-20260613T000000000000Z-0000r001.json",
                          f".shiki/locks/{T_A}.json"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _seed(root)
                _write_ledger(root, LEDGER)
                blocking = _run(root, [
                    ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
                    ChangedFile("A", forbidden),
                ])
            self.assertTrue(any("must not change" in b for b in blocking), f"{forbidden} not rejected")

    def test_dag_node_outside_frozen_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title="Injected task")  # title not frozen
            _write_ledger(root, LEDGER)
            (root / ".shiki" / "dag" / f"{GOAL}.json").write_text(
                json.dumps({"goal_id": GOAL, "nodes": [T_A], "edges": []}), encoding="utf-8")
            blocking = _run(root, [
                ChangedFile("A", f".shiki/dag/{GOAL}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("DAG node" in b and "frozen plan" in b for b in blocking))

    def test_missing_reconcile_ledger_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title=TITLE_A)
            blocking = _run(root, [ChangedFile("A", f".shiki/tasks/{T_A}.json")])
        self.assertTrue(any("reconcile ledger" in b for b in blocking))

    def test_unfrozen_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root, frozen=False)
            _write_task(root, T_A, title=TITLE_A)
            _write_ledger(root, LEDGER)
            blocking = _run(root, [
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("not spec-frozen" in b for b in blocking))

    def test_goal_complete_in_reconcile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            (root / ".shiki" / "goals" / f"{GOAL}.json").write_text(
                json.dumps({"id": GOAL, "status": "complete", "source_plan": PLAN, "title": "g"}), encoding="utf-8")
            _write_ledger(root, LEDGER)
            blocking = _run(root, [
                ChangedFile("M", f".shiki/goals/{GOAL}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("must not mark the goal complete" in b for b in blocking))


if __name__ == "__main__":
    unittest.main()
