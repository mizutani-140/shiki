"""An unusable CCA generation is regenerated once; a real answer never is.

Symptom (PR #233): the same PR, judged three times in a row, failed three
different ways. The CCA step's structured output was consumed exactly once, with
no classification between generation and enforcement, so an unusable *generation*
(absent output, non-JSON, or a schema-valid-but-empty degenerate verdict that
judged nothing) was indistinguishable from a real negative verdict. The only
recovery was a full manual ``gh run rerun`` outside the run -- and
``gh run rerun --failed`` reused the stale artifact and reproduced the identical
degenerate verdict, so the cheaper-looking recovery could not work.

These tests pin the fix at two layers:

* ``scripts/cca_verdict_usable.verdict_usability`` -- a PURE classifier that
  buckets a structured output into ``usable``/``absent``/``unparseable``/
  ``degenerate`` with a reason. Every real verdict value -- ``complete`` and
  every negative value alike -- is ``usable`` and is never regenerated;
  regenerating a real negative verdict until it changes would be judge-shopping.
* ``.github/workflows/shiki-cca-completion.yml`` -- a classification step between
  ``Run CCA`` and ``Enforce CCA verdict`` that regenerates the CCA verdict
  exactly once, and only when the first generation is absent, unparseable or
  degenerate, leaving artifact names, the evidence manifest and the Guardian
  blockers comment step unchanged.

They exercise the real module and workflow and never modify either.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import cca_verdict_usable
from cca_verdict_usable import (
    ABSENT,
    CLASSIFICATIONS,
    DEGENERATE,
    UNPARSEABLE,
    USABLE,
    verdict_usability,
)
from shiki_workflows import load_workflow_model, workflow_jobs

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "cca_verdict_usable.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "shiki-cca-completion.yml"

# The five real verdict values. Each is a genuine answer of the CCA; NONE of them
# may ever be regenerated, or the retry becomes a way to shop for a different
# judgment. ``complete`` is separated out below only for readability.
NEGATIVE_VERDICTS = ("repair_required", "needs_guardian", "blocked", "insufficient_evidence")


def verdict(**overrides):
    """A well-formed, non-degenerate verdict: one checklist item, one criterion.

    This is the minimum shape a genuine judgment emits. Tests override ``verdict``
    or empty an array to drive each classification.
    """
    base = {
        "verdict": "complete",
        "summary": "Every criterion passes.",
        "goal_id": "G-20260801T041653272590Z-81f6f71d",
        "task_id": "T-20260801T041653274543Z-6df3af7e",
        "pr": 233,
        "head_sha": "a" * 40,
        "can_merge": True,
        "checklist": [
            {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "Mirror validated."},
        ],
        "acceptance": [
            {"criterion": "A1", "status": "pass", "evidence": ["The array carries one entry."]},
        ],
        "mergegate": {"required_checks": "pass"},
        "confidence": 1.0,
        "repair_packet": None,
    }
    base.update(overrides)
    return base


class ClassifierContract(unittest.TestCase):
    """``verdict_usability`` is pure and returns a known class plus a reason."""

    def test_returns_a_known_class_and_nonempty_reason(self):
        classification, reason = verdict_usability(json.dumps(verdict()))
        self.assertIn(classification, CLASSIFICATIONS)
        self.assertTrue(reason.strip(), "every classification must carry a reason")

    def test_is_pure_and_does_not_mutate_a_dict_input(self):
        candidate = verdict()
        snapshot = json.loads(json.dumps(candidate))
        verdict_usability(candidate)
        self.assertEqual(candidate, snapshot, "classifier must not mutate its input")


class AbsentOutput(unittest.TestCase):
    """Nothing was emitted -> ``absent`` (regenerated once)."""

    def test_empty_string_is_absent(self):
        classification, reason = verdict_usability("")
        self.assertEqual(classification, ABSENT)
        self.assertIn("absent", reason.lower())

    def test_whitespace_only_is_absent(self):
        classification, _ = verdict_usability("   \n\t  ")
        self.assertEqual(classification, ABSENT)

    def test_none_is_absent(self):
        classification, _ = verdict_usability(None)
        self.assertEqual(classification, ABSENT)


class UnparseableOutput(unittest.TestCase):
    """Not a verdict object -> ``unparseable`` (regenerated once)."""

    def test_non_json_is_unparseable(self):
        classification, reason = verdict_usability("{not: valid json")
        self.assertEqual(classification, UNPARSEABLE)
        self.assertIn("json", reason.lower())

    def test_truncated_json_is_unparseable(self):
        # A verdict cut off mid-emission (a real failure mode for large outputs).
        classification, _ = verdict_usability('{"verdict": "complete", "checklist": [')
        self.assertEqual(classification, UNPARSEABLE)

    def test_json_array_is_unparseable(self):
        # Valid JSON, but a verdict must be an object; an array cannot be enforced.
        classification, reason = verdict_usability("[]")
        self.assertEqual(classification, UNPARSEABLE)
        self.assertIn("object", reason.lower())

    def test_json_scalar_is_unparseable(self):
        classification, _ = verdict_usability('"a bare string"')
        self.assertEqual(classification, UNPARSEABLE)


class DegenerateOutput(unittest.TestCase):
    """Schema-valid in shape but judging nothing -> ``degenerate`` (regenerated once)."""

    def test_empty_checklist_is_degenerate_naming_checklist(self):
        classification, reason = verdict_usability(json.dumps(verdict(checklist=[])))
        self.assertEqual(classification, DEGENERATE)
        self.assertIn("checklist", reason.lower())
        self.assertNotIn("acceptance", reason.lower())

    def test_empty_acceptance_is_degenerate_naming_acceptance(self):
        classification, reason = verdict_usability(json.dumps(verdict(acceptance=[])))
        self.assertEqual(classification, DEGENERATE)
        self.assertIn("acceptance", reason.lower())
        self.assertNotIn("checklist", reason.lower())

    def test_both_empty_is_degenerate(self):
        classification, reason = verdict_usability(json.dumps(verdict(checklist=[], acceptance=[])))
        self.assertEqual(classification, DEGENERATE)
        self.assertIn("checklist", reason.lower())
        self.assertIn("acceptance", reason.lower())

    def test_pr_233_degenerate_shape_is_degenerate(self):
        # The exact shape observed on PR #233: otherwise-valid verdict, both
        # judgment arrays empty -- it judged nothing.
        pr_233 = {
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
        classification, _ = verdict_usability(json.dumps(pr_233))
        self.assertEqual(classification, DEGENERATE)


class UsableOutput(unittest.TestCase):
    """A real answer of ANY verdict value is ``usable`` and never regenerated."""

    def test_well_formed_complete_is_usable(self):
        classification, reason = verdict_usability(json.dumps(verdict()))
        self.assertEqual(classification, USABLE)
        self.assertIn("complete", reason.lower())

    def test_each_negative_verdict_value_is_usable(self):
        # This is the case that matters most: a well-formed repair_required,
        # needs_guardian, blocked and insufficient_evidence are each usable, so no
        # real answer is ever retried (retrying one until it flips is judge-shopping).
        for value in NEGATIVE_VERDICTS:
            with self.subTest(verdict=value):
                candidate = verdict(verdict=value, can_merge=False)
                classification, _ = verdict_usability(json.dumps(candidate))
                self.assertEqual(
                    classification, USABLE,
                    f"a well-formed {value} verdict must be usable, never regenerated",
                )

    def test_non_empty_arrays_are_usable_even_with_failing_items(self):
        # A negative verdict naturally carries failing items; a non-empty judgment
        # array is a real judgment and must stay usable.
        candidate = verdict(
            verdict="repair_required",
            can_merge=False,
            checklist=[{"id": "CCA-09", "status": "fail", "blocking": True, "reason": "no guardian approval"}],
            acceptance=[{"criterion": "A1", "status": "fail", "evidence": ["regressed"]}],
        )
        classification, _ = verdict_usability(json.dumps(candidate))
        self.assertEqual(classification, USABLE)


class CommandLineInterface(unittest.TestCase):
    """The CLI the workflow calls reads STRUCTURED_OUTPUT and prints JSON."""

    def _run(self, structured_output: str) -> dict:
        env = {**os.environ, "STRUCTURED_OUTPUT": structured_output}
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_cli_classifies_a_usable_verdict(self):
        payload = self._run(json.dumps(verdict()))
        self.assertEqual(payload["classification"], USABLE)
        self.assertIn("reason", payload)

    def test_cli_classifies_a_degenerate_verdict(self):
        payload = self._run(json.dumps(verdict(checklist=[], acceptance=[])))
        self.assertEqual(payload["classification"], DEGENERATE)

    def test_cli_classifies_empty_output_as_absent(self):
        payload = self._run("")
        self.assertEqual(payload["classification"], ABSENT)


class WorkflowClassifyAndRetryWiring(unittest.TestCase):
    """The workflow classifies between Run CCA and Enforce, and retries at most once."""

    CCA_ACTION = "anthropics/claude-code-action@v1"
    RETRY_GUARD = "steps.classify.outputs.classification != 'usable'"

    def setUp(self):
        self.model = load_workflow_model(WORKFLOW_PATH)
        job = workflow_jobs(self.model).get("cca")
        self.assertIsInstance(job, dict, "cca job must exist")
        self.steps = [s for s in job.get("steps", []) if isinstance(s, dict)]
        self.names = [s.get("name") for s in self.steps]
        self.raw = WORKFLOW_PATH.read_text(encoding="utf-8")

    def _index(self, name: str) -> int:
        self.assertIn(name, self.names, f"step {name!r} must exist in the cca job")
        return self.names.index(name)

    def test_classification_runs_between_run_cca_and_enforce(self):
        run_cca = self._index("Run CCA")
        enforce = self._index("Enforce CCA verdict")
        # Exactly one step invokes the classifier, and it sits between the two.
        classify_steps = [
            i for i, s in enumerate(self.steps)
            if isinstance(s.get("run"), str) and "scripts/cca_verdict_usable.py" in s["run"]
        ]
        self.assertEqual(len(classify_steps), 1, "exactly one classification step")
        self.assertLess(run_cca, classify_steps[0])
        self.assertLess(classify_steps[0], enforce)

    def test_exactly_one_retry_of_the_cca_step(self):
        cca_uses = [s for s in self.steps if s.get("uses") == self.CCA_ACTION]
        # The first generation plus exactly one retry -- never a third.
        self.assertEqual(len(cca_uses), 2, "the CCA step is generated once and retried at most once")

    def test_retry_is_guarded_only_by_the_unusable_classification(self):
        retry = [s for s in self.steps if s.get("uses") == self.CCA_ACTION and s.get("if")]
        self.assertEqual(len(retry), 1, "exactly one CCA step carries a retry guard")
        guard = retry[0]["if"]
        # The retry fires only when the first generation is NOT usable -- i.e. only
        # for absent/unparseable/degenerate. A real answer (any verdict value) is
        # usable and never triggers it.
        self.assertIn(self.RETRY_GUARD, guard)
        # The guard must not be gated on the event name, so the retry is reachable
        # from workflow_dispatch and pull_request alike.
        self.assertNotIn("event_name", guard)

    def test_the_first_cca_step_is_unconditional(self):
        first = next(s for s in self.steps if s.get("uses") == self.CCA_ACTION)
        self.assertIsNone(first.get("if"), "the first CCA generation must always run")

    def test_enforce_consumes_the_retry_output_when_first_is_unusable(self):
        enforce = self.steps[self._index("Enforce CCA verdict")]
        structured = str(enforce.get("env", {}).get("STRUCTURED_OUTPUT", ""))
        # Enforcement reads the retry output when the first was unusable and the
        # first output when it was usable.
        self.assertIn("steps.classify.outputs.classification", structured)
        self.assertIn("steps.cca_retry.outputs.structured_output", structured)
        self.assertIn("steps.cca.outputs.structured_output", structured)

    def test_retry_reachable_from_both_triggers(self):
        # Both triggers are declared; combined with the event-name-free retry guard
        # (asserted above) the retry path is reachable from each.
        self.assertRegex(self.raw, r"\n\s*pull_request:")
        self.assertRegex(self.raw, r"\n\s*workflow_dispatch:")

    def test_retry_decision_is_echoed_to_the_log(self):
        classify = next(
            s for s in self.steps
            if isinstance(s.get("run"), str) and "scripts/cca_verdict_usable.py" in s["run"]
        )
        run = classify["run"]
        # The decision (and its reason) is written to the job log so an operator
        # can see the retry happened.
        self.assertIn("classification", run)
        self.assertIn("$GITHUB_OUTPUT", run)

    def test_artifact_names_and_downstream_steps_unchanged(self):
        # The retry must not disturb the evidence surface MergeGate consumes.
        self.assertIn("name: shiki-cca-evidence", self.raw)
        self.assertIn("Build CCA evidence manifest", self.names)
        self.assertIn("Post Guardian blockers comment", self.names)


if __name__ == "__main__":
    unittest.main()
