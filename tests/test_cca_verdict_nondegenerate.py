"""A CCA verdict that judged nothing is invalid at every layer.

Symptom (PR #233): the CCA returned a verdict with zero checklist entries and
zero acceptance entries -- it judged nothing -- and the verdict was
schema-VALID. It slipped past enforcement and was caught only by a downstream
MergeGate rule ("CCA verdict acceptance evidence is empty"). A verdict that
judged nothing should never have been representable.

These tests pin the fix at the three layers that must never drift apart:

* ``.shiki/schemas/cca-verdict.schema.json`` -- ``minItems: 1`` on ``checklist``
  and ``acceptance`` so an empty array is schema-INVALID for every reader.
* ``scripts/enforce_cca_verdict.validate_verdict`` -- an explicit non-degeneracy
  check that runs BEFORE schema validation and rejects an empty ``checklist`` or
  ``acceptance`` with a message naming which array the judge left empty (the
  schema's generic "array length must be >= 1" names neither the array nor the
  judgment it stands for).
* ``.github/workflows/shiki-cca-completion.yml`` -- the inline ``--json-schema``
  transport schema that constrains what the model may emit at generation time
  carries the same ``minItems`` AND mirrors the repository schema's checklist
  and acceptance item shapes, so the two cannot drift and a degenerate or
  malformed verdict cannot be generated in the first place.

They exercise the real modules, schema, and workflow and never modify any of them.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import enforce_cca_verdict
from shiki_schema import SchemaValidationError, validate_instance

REPO_ROOT = Path(__file__).resolve().parents[1]
VERDICT_SCHEMA_PATH = REPO_ROOT / ".shiki" / "schemas" / "cca-verdict.schema.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "shiki-cca-completion.yml"

HEAD_SHA_ENV = "SHIKI_HEAD_SHA"
REPORTED_KEY = "head_sha_reported"


@contextlib.contextmanager
def in_repo_root():
    """``validate_verdict`` loads the schema relative to the working directory."""
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def env(**overrides):
    """Set (value) or clear (``None``) env vars for the block, then restore."""
    saved = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def verdict(**overrides):
    """A schema-valid, non-degenerate verdict: one checklist item, one criterion.

    This is the minimum shape a genuine judgment emits. Tests mutate one field at
    a time (empty an array, fail an item) to drive each rejection.
    """
    base = {
        "verdict": "complete",
        "summary": "Every criterion passes.",
        "goal_id": "G-20260801T041653272590Z-81f6f71d",
        "task_id": "T-20260801T041653274008Z-54d3477c",
        "pr": 233,
        "head_sha": "a" * 40,
        "can_merge": True,
        "checklist": [
            {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "Mirror validated."},
        ],
        "acceptance": [
            {"criterion": "A1", "status": "pass", "evidence": ["The array carries minItems: 1."]},
        ],
        "mergegate": {"required_checks": "pass"},
        "confidence": 1.0,
        "repair_packet": None,
    }
    base.update(overrides)
    return base


# The exact degenerate shape observed on PR #233: a full, otherwise schema-valid
# verdict whose judgment arrays are both empty -- it judged nothing.
PR_233_DEGENERATE = {
    "verdict": "complete",
    "summary": "Completion judged.",
    "goal_id": "G-20260729T065622761503Z-4479b0d3",
    "task_id": "T-20260729T065622768738Z-fa978e4a",
    "pr": 233,
    "head_sha": "b" * 40,
    "can_merge": True,
    "checklist": [],
    "acceptance": [],
    "mergegate": {"required_checks": "pass"},
    "confidence": 1.0,
}


class NonDegeneracyEnforcement(unittest.TestCase):
    """``enforce_cca_verdict.validate_verdict`` rejects an empty judgment array."""

    def test_empty_checklist_is_rejected_naming_checklist(self):
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(verdict(checklist=[]))
        message = str(caught.exception)
        self.assertIn("checklist", message.lower())
        self.assertNotIn("acceptance", message.lower())

    def test_empty_acceptance_is_rejected_naming_acceptance(self):
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(verdict(acceptance=[]))
        message = str(caught.exception)
        self.assertIn("acceptance", message.lower())
        self.assertNotIn("checklist", message.lower())

    def test_both_empty_is_rejected(self):
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(verdict(checklist=[], acceptance=[]))
        # Both are empty; the message names at least one of the two arrays.
        message = str(caught.exception).lower()
        self.assertTrue("checklist" in message or "acceptance" in message, message)

    def test_pr_233_degenerate_payload_is_rejected(self):
        # The exact shape that slipped through on PR #233 must now be rejected.
        with in_repo_root():
            with self.assertRaises(SchemaValidationError):
                enforce_cca_verdict.validate_verdict(dict(PR_233_DEGENERATE))

    def test_single_entry_checklist_and_acceptance_is_accepted(self):
        # A verdict that judged exactly one checklist item and one acceptance
        # criterion is the minimum genuine judgment and must pass.
        with in_repo_root():
            enforce_cca_verdict.validate_verdict(verdict())

    def test_missing_checklist_still_reports_missing_required_property(self):
        # A MISSING array is a different fault than a present-but-empty one; the
        # non-degeneracy check must NOT preempt the schema's "missing required
        # property" message for it.
        degenerate = verdict()
        degenerate.pop("checklist")
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(degenerate)
        self.assertIn("missing required property", str(caught.exception).lower())

    def test_degenerate_check_runs_before_schema_validation(self):
        # The explicit named check fires without touching the schema on disk: it
        # returns the array name for a present-but-empty list and nothing for a
        # missing one.
        self.assertEqual(enforce_cca_verdict.degenerate_judgment_arrays(verdict(checklist=[])), ["checklist"])
        self.assertEqual(enforce_cca_verdict.degenerate_judgment_arrays(verdict(acceptance=[])), ["acceptance"])
        self.assertEqual(
            enforce_cca_verdict.degenerate_judgment_arrays(verdict(checklist=[], acceptance=[])),
            ["checklist", "acceptance"],
        )
        self.assertEqual(enforce_cca_verdict.degenerate_judgment_arrays(verdict()), [])
        missing = verdict()
        missing.pop("acceptance")
        self.assertEqual(enforce_cca_verdict.degenerate_judgment_arrays(missing), [])


class SchemaRejectsDegenerate(unittest.TestCase):
    """The repository schema itself rejects an empty judgment array (minItems)."""

    def setUp(self):
        self.schema = json.loads(VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_min_items_present_on_both_arrays(self):
        self.assertEqual(self.schema["properties"]["checklist"].get("minItems"), 1)
        self.assertEqual(self.schema["properties"]["acceptance"].get("minItems"), 1)

    def test_schema_rejects_empty_checklist(self):
        with self.assertRaises(SchemaValidationError):
            validate_instance(verdict(checklist=[]), self.schema)

    def test_schema_rejects_empty_acceptance(self):
        with self.assertRaises(SchemaValidationError):
            validate_instance(verdict(acceptance=[]), self.schema)

    def test_schema_rejects_pr_233_payload(self):
        with self.assertRaises(SchemaValidationError):
            validate_instance(dict(PR_233_DEGENERATE), self.schema)

    def test_schema_accepts_single_entry_arrays(self):
        validate_instance(verdict(), self.schema)


class ExistingEnforcementPreserved(unittest.TestCase):
    """Every existing enforcement still behaves on a NON-degenerate verdict.

    The non-degeneracy check must not shadow the older guards; each is exercised
    on a verdict whose arrays are both non-empty so it reaches the guard it pins.
    """

    def test_complete_with_failing_blocking_checklist_item_is_rejected(self):
        offending = verdict(
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
                enforce_cca_verdict.validate_verdict(offending)
        message = str(caught.exception)
        self.assertIn("checklist", message.lower())
        self.assertIn("CCA-09", message)

    def test_complete_with_failing_acceptance_is_rejected(self):
        offending = verdict(
            acceptance=[
                {"criterion": "A1", "status": "pass", "evidence": ["ok"]},
                {"criterion": "A2", "status": "fail", "evidence": ["regressed"]},
            ]
        )
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(offending)
        message = str(caught.exception)
        self.assertIn("acceptance", message.lower())
        self.assertIn("A2", message)

    def test_short_circuited_evaluation_is_rejected(self):
        # A blocking item left insufficient_evidence because "already blocked"
        # (not because its own evidence is missing) short-circuits the judgment.
        offending = verdict(
            verdict="blocked",
            can_merge=False,
            checklist=[
                {"id": "CCA-08", "status": "fail", "blocking": True, "reason": "guardian approval is not recorded"},
                {"id": "CCA-05", "status": "insufficient_evidence", "blocking": True, "reason": "already blocked; not evaluated"},
            ],
        )
        with in_repo_root():
            with self.assertRaises(SchemaValidationError) as caught:
                enforce_cca_verdict.validate_verdict(offending)
        self.assertIn("short-circuit", str(caught.exception).lower())

    def test_fully_evaluated_blocked_verdict_still_validates(self):
        # A blocked verdict whose blocking items are each judged on their own
        # evidence is well-formed; validation must not reject it.
        good = verdict(
            verdict="blocked",
            can_merge=False,
            checklist=[
                {"id": "CCA-08", "status": "fail", "blocking": True, "reason": "guardian approval is not recorded"},
                {"id": "CCA-05", "status": "insufficient_evidence", "blocking": True, "reason": "the PR body records no TDD command output"},
            ],
        )
        with in_repo_root():
            enforce_cca_verdict.validate_verdict(good)

    def test_head_sha_injection_still_replaces_mismatched_value(self):
        authoritative = "c" * 40
        model_value = "d" * 40
        candidate = verdict(head_sha=model_value)
        stderr = io.StringIO()
        with env(**{HEAD_SHA_ENV: authoritative}):
            with contextlib.redirect_stderr(stderr):
                result = enforce_cca_verdict.inject_authoritative_head_sha(candidate)
        self.assertEqual(result["head_sha"], authoritative)
        self.assertEqual(result[REPORTED_KEY], model_value)
        self.assertIn(authoritative, stderr.getvalue())
        self.assertIn(model_value, stderr.getvalue())


class TransportSchemaMirror(unittest.TestCase):
    """The workflow transport schema mirrors the repository schema for item shapes.

    The inline ``--json-schema`` transport schema constrains what the model may
    emit at generation time; the repository schema is what every reader validates
    against afterwards. When they disagree on the checklist/acceptance item shape
    the judge can generate a document the enforcer then rejects -- six distinct
    shape violations reached production this way. These tests pin the two schemas
    to agree so they cannot drift again.
    """

    def setUp(self):
        self.repo = json.loads(VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.transport = self._extract_transport_schema()

    def _extract_transport_schema(self) -> dict:
        """Extract the JSON written to ``cca-transport.schema.json`` in the workflow.

        The schema lives in a ``cat > ... <<'JSON' ... JSON`` heredoc inside the
        workflow's ``run: |`` block. The lines between the markers are valid JSON
        once joined (JSON ignores each line's leading indentation).
        """
        lines = WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
        start = next(
            (i for i, line in enumerate(lines) if "cca-transport.schema.json" in line and "<<'JSON'" in line),
            None,
        )
        self.assertIsNotNone(start, "transport schema heredoc not found in the workflow")
        body = []
        for line in lines[start + 1:]:
            if line.strip() == "JSON":
                break
            body.append(line)
        return json.loads("\n".join(body))

    def _item(self, schema: dict, array: str) -> dict:
        return schema["properties"][array]["items"]

    def test_both_schemas_require_min_items_on_both_arrays(self):
        for schema, label in ((self.repo, "repo"), (self.transport, "transport")):
            for array in ("checklist", "acceptance"):
                self.assertEqual(
                    schema["properties"][array].get("minItems"), 1,
                    f"{label} schema {array} must carry minItems: 1",
                )

    def test_checklist_item_shape_agrees(self):
        repo_item = self._item(self.repo, "checklist")
        transport_item = self._item(self.transport, "checklist")
        self.assertEqual(set(transport_item.get("required", [])), set(repo_item.get("required", [])))
        self.assertEqual(set(transport_item["required"]), {"id", "status", "blocking"})
        for field in ("id", "status", "blocking"):
            self.assertEqual(
                transport_item["properties"][field].get("type"),
                repo_item["properties"][field].get("type"),
                f"checklist item field {field!r} type disagrees",
            )
        self.assertEqual(
            transport_item["properties"]["status"].get("enum"),
            repo_item["properties"]["status"].get("enum"),
        )

    def test_acceptance_item_shape_agrees(self):
        repo_item = self._item(self.repo, "acceptance")
        transport_item = self._item(self.transport, "acceptance")
        self.assertEqual(set(transport_item.get("required", [])), set(repo_item.get("required", [])))
        self.assertEqual(set(transport_item["required"]), {"criterion", "status", "evidence"})
        for field in ("criterion", "status", "evidence"):
            self.assertEqual(
                transport_item["properties"][field].get("type"),
                repo_item["properties"][field].get("type"),
                f"acceptance item field {field!r} type disagrees",
            )
        self.assertEqual(
            transport_item["properties"]["status"].get("enum"),
            repo_item["properties"]["status"].get("enum"),
        )
        # evidence is an array of non-empty strings in both schemas.
        self.assertEqual(
            transport_item["properties"]["evidence"]["items"],
            repo_item["properties"]["evidence"]["items"],
        )

    def test_transport_schema_is_well_formed_json_object(self):
        # A regression guard on the extraction itself: if the heredoc stops being
        # valid JSON, every mirror assertion above is meaningless.
        self.assertIsInstance(self.transport, dict)
        self.assertEqual(self.transport.get("type"), "object")


if __name__ == "__main__":
    unittest.main()
