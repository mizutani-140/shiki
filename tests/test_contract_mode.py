"""Contract PR mode (ADR 0015): approve the task contract before dispatch.

A Contract PR carries a spec-frozen Goal's task contracts — goal, task, and DAG
registration — to the default branch so a Guardian can approve them before any
implementation is dispatched. MergeGate validates it as a contract mode that
GENERALIZES the goal_reconcile validator: deny by default, every registered task
matched against its frozen plan definition on the governance fields, the goal
file itself registered (matched to its frozen plan), and Guardian evaluation
forced from the Goal's (frozen plan's) risk level.

These tests exercise the contract-mode validator directly. The full MergeGate
main() flow (guardian block/pass, CLI -> MergeGate acceptance) is covered by
tests/test_contract_open.py.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from mergegate_check import (
    ChangedFile,
    contract_decision,
    contract_guardian_risk,
    enforce_contract,
)

GOAL = "G-20260727T000000000000Z-0000aaaa"
PLAN = "P-20260727T000000000000Z-0000pppp"
T_A = "T-20260727T000000000000Z-0000a001"
T_B = "T-20260727T000000000000Z-0000b002"
LEDGER = "L-20260727T000000000000Z-0000l003"
GOAL_LEDGER = "L-20260727T000000000000Z-0000g004"
TITLE_A = "Pre-dispatch contract gate"
TITLE_B = "Contract immutability"

PLAN_RISK = "high"

# Frozen plan task definitions. B depends on A (by title). Both carry the full
# governance-field set a registered task must match exactly.
FROZEN_A = {
    "title": TITLE_A,
    "scope": "scope-a",
    "non_goals": ["ng-a"],
    "required_skills": ["tdd", "code-review"],
    "risk_level": "high",
    "locks": ["path:scripts/a.py"],
    "acceptance_checks": ["a"],
    "runtime": "claude-code",
}
FROZEN_B = {
    "title": TITLE_B,
    "scope": "scope-b",
    "non_goals": ["ng-b"],
    "required_skills": ["tdd", "code-review"],
    "risk_level": "high",
    "locks": ["path:scripts/b.py"],
    "acceptance_checks": ["b"],
    "runtime": "claude-code",
    "dependencies": [TITLE_A],
}

_TITLE_TO_ID = {TITLE_A: T_A, TITLE_B: T_B}


def _seed(root: Path, *, frozen: bool = True, plan_risk: str = PLAN_RISK) -> None:
    for d in ("goals", "plans", "tasks", "dag", "ledger"):
        (root / ".shiki" / d).mkdir(parents=True, exist_ok=True)
    (root / ".shiki" / "plans" / f"{PLAN}.json").write_text(
        json.dumps(
            {
                "id": PLAN,
                "title": "ADR 0015 contract approval",
                "risk_level": plan_risk,
                "spec_freeze": {"status": "frozen" if frozen else "draft"},
                "tasks": [FROZEN_A, FROZEN_B],
            }
        ),
        encoding="utf-8",
    )


def _write_goal(root: Path, *, source_plan: str = PLAN, risk: str = PLAN_RISK, status: str = "planned") -> None:
    (root / ".shiki" / "goals" / f"{GOAL}.json").write_text(
        json.dumps(
            {"id": GOAL, "status": status, "source_plan": source_plan, "risk_level": risk, "title": "g"}
        ),
        encoding="utf-8",
    )


def _write_task(root: Path, tid: str, *, title: str, status: str = "planned", goal: str = GOAL, **overrides) -> None:
    frozen = {TITLE_A: FROZEN_A, TITLE_B: FROZEN_B}.get(title, {})
    deps = [_TITLE_TO_ID[d] for d in frozen.get("dependencies", []) if d in _TITLE_TO_ID]
    task = {
        "id": tid,
        "goal_id": goal,
        "title": title,
        "status": status,
        "scope": frozen.get("scope", "scope-x"),
        "non_goals": frozen.get("non_goals", []),
        "required_skills": frozen.get("required_skills", ["tdd", "code-review"]),
        "risk_level": frozen.get("risk_level", "high"),
        "locks": frozen.get("locks", []),
        "acceptance_checks": frozen.get("acceptance_checks", ["x"]),
        "assigned_runtime": frozen.get("runtime", "claude-code"),
        "dependencies": deps,
    }
    task.update(overrides)
    (root / ".shiki" / "tasks" / f"{tid}.json").write_text(json.dumps(task), encoding="utf-8")


def _write_ledger(root: Path, lid: str, *, goal: str = GOAL, ltype: str = "task-registered") -> None:
    (root / ".shiki" / "ledger" / f"{lid}.json").write_text(
        json.dumps(
            {
                "id": lid,
                "goal_id": goal,
                "task_id": None,
                "type": ltype,
                "actor": "shiki-cli",
                "timestamp": "2026-07-27T00:00:00+00:00",
                "summary": "contract registration",
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )


def _dag(root: Path, *, nodes, edges) -> None:
    (root / ".shiki" / "dag" / f"{GOAL}.json").write_text(
        json.dumps({"goal_id": GOAL, "nodes": nodes, "edges": edges}), encoding="utf-8"
    )


def _run(root: Path, changes: list[ChangedFile], *, goal: str = GOAL) -> list[str]:
    blocking: list[str] = []
    enforce_contract(target=root, goal_id=goal, changed_files_status=changes, blocking=blocking, warnings=[])
    return blocking


def _full_registration_changes() -> list[ChangedFile]:
    return [
        ChangedFile("A", f".shiki/goals/{GOAL}.json"),
        ChangedFile("A", f".shiki/tasks/{T_A}.json"),
        ChangedFile("A", f".shiki/tasks/{T_B}.json"),
        ChangedFile("A", f".shiki/dag/{GOAL}.json"),
        ChangedFile("A", f".shiki/ledger/{GOAL_LEDGER}.json"),
        ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
    ]


def _seed_full(root: Path) -> None:
    _seed(root)
    _write_goal(root)
    _write_task(root, T_A, title=TITLE_A)
    _write_task(root, T_B, title=TITLE_B)
    _write_ledger(root, GOAL_LEDGER, ltype="goal-created")
    _write_ledger(root, LEDGER, ltype="task-registered")
    _dag(root, nodes=[T_A, T_B], edges=[{"from": T_A, "to": T_B, "reason": "declared plan dependency"}])


class ContractDecisionTests(unittest.TestCase):
    MARKER = "<!-- shiki:contract -->"
    LABEL = {"name": "mergegate:contract"}

    def test_marker_and_label_enables_mode(self) -> None:
        mode, err = contract_decision({"body": f"register contracts {self.MARKER}", "labels": [self.LABEL]})
        self.assertTrue(mode)
        self.assertIsNone(err)

    def test_marker_without_label_fails_closed(self) -> None:
        # The label is the independent second factor; the CLI must not self-apply
        # it. A marker alone (self-declared) must not grant the relaxed mode.
        mode, err = contract_decision({"body": f"contract {self.MARKER}", "labels": []})
        self.assertFalse(mode)
        self.assertIsNotNone(err)
        self.assertIn("label", err)
        self.assertIn("mergegate:contract", err)

    def test_label_without_marker_is_not_contract(self) -> None:
        mode, err = contract_decision({"body": "normal PR", "labels": [self.LABEL]})
        self.assertFalse(mode)
        self.assertIsNone(err)

    def test_plain_pr_is_not_contract(self) -> None:
        mode, err = contract_decision({"body": "G-0001 normal work", "labels": []})
        self.assertFalse(mode)
        self.assertIsNone(err)


class ContractRegistrationTests(unittest.TestCase):
    def test_valid_contract_registration_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            blocking = _run(root, _full_registration_changes())
        self.assertEqual(blocking, [])

    def test_implementation_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            changes = _full_registration_changes() + [ChangedFile("M", "scripts/shiki_loop.py")]
            blocking = _run(root, changes)
        self.assertTrue(any("non-Shiki" in b for b in blocking))

    def test_task_frozen_definition_mismatch_is_rejected(self) -> None:
        # A registered task whose governance fields diverge from the frozen plan
        # (scope, non_goals, required_skills, risk_level, locks, acceptance_checks)
        # must be blocked — binding is to the frozen task definition, not title.
        for field, bad in (
            ("scope", "weakened"),
            ("non_goals", ["different"]),
            ("required_skills", ["tdd"]),
            ("risk_level", "low"),
            ("locks", ["path:scripts/anything.py"]),
            ("acceptance_checks", ["weakened"]),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _seed(root)
                _write_goal(root)
                _write_task(root, T_A, title=TITLE_A, **{field: bad})
                _write_task(root, T_B, title=TITLE_B)
                _write_ledger(root, GOAL_LEDGER, ltype="goal-created")
                _write_ledger(root, LEDGER, ltype="task-registered")
                _dag(root, nodes=[T_A, T_B], edges=[{"from": T_A, "to": T_B}])
                blocking = _run(root, _full_registration_changes())
            self.assertTrue(
                any(field in b and "frozen plan definition" in b for b in blocking),
                f"field {field} mismatch not rejected: {blocking}",
            )

    def test_task_not_in_frozen_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_goal(root)
            _write_task(root, T_A, title="Unfrozen sneak task")
            _write_ledger(root, LEDGER, ltype="task-registered")
            blocking = _run(root, [
                ChangedFile("A", f".shiki/goals/{GOAL}.json"),
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("not in the goal's frozen plan" in b for b in blocking))

    def test_goal_modification_is_rejected(self) -> None:
        # A Contract PR registers the goal for the first time (ADD); it must not
        # MODIFY an existing goal file.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            changes = [c for c in _full_registration_changes() if not c.path.endswith(f"goals/{GOAL}.json")]
            changes.append(ChangedFile("M", f".shiki/goals/{GOAL}.json"))
            blocking = _run(root, changes)
        self.assertTrue(any("goal" in b.lower() and "ADD" in b for b in blocking))

    def test_goal_risk_downgrade_is_rejected(self) -> None:
        # The registered goal's risk must match its frozen plan; a downgrade would
        # weaken the Guardian gate the contract exists to force.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root, plan_risk="high")
            _write_goal(root, risk="low")  # frozen plan is high
            _write_task(root, T_A, title=TITLE_A)
            _write_task(root, T_B, title=TITLE_B)
            _write_ledger(root, GOAL_LEDGER, ltype="goal-created")
            _write_ledger(root, LEDGER, ltype="task-registered")
            _dag(root, nodes=[T_A, T_B], edges=[{"from": T_A, "to": T_B}])
            blocking = _run(root, _full_registration_changes())
        self.assertTrue(any("risk_level" in b and "frozen plan" in b for b in blocking))

    def test_forbidden_authority_surface_is_rejected(self) -> None:
        for forbidden in (
            ".shiki/guardian-policy.json",
            ".shiki/memories/MEM-20260727T000000000000Z-0000m001.json",
            f".shiki/locks/{T_A}.json",
            f".shiki/plans/{PLAN}.json",
        ):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _seed_full(root)
                blocking = _run(root, _full_registration_changes() + [ChangedFile("A", forbidden)])
            self.assertTrue(any("must not change" in b for b in blocking), f"{forbidden} not rejected")

    def test_dag_must_cover_every_frozen_task(self) -> None:
        # A subset DAG (dropping a frozen task) is rejected: it would later let the
        # goal be forced complete on the registered subset.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_goal(root)
            _write_task(root, T_A, title=TITLE_A)
            _write_task(root, T_B, title=TITLE_B)
            _write_ledger(root, GOAL_LEDGER, ltype="goal-created")
            _write_ledger(root, LEDGER, ltype="task-registered")
            _dag(root, nodes=[T_A], edges=[])  # drops T_B
            blocking = _run(root, _full_registration_changes())
        self.assertTrue(any("cover every frozen plan task" in b for b in blocking))

    def test_missing_registration_ledger_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            _write_goal(root)
            _write_task(root, T_A, title=TITLE_A)
            _write_task(root, T_B, title=TITLE_B)
            _dag(root, nodes=[T_A, T_B], edges=[{"from": T_A, "to": T_B}])
            blocking = _run(root, [
                ChangedFile("A", f".shiki/goals/{GOAL}.json"),
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/tasks/{T_B}.json"),
                ChangedFile("A", f".shiki/dag/{GOAL}.json"),
            ])
        self.assertTrue(any("task-registered" in b for b in blocking))

    def test_goal_without_canonical_risk_is_rejected(self) -> None:
        # Fail closed: a Contract PR whose goal (and frozen plan) lack a canonical
        # risk_level must be blocked, not admitted with the Guardian gate skipped.
        # The Guardian gate is forced from risk, so an unresolvable risk is a hole.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for d in ("goals", "plans", "tasks", "dag", "ledger"):
                (root / ".shiki" / d).mkdir(parents=True, exist_ok=True)
            (root / ".shiki" / "plans" / f"{PLAN}.json").write_text(
                json.dumps({"id": PLAN, "title": "t", "spec_freeze": {"status": "frozen"},
                            "tasks": [dict(FROZEN_A), dict(FROZEN_B)]}), encoding="utf-8")
            # Goal omits risk_level entirely.
            (root / ".shiki" / "goals" / f"{GOAL}.json").write_text(
                json.dumps({"id": GOAL, "status": "planned", "source_plan": PLAN, "title": "g"}),
                encoding="utf-8")
            _write_task(root, T_A, title=TITLE_A)
            _write_task(root, T_B, title=TITLE_B)
            _write_ledger(root, GOAL_LEDGER, ltype="goal-created")
            _write_ledger(root, LEDGER, ltype="task-registered")
            _dag(root, nodes=[T_A, T_B], edges=[{"from": T_A, "to": T_B, "reason": "dep"}])
            blocking = _run(root, _full_registration_changes())
        self.assertTrue(any("canonical risk_level" in b for b in blocking), blocking)

    def test_unfrozen_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root, frozen=False)
            _write_goal(root)
            _write_task(root, T_A, title=TITLE_A)
            _write_ledger(root, LEDGER, ltype="task-registered")
            blocking = _run(root, [
                ChangedFile("A", f".shiki/goals/{GOAL}.json"),
                ChangedFile("A", f".shiki/tasks/{T_A}.json"),
                ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
            ])
        self.assertTrue(any("not spec-frozen" in b for b in blocking))


class ContractConstantConsistencyTests(unittest.TestCase):
    """The CLI-emitted marker/label and the validator's must not drift; they live
    in two locked modules (no shared unlocked home for this task), so bind them."""

    def test_cli_label_matches_validator_label(self) -> None:
        import mergegate_check
        import shiki_tasks

        self.assertEqual(shiki_tasks.CONTRACT_PR_LABEL, mergegate_check.CONTRACT_LABEL)

    def test_cli_marker_is_accepted_by_validator_regex(self) -> None:
        import mergegate_check
        import shiki_tasks

        self.assertTrue(mergegate_check.CONTRACT_MARKER.search(shiki_tasks.CONTRACT_PR_MARKER))


class ContractGuardianRiskTests(unittest.TestCase):
    def test_risk_forced_from_frozen_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root, plan_risk="high")
            _write_goal(root, risk="high")
            self.assertEqual(contract_guardian_risk(root, GOAL), "high")

    def test_risk_never_weaker_than_frozen_plan(self) -> None:
        # Even if the goal file understates its risk, the frozen plan (immutable
        # base authority) drives the Guardian gate: the resolved risk is the max.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root, plan_risk="critical")
            _write_goal(root, risk="low")
            self.assertEqual(contract_guardian_risk(root, GOAL), "critical")

    def test_missing_goal_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed(root)
            self.assertIsNone(contract_guardian_risk(root, GOAL))


if __name__ == "__main__":
    unittest.main()
