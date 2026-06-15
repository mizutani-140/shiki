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

from mergegate_check import (
    ChangedFile,
    enforce_goal_reconcile,
    enforce_post_merge_reconcile,
    goal_reconcile_decision,
    post_merge_reconcile_decision,
)

GOAL = "G-20260613T000000000000Z-0000aaaa"
PLAN = "P-20260613T000000000000Z-0000pppp"
T_A = "T-20260613T000000000000Z-0000a001"
T_B = "T-20260613T000000000000Z-0000b002"
LEDGER = "L-20260613T000000000000Z-0000l003"
TITLE_A = "Capture hooks and scorecard"
TITLE_B = "Consult injection and docs"


# The frozen plan task definitions; a registered task must match these exactly.
FROZEN_A = {"title": TITLE_A, "scope": "scope-a", "risk_level": "high", "acceptance_checks": ["a"]}
FROZEN_B = {"title": TITLE_B, "scope": "scope-b", "risk_level": "high", "acceptance_checks": ["b"]}


def _seed(root: Path, *, frozen: bool = True) -> None:
    for d in ("goals", "plans", "tasks", "dag", "ledger"):
        (root / ".shiki" / d).mkdir(parents=True, exist_ok=True)
    (root / ".shiki" / "goals" / f"{GOAL}.json").write_text(
        json.dumps({"id": GOAL, "status": "planned", "source_plan": PLAN, "title": "g"}), encoding="utf-8")
    (root / ".shiki" / "plans" / f"{PLAN}.json").write_text(
        json.dumps({"id": PLAN, "spec_freeze": {"status": "frozen" if frozen else "draft"},
                    "tasks": [FROZEN_A, FROZEN_B]}),
        encoding="utf-8")


def _write_task(root: Path, tid: str, *, title: str, status: str = "planned", goal: str = GOAL, **overrides) -> None:
    # By default register a task that MATCHES the frozen definition for its title.
    frozen = {TITLE_A: FROZEN_A, TITLE_B: FROZEN_B}.get(title, {})
    task = {"id": tid, "goal_id": goal, "title": title, "status": status,
            "scope": frozen.get("scope", "scope-x"), "risk_level": frozen.get("risk_level", "high"),
            "acceptance_checks": frozen.get("acceptance_checks", ["x"])}
    task.update(overrides)
    (root / ".shiki" / "tasks" / f"{tid}.json").write_text(json.dumps(task), encoding="utf-8")


def _write_ledger(root: Path, lid: str, *, goal: str = GOAL, ltype: str = "task-registered") -> None:
    (root / ".shiki" / "ledger" / f"{lid}.json").write_text(
        json.dumps({"id": lid, "goal_id": goal, "task_id": None, "type": ltype, "actor": "x",
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

    def test_goal_file_modification_is_rejected(self) -> None:
        # The goal file (and its source_plan binding) must be immutable under
        # reconcile: a repoint to a different frozen plan would widen the set the
        # reconcile is authorized against.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            (root / ".shiki" / "goals" / f"{GOAL}.json").write_text(
                json.dumps({"id": GOAL, "status": "planned", "source_plan": "P-other", "title": "g"}), encoding="utf-8")
            _write_ledger(root, LEDGER)
            blocking = _run(root, [
                ChangedFile("M", f".shiki/goals/{GOAL}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        # goal load now fails (source_plan repointed to a missing plan) and/or the
        # goal-file change is denied; either way it must block.
        self.assertTrue(blocking)
        self.assertTrue(any("must not change" in b or "source_plan" in b for b in blocking))

    def test_cross_goal_dag_node_is_rejected(self) -> None:
        # A task belonging to another goal but sharing a frozen title must not be
        # wired into this goal's DAG.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            foreign = "T-20260613T000000000000Z-0000f009"
            _write_task(root, foreign, title=TITLE_A, goal="G-20260613T000000000000Z-0000zzzz")
            _write_ledger(root, LEDGER)
            (root / ".shiki" / "dag" / f"{GOAL}.json").write_text(
                json.dumps({"goal_id": GOAL, "nodes": [foreign], "edges": []}), encoding="utf-8")
            blocking = _run(root, [
                ChangedFile("A", f".shiki/dag/{GOAL}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("not anchored to goal" in b for b in blocking))

    def test_wrong_ledger_type_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title=TITLE_A)
            _write_ledger(root, LEDGER, ltype="check")  # not a task-registered event
            blocking = _run(root, [
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("reconcile ledger" in b for b in blocking))

    def test_frozen_definition_mismatch_is_rejected(self) -> None:
        # A task whose title is frozen but whose risk_level/acceptance_checks
        # differ from the frozen plan definition must be rejected (binding is to
        # the frozen task definition, not just the title).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title=TITLE_A, risk_level="low", acceptance_checks=["weakened"])
            _write_ledger(root, LEDGER)
            blocking = _run(root, [
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("risk_level" in b and "frozen plan definition" in b for b in blocking))
        self.assertTrue(any("acceptance_checks" in b and "frozen plan definition" in b for b in blocking))

    def test_duplicate_frozen_title_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_task(root, T_A, title=TITLE_A)
            _write_task(root, T_B, title=TITLE_A)  # same frozen title twice
            _write_ledger(root, LEDGER)
            blocking = _run(root, [
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/tasks/{T_B}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("already covered" in b for b in blocking))


class ReconcileDecisionTests(unittest.TestCase):
    MARKER = "<!-- shiki:goal_reconcile -->"
    LABEL = {"name": "mergegate:goal_reconcile"}

    def test_marker_and_label_enables_mode(self) -> None:
        mode, err = goal_reconcile_decision({"body": f"register tasks {self.MARKER}", "labels": [self.LABEL]})
        self.assertTrue(mode)
        self.assertIsNone(err)

    def test_marker_without_label_fails_closed(self) -> None:
        mode, err = goal_reconcile_decision({"body": f"register {self.MARKER}", "labels": []})
        self.assertFalse(mode)
        self.assertIsNotNone(err)
        self.assertIn("label", err)

    def test_label_without_marker_is_not_reconcile(self) -> None:
        mode, err = goal_reconcile_decision({"body": "normal PR", "labels": [self.LABEL]})
        self.assertFalse(mode)
        self.assertIsNone(err)

    def test_plain_pr_is_not_reconcile(self) -> None:
        mode, err = goal_reconcile_decision({"body": "T-0001 normal work", "labels": []})
        self.assertFalse(mode)
        self.assertIsNone(err)


PM_TASK = "T-20260613T000000000000Z-0000ab01"
PM_LOCK = "L-20260613T000000000000Z-0000abc1"


def _pm_seed(root: Path, *, base_status: str = "review", expected_pr: int = 99) -> None:
    """Seed a merged-task residue: base task (review, expected_pr) + its lock."""
    for d in ("tasks", "locks", "ledger"):
        (root / ".shiki" / d).mkdir(parents=True, exist_ok=True)
    (root / ".shiki" / "base" / "tasks").mkdir(parents=True, exist_ok=True)
    base_task = {"id": PM_TASK, "goal_id": GOAL, "title": "t", "status": base_status,
                 "risk_level": "high", "expected_pr": expected_pr, "locks": ["path:scripts/x.py"],
                 "acceptance_checks": ["a"]}
    (root / ".shiki" / "base" / "tasks" / f"{PM_TASK}.json").write_text(json.dumps(base_task), encoding="utf-8")
    (root / ".shiki" / "locks" / f"{PM_TASK}.json").write_text(
        json.dumps({"task_id": PM_TASK, "locks": ["path:scripts/x.py"], "state": "released"}), encoding="utf-8")
    (root / ".shiki" / "ledger" / f"{PM_LOCK}.json").write_text(
        json.dumps({"id": PM_LOCK, "goal_id": GOAL, "task_id": PM_TASK, "type": "lock", "actor": "x",
                    "timestamp": "2026-06-13T00:00:00+00:00", "summary": "lock released post-merge", "evidence": []}),
        encoding="utf-8")
    return base_task


def _pm_write_head_task(root: Path, base_task: dict, **changes) -> None:
    head = dict(base_task)
    head.update(changes)
    (root / ".shiki" / "tasks" / f"{PM_TASK}.json").write_text(json.dumps(head), encoding="utf-8")


def _pm_run(root: Path, changes: list[ChangedFile], *, merged=frozenset({99})) -> list[str]:
    blocking: list[str] = []
    enforce_post_merge_reconcile(target=root, task_id=PM_TASK, base_shiki=root / ".shiki" / "base",
                                 changed_files_status=changes, blocking=blocking, warnings=[],
                                 merged_pr_numbers=set(merged))
    return blocking


class PostMergeReconcileTests(unittest.TestCase):
    def test_lock_release_and_expected_pr_clear_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, expected_pr=None)  # cleared
            blocking = _pm_run(root, [
                ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                ChangedFile("M", f".shiki/locks/{PM_TASK}.json"),
                ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json"),
            ])
        self.assertEqual(blocking, [])

    def test_status_done_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done", expected_pr=None)
            blocking = _pm_run(root, [
                ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                ChangedFile("D", f".shiki/locks/{PM_TASK}.json"),
                ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json"),
            ])
        self.assertEqual(blocking, [])

    def test_changing_other_task_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done", acceptance_checks=["a", "sneaky"])
            blocking = _pm_run(root, [
                ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json"),
            ])
        self.assertTrue(any("must not change task field 'acceptance_checks'" in b for b in blocking))

    def test_code_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done")
            blocking = _pm_run(root, [
                ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json"),
                ChangedFile("M", "scripts/mergegate_check.py"),
            ])
        self.assertTrue(any("non-Shiki" in b for b in blocking))

    def test_lock_not_released_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done")
            (root / ".shiki" / "locks" / f"{PM_TASK}.json").write_text(
                json.dumps({"task_id": PM_TASK, "locks": ["path:scripts/x.py"], "state": "active"}), encoding="utf-8")
            blocking = _pm_run(root, [
                ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                ChangedFile("M", f".shiki/locks/{PM_TASK}.json"),
                ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json"),
            ])
        self.assertTrue(any("must set state=released" in b for b in blocking))

    def test_missing_reconcile_ledger_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done")
            blocking = _pm_run(root, [ChangedFile("D", f".shiki/locks/{PM_TASK}.json")])
        self.assertTrue(any("reconcile ledger" in b for b in blocking))

    def test_unrelated_and_authority_changes_are_rejected(self) -> None:
        for forbidden in (".shiki/locks/T-20260613T000000000000Z-0000other.json",
                          ".shiki/guardian-policy.json",
                          ".shiki/memories/MEM-20260613T000000000000Z-0000m001.json"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                base = _pm_seed(root)
                _pm_write_head_task(root, base, status="done")
                blocking = _pm_run(root, [
                    ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json"),
                    ChangedFile("A", forbidden),
                ])
            self.assertTrue(any("must not change" in b for b in blocking), f"{forbidden} not rejected")

    def test_no_expected_pr_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root, expected_pr=0)
            (root / ".shiki" / "tasks" / f"{PM_TASK}.json").write_text(
                json.dumps({**base, "expected_pr": None}), encoding="utf-8")
            blocking = _pm_run(root, [ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json")])
        self.assertTrue(any("no merged PR" in b for b in blocking))

    def test_missing_base_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done")
            blocking: list[str] = []
            enforce_post_merge_reconcile(target=root, task_id=PM_TASK, base_shiki=None,
                                         changed_files_status=[], blocking=blocking, warnings=[])
        self.assertTrue(any("requires a base" in b for b in blocking))

    def test_expected_pr_not_cleared_is_rejected(self) -> None:
        # The residue MUST be cleaned up: leaving expected_pr set is rejected.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done")  # expected_pr left at 99
            blocking = _pm_run(root, [
                ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                ChangedFile("D", f".shiki/locks/{PM_TASK}.json"),
                ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json"),
            ])
        self.assertTrue(any("must clear expected_pr" in b for b in blocking))

    def test_unmerged_referenced_pr_is_rejected(self) -> None:
        # Fail closed unless the referenced PR is proven merged.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)  # base expected_pr=99
            _pm_write_head_task(root, base, status="done", expected_pr=None)
            blocking = _pm_run(root, [
                ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                ChangedFile("D", f".shiki/locks/{PM_TASK}.json"),
                ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json"),
            ], merged=frozenset())  # PR #99 NOT in the merged set
        self.assertTrue(any("not proven merged" in b for b in blocking))

    def test_lock_left_active_is_rejected(self) -> None:
        # A lock file exists at head but is not released → residue not cleaned.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done", expected_pr=None)
            # Lock stays active and is NOT in the changed files.
            (root / ".shiki" / "locks" / f"{PM_TASK}.json").write_text(
                json.dumps({"task_id": PM_TASK, "locks": ["path:scripts/x.py"], "state": "active"}), encoding="utf-8")
            blocking = _pm_run(root, [
                ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json"),
            ])
        self.assertTrue(any("must release the task lock" in b for b in blocking))

    def test_multiple_task_ids_in_body_is_rejected(self) -> None:
        # The body must reference exactly one task id, else the workflow's merge
        # proof and the validator's task could resolve to different ids.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done", expected_pr=None)
            blocking: list[str] = []
            enforce_post_merge_reconcile(
                target=root, task_id=PM_TASK, base_shiki=root / ".shiki" / "base",
                changed_files_status=[ChangedFile("D", f".shiki/locks/{PM_TASK}.json"),
                                      ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                                      ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json")],
                blocking=blocking, warnings=[], merged_pr_numbers={99},
                pr_body=f"reconcile {PM_TASK} but also T-0001 <!-- shiki:post_merge_reconcile -->")
        self.assertTrue(any("exactly one task id" in b for b in blocking))

    def test_single_task_id_body_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = _pm_seed(root)
            _pm_write_head_task(root, base, status="done", expected_pr=None)
            blocking: list[str] = []
            enforce_post_merge_reconcile(
                target=root, task_id=PM_TASK, base_shiki=root / ".shiki" / "base",
                changed_files_status=[ChangedFile("D", f".shiki/locks/{PM_TASK}.json"),
                                      ChangedFile("M", f".shiki/tasks/{PM_TASK}.json"),
                                      ChangedFile("A", f".shiki/ledger/{PM_LOCK}.json")],
                blocking=blocking, warnings=[], merged_pr_numbers={99},
                pr_body=f"reconcile {PM_TASK} <!-- shiki:post_merge_reconcile -->")
        self.assertEqual(blocking, [])

    def test_decision_marker_and_label(self) -> None:
        m = "<!-- shiki:post_merge_reconcile -->"
        lab = {"name": "mergegate:post_merge_reconcile"}
        self.assertEqual(post_merge_reconcile_decision({"body": m, "labels": [lab]}), (True, None))
        mode, err = post_merge_reconcile_decision({"body": m, "labels": []})
        self.assertFalse(mode)
        self.assertIn("label", err)
        self.assertEqual(post_merge_reconcile_decision({"body": "x", "labels": [lab]}), (False, None))


if __name__ == "__main__":
    unittest.main()
