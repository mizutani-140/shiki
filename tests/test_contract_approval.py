"""SADR-0015 Contract Approval — the OR-alternative to a live PR Guardian approval.

SADR-0015 shipped in halves. The registration half (contract mode,
``enforce_contract``, ``contract_guardian_risk``, the forced Guardian evaluation
from the frozen plan) landed. The "Evaluation placement" half — a new, separately
tested function that MergeGate accepts as an alternative to live PR approval — was
missing, so a Goal whose Contract PR was approved before dispatch still stopped
every implementation PR at ``needs_guardian`` and asked for the same approval
again.

``scripts/shiki_contract_approval.evaluate_contract_approval`` now answers, with no
I/O, whether a PR's task contract was registered and Guardian-approved before
dispatch and left unchanged. MergeGate's Guardian requirement is satisfied by
``evaluate_guardian_approval`` (live approval) OR a ``applies=True`` Contract
Approval; ``evaluate_guardian_approval`` is untouched, so a defect in the new path
degrades to "does not apply".

These tests DRIVE THE REAL GATE, never read the source:

  * the pure evaluator for the four conditions and every negative case;
  * ``mergegate_check.enforce_guardian_policy`` — the actual OR — asserting that a
    valid contract carries the Guardian requirement with NO live approval, and
    that EVERY negative still leaves the Guardian requirement resolving True
    (blocked), so a regression cannot hide behind an unrelated block;
  * ``mergegate_check.contract_approval_for_pr`` /
    ``load_contract_approval_registration`` — the flag-absent / missing /
    unreadable inputs that must leave the gate byte-for-byte what it is today.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import mergegate_check
from mergegate_check import (
    ChangedFile,
    _NORMAL_PATH_GOVERNANCE_FIELDS,
    contract_approval_for_pr,
    load_contract_approval_registration,
)
from shiki_contract_approval import (
    CONTRACT_APPROVAL_SOURCE,
    ContractApprovalResult,
    evaluate_contract_approval,
)

REPO_ROOT = shiki_test_support.REPO_ROOT
GUARDIAN_POLICY = (REPO_ROOT / ".shiki" / "guardian-policy.json").read_text(encoding="utf-8")

TASK_ID = "T-20260801T040836631930Z-97030be9"
GOAL_ID = "G-20260801T040836630398Z-f28167e5"
HEAD_SHA = "b" * 40
IMPL_PR = 199  # the implementation PR (carries no approval of its own)
REG_PR = 188  # the SEPARATE, already-merged, Guardian-approved Contract PR

# A well-formed base task contract, critical risk so the Guardian gate engages.
BASE_TASK = {
    "id": TASK_ID,
    "goal_id": GOAL_ID,
    "title": "Evaluate contract approval as a distinct approval source",
    "scope": "Add evaluate_contract_approval and OR it into the Guardian gate.",
    "non_goals": ["No change to evaluate_guardian_approval."],
    "required_skills": ["tdd", "code-review"],
    "risk_level": "critical",
    "locks": ["path:scripts/shiki_contract_approval.py"],
    "acceptance_checks": ["a contract-approved PR needs no live approval"],
    "test_command": "python3 -m unittest discover -s tests",
    "dependencies": [],
    "assigned_runtime": "claude-code",
    "status": "planned",
    "expected_pr": None,
    "expected_branch": "shiki/t-97030be9",
    "ledger_evidence": [],
}


# A sentinel distinct from ``None``: the test helpers use it for "use the default",
# so a test can still pass ``registration=None`` to mean the real "no proof" input.
_UNSET = object()


def valid_registration(**overrides) -> dict:
    """The deterministic proof of the merged, Guardian-approved Contract PR."""
    reg = {
        "task_id": TASK_ID,
        "pr": REG_PR,
        "merged": True,
        "adding_commit": "c" * 40,
        "adding_commit_pr": REG_PR,
        "guardian_approved": True,
        "guardian_source": "guardian_label",
    }
    reg.update(overrides)
    return reg


class EvaluatorTest(unittest.TestCase):
    """Drive the pure ``evaluate_contract_approval`` for all four conditions."""

    def evaluate(self, *, base_task=BASE_TASK, head_task=_UNSET, registration=_UNSET, changed=None):
        return evaluate_contract_approval(
            task_id=TASK_ID,
            base_task=base_task,
            head_task=BASE_TASK if head_task is _UNSET else head_task,
            changed_files_status=[] if changed is None else changed,
            registration=valid_registration() if registration is _UNSET else registration,
        )

    # --- covered ----------------------------------------------------------

    def test_all_four_conditions_hold_is_covered(self) -> None:
        result = self.evaluate()
        self.assertIsInstance(result, ContractApprovalResult)
        self.assertTrue(result.applies, result.reasons)
        self.assertEqual(result.sources, (CONTRACT_APPROVAL_SOURCE,))
        self.assertEqual(result.reasons, [])

    def test_result_is_frozen_and_pure(self) -> None:
        # No I/O: the proof is data. A frozen dataclass cannot be mutated to a
        # default-true result after the fact.
        result = self.evaluate()
        with self.assertRaises(Exception):
            result.applies = False  # type: ignore[misc]

    # --- condition 1: base snapshot must contain the task -----------------

    def test_task_absent_from_base_defeats_the_carry(self) -> None:
        for absent in (None, "not-a-dict", 123, []):
            with self.subTest(base=absent):
                result = self.evaluate(base_task=absent)
                self.assertFalse(result.applies)
                self.assertTrue(any("absent from the base snapshot" in r for r in result.reasons), result.reasons)

    # --- condition 2: each governance field differing defeats the carry ---

    def test_each_governance_field_differing_defeats_the_carry(self) -> None:
        # One case per field — the SAME seven-field constant the normal-path
        # immutability gate freezes, so the two can never drift.
        self.assertEqual(len(_NORMAL_PATH_GOVERNANCE_FIELDS), 7)
        for field in _NORMAL_PATH_GOVERNANCE_FIELDS:
            with self.subTest(field=field):
                head = {**BASE_TASK, field: "shiki:contract-tamper-sentinel"}
                result = self.evaluate(head_task=head)
                self.assertFalse(result.applies, f"changing {field} must defeat the carry")
                self.assertTrue(
                    any(f"{field!r}" in r for r in result.reasons),
                    f"a reason must name the field {field!r}: {result.reasons}",
                )

    def test_non_governance_field_difference_does_not_defeat_the_carry(self) -> None:
        # Lifecycle/bookkeeping fields the implementation PR legitimately moves must
        # NOT defeat the carry — only the seven governance fields are frozen.
        head = {**BASE_TASK, "status": "review", "expected_pr": IMPL_PR, "ledger_evidence": ["L-x"]}
        self.assertTrue(self.evaluate(head_task=head).applies)

    # --- condition 3: registration proof ----------------------------------

    def test_registration_missing_defeats_the_carry(self) -> None:
        for missing in (None, "not-a-dict", 0):
            with self.subTest(registration=missing):
                result = self.evaluate(registration=missing)
                self.assertFalse(result.applies)
                self.assertTrue(any("no registration proof was supplied" in r for r in result.reasons), result.reasons)

    def test_registration_merged_false_defeats_the_carry(self) -> None:
        for merged in (False, None, "true"):
            with self.subTest(merged=merged):
                result = self.evaluate(registration=valid_registration(merged=merged))
                self.assertFalse(result.applies)
                self.assertTrue(any("does not report the registering PR merged" in r for r in result.reasons), result.reasons)

    def test_registration_guardian_approved_false_defeats_the_carry(self) -> None:
        for approved in (False, None, "true"):
            with self.subTest(guardian_approved=approved):
                result = self.evaluate(registration=valid_registration(guardian_approved=approved))
                self.assertFalse(result.applies)
                self.assertTrue(any("does not report guardian_approved" in r for r in result.reasons), result.reasons)

    def test_registration_missing_guardian_source_defeats_the_carry(self) -> None:
        for source in ("", None, 5):
            with self.subTest(source=source):
                result = self.evaluate(registration=valid_registration(guardian_source=source))
                self.assertFalse(result.applies)
                self.assertTrue(any("no approving" in r for r in result.reasons), result.reasons)

    def test_registration_naming_a_different_pr_than_the_adding_commit_defeats(self) -> None:
        result = self.evaluate(registration=valid_registration(adding_commit_pr=REG_PR + 1))
        self.assertFalse(result.applies)
        self.assertTrue(any("does not belong to the registering PR" in r for r in result.reasons), result.reasons)

    def test_registration_missing_pr_number_defeats_the_carry(self) -> None:
        for pr in (None, 0, -3, "188", True):
            with self.subTest(pr=pr):
                # adding_commit_pr mirrors pr so only the PR-number condition is exercised.
                result = self.evaluate(registration=valid_registration(pr=pr, adding_commit_pr=pr))
                self.assertFalse(result.applies)

    def test_registration_missing_adding_commit_defeats_the_carry(self) -> None:
        for commit in (None, "", 42):
            with self.subTest(adding_commit=commit):
                result = self.evaluate(registration=valid_registration(adding_commit=commit))
                self.assertFalse(result.applies)
                self.assertTrue(any("commit that added" in r for r in result.reasons), result.reasons)

    # --- condition 4: proof must be for THIS task -------------------------

    def test_registration_task_id_mismatch_defeats_the_carry(self) -> None:
        result = self.evaluate(registration=valid_registration(task_id="T-20260101T000000000000Z-deadbeef"))
        self.assertFalse(result.applies)
        self.assertTrue(any("does not match task" in r for r in result.reasons), result.reasons)


class GuardianGateOrTest(unittest.TestCase):
    """Drive the REAL Guardian gate (``enforce_guardian_policy``): the OR must
    carry a valid contract with no live approval, and every negative must still
    leave the Guardian requirement resolving True (blocked)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.target = root / "target"
        (self.target / ".shiki" / "tasks").mkdir(parents=True)
        (self.target / ".shiki" / "guardian-policy.json").write_text(GUARDIAN_POLICY, encoding="utf-8")
        # Head task snapshot (the worktree state MergeGate reads).
        self._write_task(self.target / ".shiki" / "tasks" / f"{TASK_ID}.json", BASE_TASK)
        # Base snapshot (.shiki dir) with the registered contract.
        self.base_shiki = root / "base" / ".shiki"
        (self.base_shiki / "tasks").mkdir(parents=True)
        self._write_task(self.base_shiki / "tasks" / f"{TASK_ID}.json", BASE_TASK)
        # Empty live-approval evidence: NO guardian:approved label, NO approval
        # comment. The ONLY thing that can satisfy the gate is Contract Approval.
        self.comments = root / "comments.json"
        self.events = root / "events.json"
        self.comments.write_text("[]", encoding="utf-8")
        self.events.write_text("[]", encoding="utf-8")
        self.pr = {
            "number": IMPL_PR,
            "headRefOid": HEAD_SHA,
            "author": {"login": "implementer"},
            "labels": [{"name": "risk:critical"}],
            "reviews": [],
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _write_task(path: Path, task: dict) -> None:
        path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def guardian_result(self, contract_approval) -> tuple[list[str], list[str]]:
        """Run the real gate and return (blocking, warnings)."""
        blocking: list[str] = []
        warnings: list[str] = []
        mergegate_check.enforce_guardian_policy(
            pr=self.pr,
            task={"risk_level": "critical"},
            target=self.target,
            guardian_policy=str(self.target / ".shiki" / "guardian-policy.json"),
            guardian_comments=str(self.comments),
            guardian_events=str(self.events),
            guardian_timeline="",
            blocking=blocking,
            warnings=warnings,
            expected_repository="mizutani-140/shiki",
            contract_approval=contract_approval,
        )
        return blocking, warnings

    def carry(self, *, registration=_UNSET, base=True, changed=None):
        """Compute the contract-approval result exactly as ``main`` does, through
        ``contract_approval_for_pr`` (which reads the base snapshot from disk)."""
        return contract_approval_for_pr(
            task_id=TASK_ID,
            head_task=BASE_TASK,
            base_shiki=self.base_shiki if base else None,
            changed_files_status=[] if changed is None else changed,
            registration=valid_registration() if registration is _UNSET else registration,
        )

    def assertGuardianBlocked(self, contract_approval, msg: str) -> None:
        blocking, _ = self.guardian_result(contract_approval)
        self.assertTrue(blocking, f"{msg}: the Guardian requirement must still resolve True (blocked)")
        self.assertTrue(
            any("guardian" in reason.lower() for reason in blocking),
            f"{msg}: a blocking reason must be the Guardian requirement, not an unrelated block: {blocking}",
        )

    # --- the headline case ------------------------------------------------

    def test_contract_approved_pr_satisfies_guardian_with_no_live_approval(self) -> None:
        carry = self.carry()
        self.assertTrue(carry.applies, carry.reasons)
        blocking, warnings = self.guardian_result(carry)
        self.assertEqual(blocking, [], "a valid Contract Approval must satisfy the Guardian requirement")
        self.assertTrue(
            any("Contract Approval" in w for w in warnings),
            f"the satisfying source must be recorded: {warnings}",
        )
        # Control: WITHOUT the contract approval, the very same PR is blocked —
        # proving the carry (not an unrelated pass) is load-bearing.
        self.assertGuardianBlocked(None, "no contract approval at all")

    # --- every negative still leaves the Guardian requirement True --------

    def test_task_absent_from_base_still_requires_guardian(self) -> None:
        carry = self.carry(base=False)
        self.assertFalse(carry.applies)
        self.assertGuardianBlocked(carry, "task absent from base")

    def test_each_governance_field_change_still_requires_guardian(self) -> None:
        for field in _NORMAL_PATH_GOVERNANCE_FIELDS:
            with self.subTest(field=field):
                head = {**BASE_TASK, field: "shiki:contract-tamper-sentinel"}
                carry = contract_approval_for_pr(
                    task_id=TASK_ID,
                    head_task=head,
                    base_shiki=self.base_shiki,
                    changed_files_status=[ChangedFile("M", f".shiki/tasks/{TASK_ID}.json")],
                    registration=valid_registration(),
                )
                self.assertFalse(carry.applies, f"changing {field} must defeat the carry")
                self.assertGuardianBlocked(carry, f"governance field {field} changed")

    def test_registration_missing_still_requires_guardian(self) -> None:
        self.assertGuardianBlocked(self.carry(registration=None), "registration missing")

    def test_registration_merged_false_still_requires_guardian(self) -> None:
        self.assertGuardianBlocked(self.carry(registration=valid_registration(merged=False)), "merged=false")

    def test_registration_guardian_approved_false_still_requires_guardian(self) -> None:
        self.assertGuardianBlocked(
            self.carry(registration=valid_registration(guardian_approved=False)), "guardian_approved=false"
        )

    def test_registration_pr_commit_mismatch_still_requires_guardian(self) -> None:
        self.assertGuardianBlocked(
            self.carry(registration=valid_registration(adding_commit_pr=REG_PR + 1)), "pr/commit mismatch"
        )

    def test_registration_task_id_mismatch_still_requires_guardian(self) -> None:
        self.assertGuardianBlocked(
            self.carry(registration=valid_registration(task_id="T-20260101T000000000000Z-deadbeef")),
            "task_id mismatch",
        )

    # --- flag absent / file missing / unreadable == unchanged behaviour ---

    def test_flag_absent_is_none_and_still_requires_guardian(self) -> None:
        # No --contract-approval => loader returns None => carry is None => the
        # Guardian gate is byte-for-byte what it is today (blocked, no approval).
        self.assertIsNone(load_contract_approval_registration(""))
        self.assertIsNone(load_contract_approval_registration(None))
        carry = contract_approval_for_pr(
            task_id=TASK_ID,
            head_task=BASE_TASK,
            base_shiki=self.base_shiki,
            changed_files_status=[],
            registration=load_contract_approval_registration(""),
        )
        self.assertIsNone(carry)
        self.assertGuardianBlocked(carry, "flag absent")

    def test_missing_and_unreadable_proof_file_is_none(self) -> None:
        root = Path(self._tmp.name)
        missing = root / "does-not-exist.json"
        self.assertIsNone(load_contract_approval_registration(str(missing)))
        unreadable = root / "garbage.json"
        unreadable.write_text("{ this is not valid json", encoding="utf-8")
        self.assertIsNone(load_contract_approval_registration(str(unreadable)))
        # A JSON value that is not an object is likewise "no proof".
        not_object = root / "list.json"
        not_object.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(load_contract_approval_registration(str(not_object)))
        # And each still leaves the Guardian requirement True.
        self.assertGuardianBlocked(
            contract_approval_for_pr(
                task_id=TASK_ID,
                head_task=BASE_TASK,
                base_shiki=self.base_shiki,
                changed_files_status=[],
                registration=load_contract_approval_registration(str(missing)),
            ),
            "missing proof file",
        )

    def test_valid_proof_file_round_trips_through_the_loader(self) -> None:
        # The end-to-end path: a real JSON file on disk, loaded and evaluated,
        # carries the Guardian requirement.
        proof = Path(self._tmp.name) / "contract-approval.json"
        proof.write_text(json.dumps(valid_registration()), encoding="utf-8")
        registration = load_contract_approval_registration(str(proof))
        self.assertIsInstance(registration, dict)
        carry = contract_approval_for_pr(
            task_id=TASK_ID,
            head_task=BASE_TASK,
            base_shiki=self.base_shiki,
            changed_files_status=[],
            registration=registration,
        )
        self.assertTrue(carry.applies, carry.reasons)
        blocking, _ = self.guardian_result(carry)
        self.assertEqual(blocking, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
