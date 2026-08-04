"""Spec Amendment PR mode (ADR 0009/0015): correct a frozen contract on a
protected branch.

When implementation proves a spec-frozen contract wrong — an acceptance criterion
that cannot be satisfied, locks too narrow for the change the task requires — the
loop's only options are to stop or for the operator to hand-commit to main,
losing branch protection and the audit trail. Three independent MergeGate blocks
(the spec_freeze-authoring gate, the contract/goal_reconcile ADD-only task rule,
and the normal-path contract-immutability freeze) leave zero routes to correct it
through a PR. Amendment mode is the sanctioned route: a two-factor (marker +
maintainer label) PR that appends to the source plan's ``spec_freeze.amendments``
and re-opens this goal's tasks bound to the amended plan — deny by default for
everything else.

These tests drive the real gate (``enforce_amendment`` / ``amendment_decision``)
directly, plus the ``contract-amended`` ledger type registration that the mode's
amendment ledger depends on.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from mergegate_check import (
    ChangedFile,
    _max_risk,
    amendment_decision,
    contract_guardian_risk,
    enforce_amendment,
)

GOAL = "G-20260804T000000000000Z-0000aaaa"
OTHER_GOAL = "G-20260804T000000000000Z-0000bbbb"
PLAN = "P-20260804T000000000000Z-0000pppp"
OTHER_PLAN = "P-20260804T000000000000Z-0000qqqq"
T_A = "T-20260804T000000000000Z-0000a001"
T_B = "T-20260804T000000000000Z-0000b002"
T_C = "T-20260804T000000000000Z-0000c003"
T_FOREIGN = "T-20260804T000000000000Z-0000f004"
AMEND_LEDGER = "L-20260804T000000000000Z-0000a005"

TITLE_A = "Widen the narrow lock"
TITLE_B = "Downstream slice"

NARROW = ["path:scripts/a.py"]
WIDE = ["path:scripts/a.py", "path:scripts/a2.py"]

# spec_freeze.amendments entries. A1 exists at base; A2 is the new entry the
# amendment appends. Their exact shape is opaque to the gate (it only checks the
# base list is a strict prefix of the head list).
A1 = {"id": "AMD-0001", "reason": "initial freeze note"}
A2 = {"id": "AMD-0002", "reason": "widen T_A locks to include scripts/a2.py"}

_TITLE_TO_ID = {TITLE_A: T_A, TITLE_B: T_B}


def _goal(risk: str = "high") -> dict:
    return {"id": GOAL, "status": "planned", "source_plan": PLAN, "risk_level": risk, "title": "g"}


def _spec_freeze(*, amendments: list, status: str = "frozen") -> dict:
    return {
        "status": status,
        "approved_by": "operator",
        "prd": "PRD-1",
        "source": "grill-with-docs",
        "amendments": amendments,
    }


def _frozen_a(locks: list) -> dict:
    return {
        "title": TITLE_A,
        "scope": "scope-a",
        "non_goals": ["ng-a"],
        "required_skills": ["tdd", "code-review"],
        "risk_level": "high",
        "locks": locks,
        "acceptance_checks": ["a"],
        "runtime": "claude-code",
    }


def _frozen_b() -> dict:
    return {
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


def _plan(*, amendments: list, a_locks: list, risk: str = "high", status: str = "frozen") -> dict:
    return {
        "id": PLAN,
        "title": "amendment plan",
        "outcome": "corrected contract",
        "risk_level": risk,
        "spec_freeze": _spec_freeze(amendments=amendments, status=status),
        "tasks": [_frozen_a(a_locks), _frozen_b()],
    }


def _task_a(*, locks: list, status: str, goal: str = GOAL, **overrides) -> dict:
    task = {
        "id": T_A,
        "goal_id": goal,
        "title": TITLE_A,
        "status": status,
        "scope": "scope-a",
        "non_goals": ["ng-a"],
        "required_skills": ["tdd", "code-review"],
        "risk_level": "high",
        "locks": locks,
        "acceptance_checks": ["a"],
        "assigned_runtime": "claude-code",
        "dependencies": [],
        "expected_pr": 100,
        "closeout_pr": None,
        "expected_branch": "shiki/t-a-branch",
        "ledger_evidence": ["L-20260804T000000000000Z-0000base1"],
    }
    task.update(overrides)
    return task


def _task_b() -> dict:
    return {
        "id": T_B,
        "goal_id": GOAL,
        "title": TITLE_B,
        "status": "planned",
        "scope": "scope-b",
        "non_goals": ["ng-b"],
        "required_skills": ["tdd", "code-review"],
        "risk_level": "high",
        "locks": ["path:scripts/b.py"],
        "acceptance_checks": ["b"],
        "assigned_runtime": "claude-code",
        "dependencies": [T_A],
        "expected_pr": 101,
        "closeout_pr": None,
        "expected_branch": "shiki/t-b-branch",
        "ledger_evidence": ["L-20260804T000000000000Z-0000base2"],
    }


def _dag() -> dict:
    return {
        "goal_id": GOAL,
        "nodes": [T_A, T_B],
        "edges": [{"from": T_A, "to": T_B, "reason": "declared plan dependency"}],
    }


def _ledger(ltype: str = "contract-amended", *, goal: str = GOAL) -> dict:
    return {
        "id": AMEND_LEDGER,
        "goal_id": goal,
        "task_id": None,
        "type": ltype,
        "actor": "operator",
        "timestamp": "2026-08-04T00:00:00+00:00",
        "summary": "Spec Amendment: widen T_A locks",
        "evidence": ["PR #300"],
    }


def _write(root: Path, rel: str, payload: dict) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_valid(head: Path, base: Path) -> None:
    """A well-formed amendment: the base plan is spec-frozen with one amendment;
    the head appends a second amendment, widens the frozen T_A locks, and re-opens
    T_A to planned with the same widened locks. Everything else is unchanged."""
    # Base snapshot (pre-amendment, spec-frozen).
    _write(base, f".shiki/goals/{GOAL}.json", _goal())
    _write(base, f".shiki/plans/{PLAN}.json", _plan(amendments=[A1], a_locks=NARROW))
    _write(base, f".shiki/tasks/{T_A}.json", _task_a(locks=NARROW, status="review"))
    _write(base, f".shiki/tasks/{T_B}.json", _task_b())
    _write(base, f".shiki/dag/{GOAL}.json", _dag())
    # Head (the amendment).
    _write(head, f".shiki/goals/{GOAL}.json", _goal())
    _write(head, f".shiki/plans/{PLAN}.json", _plan(amendments=[A1, A2], a_locks=WIDE))
    _write(head, f".shiki/tasks/{T_A}.json", _task_a(locks=WIDE, status="planned"))
    _write(head, f".shiki/tasks/{T_B}.json", _task_b())
    _write(head, f".shiki/dag/{GOAL}.json", _dag())
    _write(head, f".shiki/ledger/{AMEND_LEDGER}.json", _ledger())


def _default_changes() -> list[ChangedFile]:
    return [
        ChangedFile("M", f".shiki/plans/{PLAN}.json"),
        ChangedFile("M", f".shiki/tasks/{T_A}.json"),
        ChangedFile("A", f".shiki/ledger/{AMEND_LEDGER}.json"),
    ]


def _run(head: Path, base: Path, changes: list[ChangedFile], *, goal: str = GOAL) -> list[str]:
    blocking: list[str] = []
    enforce_amendment(
        target=head,
        goal_id=goal,
        base_shiki=base / ".shiki",
        changed_files_status=changes,
        blocking=blocking,
        warnings=[],
    )
    return blocking


class AmendmentDecisionTests(unittest.TestCase):
    MARKER = "<!-- shiki:amendment -->"
    LABEL = {"name": "mergegate:amendment"}

    def test_marker_and_label_enables_mode(self) -> None:
        mode, err = amendment_decision({"body": f"amend {self.MARKER}", "labels": [self.LABEL]})
        self.assertTrue(mode)
        self.assertIsNone(err)

    def test_marker_without_label_fails_closed_with_label_message(self) -> None:
        # The label is the independent second factor; a marker alone (self-declared
        # in untrusted PR text) must not grant the relaxed mode. Same message shape
        # as contract_decision.
        mode, err = amendment_decision({"body": f"amend {self.MARKER}", "labels": []})
        self.assertFalse(mode)
        self.assertIsNotNone(err)
        self.assertIn("label", err)
        self.assertIn("mergegate:amendment", err)

    def test_label_without_marker_does_not_enter_mode(self) -> None:
        mode, err = amendment_decision({"body": "normal PR G-0001", "labels": [self.LABEL]})
        self.assertFalse(mode)
        self.assertIsNone(err)

    def test_plain_pr_is_not_amendment(self) -> None:
        mode, err = amendment_decision({"body": "G-0001 normal work", "labels": []})
        self.assertFalse(mode)
        self.assertIsNone(err)


class AmendmentGateTests(unittest.TestCase):
    def _ctx(self):
        return tempfile.TemporaryDirectory()

    def test_well_formed_amendment_passes(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            blocking = _run(head, base, _default_changes())
        self.assertEqual(blocking, [])

    def test_implementation_file_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            changes = _default_changes() + [ChangedFile("M", "scripts/mergegate_check.py")]
            blocking = _run(head, base, changes)
        self.assertTrue(any("non-Shiki" in b and "amendment" in b for b in blocking), blocking)

    def test_missing_contract_amended_ledger_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            # Drop the ledger from the diff: no contract-amended event registered.
            changes = [
                ChangedFile("M", f".shiki/plans/{PLAN}.json"),
                ChangedFile("M", f".shiki/tasks/{T_A}.json"),
            ]
            blocking = _run(head, base, changes)
        self.assertTrue(any("contract-amended ledger" in b for b in blocking), blocking)

    def test_wrong_type_ledger_does_not_satisfy_requirement(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            _write(head, f".shiki/ledger/{AMEND_LEDGER}.json", _ledger("review"))
            blocking = _run(head, base, _default_changes())
        self.assertTrue(any("contract-amended ledger" in b for b in blocking), blocking)

    # --- spec_freeze / amendments immutability ---

    def _mutate_head_plan(self, head: Path, plan: dict) -> None:
        _write(head, f".shiki/plans/{PLAN}.json", plan)

    def test_editing_an_existing_amendment_entry_is_blocked(self) -> None:
        edited = dict(A1, reason="tampered")
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            self._mutate_head_plan(head, _plan(amendments=[edited, A2], a_locks=WIDE))
            blocking = _run(head, base, _default_changes())
        self.assertTrue(any("strict prefix" in b for b in blocking), blocking)

    def test_removing_an_existing_amendment_entry_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            # Base has two entries; head drops the first (breaks the prefix).
            _seed_valid(head, base)
            _write(base, f".shiki/plans/{PLAN}.json", _plan(amendments=[A1, A2], a_locks=NARROW))
            self._mutate_head_plan(head, _plan(amendments=[A2], a_locks=WIDE))
            blocking = _run(head, base, _default_changes())
        self.assertTrue(any("strict prefix" in b for b in blocking), blocking)

    def test_reordering_an_existing_amendment_entry_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            _write(base, f".shiki/plans/{PLAN}.json", _plan(amendments=[A1, A2], a_locks=NARROW))
            # Reorder the two existing entries and append a third.
            a3 = {"id": "AMD-0003", "reason": "third"}
            self._mutate_head_plan(head, _plan(amendments=[A2, A1, a3], a_locks=WIDE))
            blocking = _run(head, base, _default_changes())
        self.assertTrue(any("strict prefix" in b for b in blocking), blocking)

    def test_amendment_with_no_new_entry_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            # Head amendments identical to base: a strict prefix, but no new entry.
            self._mutate_head_plan(head, _plan(amendments=[A1], a_locks=WIDE))
            blocking = _run(head, base, _default_changes())
        self.assertTrue(any("at least one new" in b for b in blocking), blocking)

    def test_changing_spec_freeze_status_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            # A head plan that is no longer frozen cannot bind as the frozen
            # authority: the amendment is refused (status is not a movable field).
            self._mutate_head_plan(head, _plan(amendments=[A1, A2], a_locks=WIDE, status="draft"))
            blocking = _run(head, base, _default_changes())
        self.assertTrue(any("spec-frozen" in b or "spec_freeze" in b for b in blocking), blocking)

    def _spec_freeze_field_case(self, field: str, value) -> list[str]:
        head_plan = _plan(amendments=[A1, A2], a_locks=WIDE)
        head_plan["spec_freeze"][field] = value
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            self._mutate_head_plan(head, head_plan)
            return _run(head, base, _default_changes())

    def test_changing_spec_freeze_approved_by_is_blocked(self) -> None:
        blocking = self._spec_freeze_field_case("approved_by", "attacker")
        self.assertTrue(any("spec_freeze fields other than amendments" in b for b in blocking), blocking)

    def test_changing_spec_freeze_prd_is_blocked(self) -> None:
        blocking = self._spec_freeze_field_case("prd", "PRD-EVIL")
        self.assertTrue(any("spec_freeze fields other than amendments" in b for b in blocking), blocking)

    def test_changing_spec_freeze_source_is_blocked(self) -> None:
        blocking = self._spec_freeze_field_case("source", "forged")
        self.assertTrue(any("spec_freeze fields other than amendments" in b for b in blocking), blocking)

    def test_foreign_plan_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            # A plan that is not this goal's source_plan.
            changes = _default_changes() + [ChangedFile("M", f".shiki/plans/{OTHER_PLAN}.json")]
            blocking = _run(head, base, changes)
        self.assertTrue(any("source_plan" in b and OTHER_PLAN in b for b in blocking), blocking)

    def test_plan_not_spec_frozen_at_base_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            # Base plan is a draft: the "amendment" would be an operator-only freeze.
            _write(base, f".shiki/plans/{PLAN}.json", _plan(amendments=[A1], a_locks=NARROW, status="draft"))
            blocking = _run(head, base, _default_changes())
        self.assertTrue(any("not spec-frozen at base" in b for b in blocking), blocking)

    # --- task binding ---

    def test_task_not_matching_amended_plan_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            # Head plan widens T_A locks but the task keeps the narrow set: the task
            # no longer matches the amended plan's frozen definition.
            _write(head, f".shiki/tasks/{T_A}.json", _task_a(locks=NARROW, status="planned"))
            blocking = _run(head, base, _default_changes())
        self.assertTrue(
            any("locks" in b and "frozen plan definition" in b for b in blocking), blocking
        )

    def test_title_absent_from_amended_plan_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            _write(head, f".shiki/tasks/{T_A}.json", _task_a(locks=WIDE, status="planned", title="Sneaky title"))
            blocking = _run(head, base, _default_changes())
        self.assertTrue(any("not in the goal's frozen plan" in b for b in blocking), blocking)

    def test_task_of_another_goal_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            # A modified task file that belongs to a DIFFERENT goal.
            foreign = _task_a(locks=WIDE, status="planned", goal=OTHER_GOAL)
            foreign["id"] = T_FOREIGN
            _write(head, f".shiki/tasks/{T_FOREIGN}.json", foreign)
            _write(base, f".shiki/tasks/{T_FOREIGN}.json", foreign)
            changes = _default_changes() + [ChangedFile("M", f".shiki/tasks/{T_FOREIGN}.json")]
            blocking = _run(head, base, changes)
        self.assertTrue(any(T_FOREIGN in b and "not anchored to goal" in b for b in blocking), blocking)

    def test_added_task_file_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            added = _task_a(locks=WIDE, status="planned")
            added["id"] = T_C
            added["title"] = TITLE_A
            _write(head, f".shiki/tasks/{T_C}.json", added)
            changes = _default_changes() + [ChangedFile("A", f".shiki/tasks/{T_C}.json")]
            blocking = _run(head, base, changes)
        self.assertTrue(any(T_C in b and "not ADD" in b for b in blocking), blocking)

    def test_status_other_than_planned_or_ready_is_blocked(self) -> None:
        for bad in ("done", "review", "in_progress", "cancelled"):
            with self._ctx() as tmp:
                head, base = Path(tmp) / "head", Path(tmp) / "base"
                _seed_valid(head, base)
                _write(head, f".shiki/tasks/{T_A}.json", _task_a(locks=WIDE, status=bad))
                blocking = _run(head, base, _default_changes())
            self.assertTrue(
                any("status must be planned or ready" in b for b in blocking),
                f"status={bad} not blocked: {blocking}",
            )

    def test_ready_status_is_allowed(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            _write(head, f".shiki/tasks/{T_A}.json", _task_a(locks=WIDE, status="ready"))
            blocking = _run(head, base, _default_changes())
        self.assertEqual(blocking, [])

    def test_each_frozen_task_field_change_is_blocked(self) -> None:
        for field, bad in (
            ("expected_pr", 999),
            ("closeout_pr", 999),
            ("expected_branch", "shiki/hijacked"),
            ("ledger_evidence", ["L-20260804T000000000000Z-0000evil99"]),
        ):
            with self._ctx() as tmp:
                head, base = Path(tmp) / "head", Path(tmp) / "base"
                _seed_valid(head, base)
                _write(head, f".shiki/tasks/{T_A}.json", _task_a(locks=WIDE, status="planned", **{field: bad}))
                blocking = _run(head, base, _default_changes())
            self.assertTrue(
                any(f"frozen field {field!r}" in b for b in blocking),
                f"field {field} change not blocked: {blocking}",
            )

    # --- DAG (same rules the frozen-registration modes apply) ---

    def test_dag_dropping_a_frozen_task_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            # A truncated head DAG that abandons T_B.
            _write(head, f".shiki/dag/{GOAL}.json", {"goal_id": GOAL, "nodes": [T_A], "edges": []})
            blocking = _run(head, base, _default_changes())
        self.assertTrue(any("covering every frozen plan task" in b for b in blocking), blocking)

    def test_foreign_goal_dag_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            changes = _default_changes() + [ChangedFile("M", f".shiki/dag/{OTHER_GOAL}.json")]
            blocking = _run(head, base, changes)
        self.assertTrue(any("another goal's DAG" in b for b in blocking), blocking)

    def test_goal_file_change_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            changes = _default_changes() + [ChangedFile("M", f".shiki/goals/{GOAL}.json")]
            blocking = _run(head, base, changes)
        self.assertTrue(any("must not change" in b and f"goals/{GOAL}" in b for b in blocking), blocking)

    def test_deletion_is_blocked(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_valid(head, base)
            changes = _default_changes() + [ChangedFile("D", f".shiki/tasks/{T_B}.json")]
            blocking = _run(head, base, changes)
        self.assertTrue(any("must not delete" in b for b in blocking), blocking)


class AmendmentGuardianRiskTests(unittest.TestCase):
    def test_guardian_risk_is_the_stronger_of_base_and_head(self) -> None:
        # main() resolves the amendment's Guardian risk as
        #   _max_risk(contract_guardian_risk(head), contract_guardian_risk(base))
        # so an amendment that WEAKENS its own plan's risk cannot lower its own
        # gate. Prove it with a head plan weaker than base: base plan critical,
        # head plan low, goal low in both — the resolved risk must be critical.
        with tempfile.TemporaryDirectory() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _write(base, f".shiki/goals/{GOAL}.json", _goal("low"))
            _write(base, f".shiki/plans/{PLAN}.json", _plan(amendments=[A1], a_locks=NARROW, risk="critical"))
            _write(head, f".shiki/goals/{GOAL}.json", _goal("low"))
            _write(head, f".shiki/plans/{PLAN}.json", _plan(amendments=[A1, A2], a_locks=WIDE, risk="low"))

            head_risk = contract_guardian_risk(head, GOAL)
            base_risk = contract_guardian_risk(base, GOAL)
            self.assertEqual(head_risk, "low")
            self.assertEqual(base_risk, "critical")
            self.assertEqual(_max_risk(head_risk or "", base_risk or ""), "critical")


class ContractAmendedLedgerTypeTests(unittest.TestCase):
    """The ``contract-amended`` ledger type must be registered in BOTH bound
    places (ledger.schema.json enum AND validate_shiki.LEDGER_TYPES), which
    validate_shiki holds set-equal, and a contract-amended entry must validate
    against the schema."""

    @property
    def _schema(self) -> dict:
        schema_path = shiki_test_support.REPO_ROOT / ".shiki" / "schemas" / "ledger.schema.json"
        return json.loads(schema_path.read_text(encoding="utf-8"))

    def test_enum_and_ledger_types_are_set_equal(self) -> None:
        import validate_shiki

        enum = self._schema["properties"]["type"]["enum"]
        self.assertEqual(set(enum), validate_shiki.LEDGER_TYPES)
        self.assertIn("contract-amended", validate_shiki.LEDGER_TYPES)
        self.assertIn("contract-amended", enum)

    def test_contract_amended_ledger_validates_against_schema(self) -> None:
        from shiki_jsonschema import validate_json_schema

        entry = {
            "id": AMEND_LEDGER,
            "timestamp": "2026-08-04T00:00:00Z",
            "goal_id": GOAL,
            "task_id": None,
            "type": "contract-amended",
            "actor": "operator",
            "summary": "Spec Amendment recorded",
            "evidence": ["PR #300"],
        }
        # Raises JsonSchemaError on any violation; a clean return proves validity.
        validate_json_schema(entry, self._schema)


if __name__ == "__main__":
    unittest.main()
