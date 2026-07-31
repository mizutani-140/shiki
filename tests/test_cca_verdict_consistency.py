"""A ``complete`` CCA verdict must not contradict its own evidence.

A verdict is self-contradictory when it claims ``complete`` while its evidence
says otherwise: a blocking checklist item that failed, an acceptance criterion
that failed or lacks proof, or a ``can_merge`` flag that is not ``true``. It is
also incomplete when it silently omits a checklist id the task requires (its
``cca_checklist_profile``) or leaves one unresolved.

These tests pin the two enforcement surfaces:

* ``scripts/enforce_cca_verdict.validate_verdict`` — rejects a ``complete``
  verdict whose blocking checklist items or acceptance criteria failed.
* ``scripts/mergegate_check`` — the CCA-branch helpers that promote ``can_merge``
  to a blocking reason and require the task's ``cca_checklist_profile`` to be
  covered by terminal checklist statuses.

They exercise the real modules and never modify them. The
``ValidateVerdictRejection`` cases are also the mutation guard for the
complete-with-failures rejections: reverting either guard in
``enforce_cca_verdict.py`` makes at least one of them fail.
"""

from __future__ import annotations

import contextlib
import os
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import enforce_cca_verdict
import mergegate_check
from shiki_schema import SchemaValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def in_repo_root():
    """``validate_verdict`` loads schemas relative to the working directory."""
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


def complete_verdict(**overrides):
    """A schema-valid ``complete`` verdict whose evidence agrees with the value.

    Every blocking checklist item passes, every acceptance criterion passes, and
    ``can_merge`` is true. This is the shape a genuinely complete run emits; the
    tests mutate one field at a time to make it self-contradictory.
    """
    verdict = {
        "verdict": "complete",
        "summary": "Every criterion passes.",
        "goal_id": "G-20260729T065622761503Z-4479b0d3",
        "task_id": "T-20260729T065622764296Z-48021407",
        "pr": 224,
        "head_sha": "abc123",
        "can_merge": True,
        "checklist": [
            {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "Mirror validated."},
            {"id": "CCA-02", "status": "not_applicable", "blocking": False, "reason": "No migration in scope."},
        ],
        "acceptance": [
            {"criterion": "A1", "status": "pass", "evidence": ["Enforcer rejects the contradictory fixture."]},
        ],
        "mergegate": {"required_checks": "pass"},
        "confidence": 1.0,
        "repair_packet": None,
    }
    verdict.update(overrides)
    return verdict


class ValidateVerdictRejection(unittest.TestCase):
    """``enforce_cca_verdict.validate_verdict`` rejects contradictory ``complete``."""

    def test_complete_with_all_evidence_passing_is_accepted(self):
        with in_repo_root():
            enforce_cca_verdict.validate_verdict(complete_verdict())

    def test_complete_with_failing_acceptance_is_rejected_naming_criterion(self):
        # Mutation guard for the acceptance rejection. A genuinely failed
        # acceptance criterion contradicts a complete verdict; the error names it.
        verdict = complete_verdict(
            acceptance=[
                {"criterion": "A1", "status": "pass", "evidence": ["ok"]},
                {"criterion": "A2-guard-reverted", "status": "fail", "evidence": ["the guard was removed"]},
            ]
        )
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(verdict)
        message = str(caught.exception)
        self.assertIn("acceptance", message.lower())
        self.assertIn("A2-guard-reverted", message)

    def test_complete_with_insufficient_acceptance_is_rejected(self):
        # An acceptance criterion left unproven also contradicts complete. The
        # reason names the criterion's own missing evidence (not an "already
        # blocked" short-circuit), so ONLY the new acceptance guard can catch it.
        verdict = complete_verdict(
            acceptance=[
                {
                    "criterion": "A3",
                    "status": "insufficient_evidence",
                    "evidence": [],
                    "reason": "The PR body records no verification output for this criterion.",
                },
            ]
        )
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(verdict)
        self.assertIn("A3", str(caught.exception))

    def test_complete_with_failing_blocking_checklist_item_is_rejected(self):
        # Mutation guard for the checklist rejection. The failing item carries a
        # genuine reason so the short-circuit detector does not fire; only the
        # complete-with-blocking-failure guard can reject it.
        verdict = complete_verdict(
            checklist=[
                {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "ok"},
                {
                    "id": "CCA-09",
                    "status": "fail",
                    "blocking": True,
                    "reason": "The required guardian approval is not recorded in the PR.",
                },
            ]
        )
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(verdict)
        message = str(caught.exception)
        self.assertIn("checklist", message.lower())
        self.assertIn("CCA-09", message)


class FailingAcceptanceCriteria(unittest.TestCase):
    """The ``failing_acceptance_criteria`` helper (shared shape in both modules)."""

    def test_reports_fail_and_insufficient_evidence(self):
        verdict = {
            "acceptance": [
                {"criterion": "ok", "status": "pass", "evidence": ["e"]},
                {"criterion": "broke", "status": "fail", "evidence": ["e"]},
                {"criterion": "unproven", "status": "insufficient_evidence", "reason": "no evidence"},
                {"criterion": "excluded", "status": "not_applicable", "reason": "n/a"},
            ]
        }
        for module in (enforce_cca_verdict, mergegate_check):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.failing_acceptance_criteria(verdict), ["broke", "unproven"])

    def test_all_passing_yields_no_failures(self):
        verdict = {"acceptance": [{"criterion": "a", "status": "pass", "evidence": ["e"]}]}
        for module in (enforce_cca_verdict, mergegate_check):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.failing_acceptance_criteria(verdict), [])


class ChecklistProfileCoverage(unittest.TestCase):
    """``mergegate_check.checklist_profile_coverage_failures``: every required id
    must appear in the verdict checklist judged to a terminal status."""

    def _verdict(self, checklist):
        return {"checklist": checklist}

    def test_every_profile_id_present_and_pass_has_no_failures(self):
        task = {"cca_checklist_profile": ["PR", "TDD", "V", "CCA"]}
        verdict = self._verdict([{"id": pid, "status": "pass", "blocking": True} for pid in task["cca_checklist_profile"]])
        self.assertEqual(mergegate_check.checklist_profile_coverage_failures(task, verdict), [])

    def test_not_applicable_is_terminal(self):
        task = {"cca_checklist_profile": ["PR"]}
        verdict = self._verdict([{"id": "PR", "status": "not_applicable", "blocking": False}])
        self.assertEqual(mergegate_check.checklist_profile_coverage_failures(task, verdict), [])

    def test_missing_profile_id_is_reported(self):
        task = {"cca_checklist_profile": ["PR", "TDD"]}
        verdict = self._verdict([{"id": "PR", "status": "pass", "blocking": True}])
        failures = mergegate_check.checklist_profile_coverage_failures(task, verdict)
        self.assertTrue(any("TDD" in reason and "missing" in reason for reason in failures), failures)

    def test_empty_checklist_reports_all_ids(self):
        task = {"cca_checklist_profile": ["PR", "TDD"]}
        failures = mergegate_check.checklist_profile_coverage_failures(task, self._verdict([]))
        self.assertEqual(len(failures), 2)

    def test_insufficient_evidence_is_not_terminal(self):
        task = {"cca_checklist_profile": ["PR"]}
        verdict = self._verdict([{"id": "PR", "status": "insufficient_evidence", "blocking": True}])
        failures = mergegate_check.checklist_profile_coverage_failures(task, verdict)
        self.assertTrue(any("non-terminal" in reason for reason in failures), failures)

    def test_no_profile_imposes_no_requirement(self):
        verdict = self._verdict([])
        self.assertEqual(mergegate_check.checklist_profile_coverage_failures({}, verdict), [])
        self.assertEqual(mergegate_check.checklist_profile_coverage_failures({"cca_checklist_profile": []}, verdict), [])
        self.assertEqual(mergegate_check.checklist_profile_coverage_failures(None, verdict), [])


if __name__ == "__main__":
    unittest.main()
