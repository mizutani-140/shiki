"""The CCA verdict enforcer must reject short-circuited blocking evaluations.

Observed on PR #179: with one blocking checklist item (CCA-08) already
failing, the CCA judged the verdict "already determined" and stopped, returning
seven acceptance criteria as ``insufficient_evidence`` in 1m30s where an earlier
run had evaluated the same criteria in detail. The verdict value was right and
the evidence record was hollow. Because ``complete`` requires every blocking
criterion to pass, a short-circuited run can never become ``complete`` -- so the
record is systematically thin exactly when a blocker exists, which is most of
the time.

These tests pin the fix: the enforcer must reject a verdict that leaves a
blocking checklist item or acceptance criterion ``insufficient_evidence`` with
an "already blocked" style reason, and must accept a verdict whose blocking
items are all genuinely evaluated regardless of the verdict value. They exercise
the real ``scripts/enforce_cca_verdict.py`` module and never modify it.
"""

from __future__ import annotations

import contextlib
import os
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import enforce_cca_verdict
from shiki_schema import SchemaValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = REPO_ROOT / ".github" / "prompts" / "cca-completion-check.md"


@contextlib.contextmanager
def in_repo_root():
    """``validate_verdict`` loads schemas relative to the working directory."""
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


def base_verdict(**overrides):
    """A schema-valid ``blocked`` verdict whose blocking items are all evaluated.

    One blocking checklist item fails (the real blocker) and every other
    blocking item and acceptance criterion carries a genuine evaluation. This is
    the shape a non-short-circuited run must be able to emit.
    """
    verdict = {
        "verdict": "blocked",
        "summary": "One blocking item fails; the rest are evaluated.",
        "goal_id": "G-20260729T065622761503Z-4479b0d3",
        "task_id": "T-20260729T065622770500Z-513839f0",
        "pr": 179,
        "head_sha": "abc123",
        "can_merge": False,
        "blocking_reason": "CCA-08 Guardian approval is missing.",
        "checklist": [
            {
                "id": "CCA-08",
                "status": "fail",
                "blocking": True,
                "reason": "Guardian approval is required and not recorded.",
            },
            {
                "id": "CCA-01",
                "status": "pass",
                "blocking": True,
                "evidence": "Validate Shiki mirror succeeded for the head SHA.",
            },
            {
                "id": "CCA-05",
                "status": "insufficient_evidence",
                "blocking": True,
                "reason": "The PR body records no TDD command output for the new path.",
            },
        ],
        "acceptance": [
            {
                "criterion": "A1",
                "status": "pass",
                "evidence": ["Enforcer rejects the short-circuited fixture."],
            },
            {
                "criterion": "A2",
                "status": "fail",
                "evidence": ["The prompt still lacked the evaluate-all rule."],
                "reason": "Prompt did not instruct full evaluation.",
            },
        ],
        "mergegate": {"required_checks": "pending"},
        "confidence": 0.9,
    }
    verdict.update(overrides)
    return verdict


# "already blocked" style reasons that must be rejected on a blocking item:
# each blames an already-known blocker / already-determined verdict rather than
# this item's own missing evidence.
SHORT_CIRCUIT_REASONS = (
    "Already blocked, so this was not evaluated.",
    "Not evaluated because CCA-08 already fails.",
    "The verdict is already determined; skipped this item.",
    "already-blocked",
    "Short-circuited: a blocker already exists.",
    "No need to evaluate once a blocker is known.",
)

# Genuine ``insufficient_evidence`` reasons that must NOT be rejected: they name
# the item's own missing evidence rather than pointing at an existing blocker.
# The last four are adversarial near-misses that share a word with a skip or
# already-blocked marker but describe genuine missing evidence, so they must
# stay accepted.
GENUINE_REASONS = (
    "The PR body records no verification command output for this path.",
    "No ledger entry links the required skill evidence for this criterion.",
    "The diff does not show a regression test proving the fixed behavior.",
    "No test covers the new short-circuit branch added to the parser.",
    "The e2e check was skipped due to a missing browser binary; no durable evidence covers this criterion.",
    "This acceptance criterion was not evaluated by any CI job and the PR provides no manual verification.",
    "The lint job is already failing on main, so no result can be attributed to this PR.",
)

# Real short-circuit reasons measured against the enforcer. These are verbatim
# inputs -- paraphrasing a measured reason defeats the measurement -- and every
# one must be rejected. Each is a way a judge phrased "this blocking item was
# left unjudged because a blocker already exists": some are strong standalone
# phrases, some pair a skip cue with an already-blocked cause phrase, and two
# (``Deferred pending resolution of CCA-08`` and ``Blocked by CCA-08, so this
# was left unjudged``) name ANOTHER checklist item's id as the cause with no
# failure word attached -- the most natural way a judge phrases a cross-item
# short-circuit, and the two the marker-only detector missed.
MEASURED_SHORT_CIRCUIT_REASONS = (
    "CCA-08 already blocked; not evaluated",
    "Verdict already determined by CCA-08 failure",
    "No need to evaluate; a blocker already exists",
    "Skipped because another blocker is present",
    "Deferred pending resolution of CCA-08",
    "Not reached: the verdict is blocked upstream",
    "Superseded by the CCA-08 failure",
    "Blocked by CCA-08, so this was left unjudged",
    "Cannot be assessed while the Guardian gate fails",
    "Irrelevant given the failing blocker above",
    "N/A because the PR cannot merge anyway",
    "Halted evaluation after the first blocking failure",
)

# Genuine ``insufficient_evidence`` reasons measured as safe: each names this
# item's own missing evidence and must stay accepted even though some share a
# word with a widened marker ("short-circuit branch", "was skipped"). The last
# two guard the item-id cause rule: ``CCA-06 has no durable artifact`` names a
# DIFFERENT item but carries no skip cue, so the two-signal requirement keeps it
# accepted; the self-referencing reason names the judged item's OWN id (CCA-05,
# the item these reasons are attached to) and must not count as an external
# cause even with a skip cue present.
MEASURED_GENUINE_REASONS = (
    "No test covers the new short-circuit branch.",
    "The e2e check was skipped due to a missing binary.",
    "No CI artifact records the migration run.",
    "The PR body does not state a risk level.",
    "CCA-06 has no durable artifact.",
    "CCA-05 has no durable evidence in the PR, so it stays unjudged.",
)

# Full sets exercised by the detection and acceptance tests below.
ALL_SHORT_CIRCUIT_REASONS = SHORT_CIRCUIT_REASONS + MEASURED_SHORT_CIRCUIT_REASONS
ALL_GENUINE_REASONS = GENUINE_REASONS + MEASURED_GENUINE_REASONS


class ShortCircuitDetection(unittest.TestCase):
    def test_blocking_item_with_already_blocked_reason_is_detected(self):
        for reason in ALL_SHORT_CIRCUIT_REASONS:
            with self.subTest(reason=reason):
                verdict = base_verdict()
                verdict["checklist"][2]["reason"] = reason
                offenders = enforce_cca_verdict.short_circuited_evaluations(verdict)
                self.assertTrue(
                    offenders,
                    f"{reason!r} should be detected as a short-circuited evaluation",
                )
                self.assertTrue(any("CCA-05" in offender for offender in offenders))

    def test_genuine_insufficient_evidence_reason_is_not_flagged(self):
        for reason in ALL_GENUINE_REASONS:
            with self.subTest(reason=reason):
                verdict = base_verdict()
                verdict["checklist"][2]["reason"] = reason
                self.assertEqual(
                    enforce_cca_verdict.short_circuited_evaluations(verdict), []
                )

    def test_no_genuine_reason_is_a_false_positive(self):
        # Explicit zero-false-positive assertion. If a later widening of the
        # markers starts rejecting any genuine reason, this count is non-zero
        # and the suite fails, naming the offending reasons.
        false_positives = []
        for reason in ALL_GENUINE_REASONS:
            verdict = base_verdict()
            verdict["checklist"][2]["reason"] = reason
            if enforce_cca_verdict.short_circuited_evaluations(verdict):
                false_positives.append(reason)
        self.assertEqual(
            false_positives,
            [],
            f"genuine insufficient-evidence reasons were wrongly flagged: {false_positives!r}",
        )

    def test_short_circuited_acceptance_criterion_is_detected(self):
        verdict = base_verdict()
        verdict["acceptance"][1]["status"] = "insufficient_evidence"
        verdict["acceptance"][1]["reason"] = "already blocked, not evaluated"
        offenders = enforce_cca_verdict.short_circuited_evaluations(verdict)
        self.assertTrue(any("A2" in offender for offender in offenders))

    def test_non_blocking_checklist_item_is_not_flagged(self):
        verdict = base_verdict()
        verdict["checklist"].append(
            {
                "id": "CCA-INFO",
                "status": "insufficient_evidence",
                "blocking": False,
                "reason": "already blocked, skipped",
            }
        )
        self.assertEqual(
            enforce_cca_verdict.short_circuited_evaluations(verdict), []
        )


class ValidateVerdictRejection(unittest.TestCase):
    def test_short_circuited_blocking_item_is_rejected(self):
        # Mutation guard: this must fail if the new rejection is removed from
        # validate_verdict.
        verdict = base_verdict()
        verdict["checklist"][2]["reason"] = "Already blocked; not evaluated."
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(verdict)
        self.assertIn("short-circuit", str(caught.exception).lower())

    def test_short_circuited_acceptance_criterion_is_rejected(self):
        verdict = base_verdict()
        verdict["acceptance"][1]["status"] = "insufficient_evidence"
        verdict["acceptance"][1]["reason"] = "verdict already determined"
        with in_repo_root():
            with self.assertRaises(SchemaValidationError):
                enforce_cca_verdict.validate_verdict(verdict)

    def test_measured_short_circuit_reasons_are_rejected(self):
        # Every measured short-circuit reason must fail full enforcement, not
        # only the detection helper.
        for reason in MEASURED_SHORT_CIRCUIT_REASONS:
            with self.subTest(reason=reason):
                verdict = base_verdict()
                verdict["checklist"][2]["reason"] = reason
                with in_repo_root():
                    with self.assertRaises(SchemaValidationError) as caught:
                        enforce_cca_verdict.validate_verdict(verdict)
                self.assertIn("short-circuit", str(caught.exception).lower())

    def test_fully_evaluated_blocked_verdict_is_accepted(self):
        # A real blocker exists (CCA-08 fails) but every blocking item is
        # evaluated. The verdict must pass validation regardless of its value.
        verdict = base_verdict()
        with in_repo_root():
            enforce_cca_verdict.validate_verdict(verdict)

    def test_genuine_insufficient_evidence_verdict_is_accepted(self):
        # Every genuine near-miss reason, including the adversarial ones that
        # share a word with a marker, must survive full enforcement.
        for reason in ALL_GENUINE_REASONS:
            with self.subTest(reason=reason):
                verdict = base_verdict()
                verdict["checklist"][2]["reason"] = reason
                with in_repo_root():
                    enforce_cca_verdict.validate_verdict(verdict)


class PromptInstruction(unittest.TestCase):
    def setUp(self):
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def test_prompt_requires_evaluating_every_blocking_item(self):
        self.assertIn("every blocking checklist item", self.prompt)

    def test_prompt_requires_evaluation_even_when_a_blocker_exists(self):
        lowered = self.prompt.lower()
        self.assertIn("already fails", lowered)

    def test_prompt_forbids_already_blocked_short_circuit(self):
        self.assertIn("already blocked", self.prompt.lower())


if __name__ == "__main__":
    unittest.main()
