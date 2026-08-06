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
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import enforce_cca_verdict
import mergegate_check
import shiki_loop
import validate_shiki
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


class TaskSchemaRequiredConsistency(unittest.TestCase):
    """``task.schema.json`` ``required`` and ``validate_shiki.TASK_REQUIRED`` must agree.

    The two surfaces encode the same contract — which fields a task record must
    carry — and must never disagree about ``cca_checklist_profile``. When they
    do, a field is required by one surface and optional by the other, so no task
    file can satisfy both: exactly the self-contradiction this task removed. The
    schema once listed ``cca_checklist_profile`` in ``required`` while
    ``TASK_REQUIRED`` deliberately omitted it (promoting it to required would
    force backfilling every pre-existing task file, which is out of this task's
    locks). These tests fail if that divergence is ever re-introduced by
    promoting the field in one surface without the other.
    """

    def _schema_required(self) -> list[str]:
        schema = json.loads((REPO_ROOT / ".shiki/schemas/task.schema.json").read_text())
        return schema["required"]

    def test_schema_required_and_task_required_agree_on_cca_checklist_profile(self):
        # The named landmine. cca_checklist_profile must be required by both
        # surfaces or neither; requiring it in the schema alone plants a
        # constraint no existing task file — including this task's own record —
        # can satisfy, because TASK_REQUIRED (and the backfill it would need) is
        # out of scope here.
        schema_required = self._schema_required()
        self.assertEqual(
            "cca_checklist_profile" in schema_required,
            "cca_checklist_profile" in validate_shiki.TASK_REQUIRED,
            "task.schema.json `required` and validate_shiki.TASK_REQUIRED disagree "
            "about cca_checklist_profile; promote it in both surfaces (after "
            "backfilling every task file) or neither.",
        )

    def test_schema_required_matches_task_required_field_set(self):
        # Stronger guard covering the same class of divergence for any field:
        # the two surfaces must describe the identical required-field set.
        schema_required = self._schema_required()
        self.assertEqual(
            set(schema_required),
            set(validate_shiki.TASK_REQUIRED),
            "task.schema.json `required` and validate_shiki.TASK_REQUIRED must "
            "list the same required task fields.",
        )


@contextlib.contextmanager
def _enforcer_env(**overrides):
    """Set the enforcer's env vars for one run, restoring the prior state after.

    ``SHIKI_HEAD_SHA`` is cleared unless overridden so the fixtures' head_sha is
    used verbatim; ``STRUCTURED_OUTPUT`` / ``CCA_VERDICT_FILE`` come from callers.
    """
    keys = ["STRUCTURED_OUTPUT", "CCA_VERDICT_FILE", "SHIKI_HEAD_SHA"]
    previous = {key: os.environ.get(key) for key in keys}
    os.environ.pop("SHIKI_HEAD_SHA", None)
    for key, value in overrides.items():
        os.environ[key] = value
    try:
        yield
    finally:
        for key in keys:
            if previous[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous[key]


def _run_enforcer(structured_output: str, tmp_dir: Path):
    """Run ``enforce_cca_verdict.main()`` in-process over ``structured_output``.

    Returns ``(exit_code, combined_output, verdict_file, refusal_file)`` where the
    two paths are the operative-authority path and its derived refusal-record
    sibling under ``tmp_dir``.
    """
    verdict_file = tmp_dir / "cca-verdict.json"
    refusal_file = enforce_cca_verdict.refusal_record_path(verdict_file)
    out, err = io.StringIO(), io.StringIO()
    with in_repo_root(), _enforcer_env(
        STRUCTURED_OUTPUT=structured_output, CCA_VERDICT_FILE=str(verdict_file)
    ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = enforce_cca_verdict.main()
    return code, out.getvalue() + err.getvalue(), verdict_file, refusal_file


def _complete_json(**overrides) -> str:
    return json.dumps(complete_verdict(**overrides))


class RefusalRecordTests(unittest.TestCase):
    """A rejected verdict leaves a readable refusal record on a NON-authority path;
    a valid verdict still writes ``cca-verdict.json`` and no refusal record."""

    def test_rejected_verdict_writes_refusal_record_not_cca_verdict_json(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, verdict_file, refusal_file = _run_enforcer(
                _complete_json(
                    checklist=[
                        {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "ok"},
                        {"id": "CCA-09", "status": "fail", "blocking": True, "reason": "guardian approval not recorded"},
                    ]
                ),
                Path(d),
            )
            self.assertEqual(code, 1)  # the check stays red
            self.assertFalse(verdict_file.exists(), "a refused verdict must never write cca-verdict.json")
            self.assertTrue(refusal_file.exists(), "a refused verdict must write the refusal record")
            self.assertNotEqual(refusal_file, verdict_file)
            self.assertNotEqual(refusal_file.name, "cca-verdict.json")

    def test_refusal_record_names_the_rule_and_offending_ids(self):
        with tempfile.TemporaryDirectory() as d:
            _, _, _, refusal_file = _run_enforcer(
                _complete_json(
                    checklist=[
                        {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "ok"},
                        {"id": "CCA-09", "status": "fail", "blocking": True, "reason": "guardian approval not recorded"},
                    ]
                ),
                Path(d),
            )
            record = json.loads(refusal_file.read_text())
        self.assertEqual(record["kind"], enforce_cca_verdict.REFUSAL_RECORD_KIND)
        self.assertIn("blocking failed checklist items", record["rule_violated"])
        self.assertEqual(record["offending_ids"], ["CCA-09"])
        # The verdict as received is recorded so a diagnosis can read what the judge emitted.
        self.assertEqual(record["verdict_as_received"]["verdict"], "complete")

    def test_valid_verdict_writes_cca_verdict_json_and_no_refusal_record(self):
        with tempfile.TemporaryDirectory() as d:
            code, output, verdict_file, refusal_file = _run_enforcer(_complete_json(), Path(d))
            self.assertEqual(code, 0)
            self.assertTrue(verdict_file.exists())
            self.assertFalse(refusal_file.exists())
            self.assertIn("CCA verdict complete", output)

    def test_every_rejection_rule_writes_a_record_and_never_cca_verdict_json(self):
        # Asserted for every rejection rule (acceptance criterion): the operative
        # authority path is never minted by a refusal, whatever the rule.
        rejections = {
            "degenerate": _complete_json(checklist=[]),
            "schema": '{"verdict":"complete"}',
            "invalid_verdict_value": '{"verdict":"approved_by_me","summary":"x","goal_id":"G-1","task_id":"T-1","pr":1,"head_sha":"abc123","can_merge":true,"checklist":[{"id":"CCA-01","status":"pass","blocking":true,"evidence":"ok"}],"acceptance":[{"criterion":"A1","status":"pass","evidence":["ok"]}],"mergegate":{},"confidence":1.0}',
            "repair_required_no_packet": '{"verdict":"repair_required","summary":"x","goal_id":"G-1","task_id":"T-1","pr":1,"head_sha":"abc123","can_merge":false,"checklist":[{"id":"CCA-01","status":"fail","blocking":true}],"acceptance":[{"criterion":"A1","status":"fail","evidence":["x"]}],"mergegate":{},"confidence":0.5,"repair_packet":null}',
            "complete_blocking_fail": _complete_json(
                checklist=[
                    {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "ok"},
                    {"id": "CCA-09", "status": "fail", "blocking": True, "reason": "no approval"},
                ]
            ),
            "complete_acceptance_fail": _complete_json(
                acceptance=[{"criterion": "A2", "status": "fail", "evidence": ["regressed"]}]
            ),
        }
        for rule, structured in rejections.items():
            with self.subTest(rule=rule):
                with tempfile.TemporaryDirectory() as d:
                    code, _, verdict_file, refusal_file = _run_enforcer(structured, Path(d))
                    self.assertEqual(code, 1, rule)
                    self.assertFalse(verdict_file.exists(), f"{rule}: cca-verdict.json was written for a refusal")
                    self.assertTrue(refusal_file.exists(), f"{rule}: no refusal record was written")


class RefusalClassificationTests(unittest.TestCase):
    """Transient vs non-transient classification, by the item ``reason`` TEXT, for
    each rule (no new status value, no schema change)."""

    def _analyze(self, verdict):
        with in_repo_root():
            return enforce_cca_verdict.analyze_refusal(verdict)

    def test_blocking_insufficient_not_yet_available_is_transient(self):
        # Measured 1 (PR #291): a blocking checklist item left insufficient_evidence
        # because the concurrent required checks are still in flight at judge time.
        ids, transient, _ = self._analyze(
            complete_verdict(
                checklist=[
                    {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "ok"},
                    {"id": "CCA-06", "status": "insufficient_evidence", "blocking": True,
                     "reason": "cannot be proven on the first run: the required workflows fire concurrently and are still in progress at judge time"},
                ]
            )
        )
        self.assertEqual(ids, ["CCA-06"])
        self.assertTrue(transient)

    def test_acceptance_insufficient_in_progress_is_transient(self):
        # Measured 2 (PR #292): an acceptance criterion left insufficient_evidence
        # because the validate_shiki required check was IN_PROGRESS at judge time.
        ids, transient, _ = self._analyze(
            complete_verdict(
                acceptance=[
                    {"criterion": "validate_shiki passes on this HEAD", "status": "insufficient_evidence",
                     "evidence": [], "reason": "the validate_shiki required check was IN_PROGRESS at judge time"},
                ]
            )
        )
        self.assertEqual(ids, ["validate_shiki passes on this HEAD"])
        self.assertTrue(transient)

    def test_insufficient_missing_evidence_is_not_transient(self):
        _, transient, _ = self._analyze(
            complete_verdict(
                checklist=[
                    {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "ok"},
                    {"id": "CCA-09", "status": "insufficient_evidence", "blocking": True,
                     "reason": "the PR body records no verification output for this item"},
                ]
            )
        )
        self.assertFalse(transient)

    def test_durable_fail_is_not_transient_even_with_a_timing_reason(self):
        # A durable fail is a real failure a re-run cannot fix, regardless of the
        # reason wording — status beats reason text.
        _, transient, _ = self._analyze(
            complete_verdict(
                checklist=[
                    {"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "ok"},
                    {"id": "CCA-09", "status": "fail", "blocking": True, "reason": "still in progress"},
                ]
            )
        )
        self.assertFalse(transient)

    def test_structural_faults_are_not_transient(self):
        for verdict in (
            complete_verdict(checklist=[]),  # degenerate array
            {"verdict": "complete"},  # schema-invalid
            complete_verdict(
                verdict="blocked",
                checklist=[
                    {"id": "CCA-08", "status": "fail", "blocking": True, "reason": "guardian approval required and not recorded"},
                    {"id": "CCA-05", "status": "insufficient_evidence", "blocking": True, "reason": "already blocked; not evaluated"},
                ],
            ),  # short-circuit
        ):
            with self.subTest(verdict=verdict.get("verdict")):
                _, transient, _ = self._analyze(verdict)
                self.assertFalse(transient)


class EnforcerVerbatimStringTests(unittest.TestCase):
    """The five strings ``test_shiki_control_plane.sh`` greps survive byte-identical
    through the enforcer (a second guard so an edit here is caught in this suite too)."""

    _VALID_COMPLETE = '{"verdict":"complete","summary":"complete","goal_id":"G-0001","task_id":"T-0001","pr":1,"head_sha":"abc123","can_merge":true,"checklist":[{"id":"CCA-01","status":"pass","blocking":true,"evidence":"fixture"}],"acceptance":[{"criterion":"A1","status":"pass","evidence":["fixture"]}],"mergegate":{"required_checks":"pass"},"confidence":1.0,"repair_packet":null}'
    _VALID_BLOCKED = '{"verdict":"blocked","summary":"fully evaluated","goal_id":"G-0001","task_id":"T-0001","pr":1,"head_sha":"abc123","can_merge":false,"checklist":[{"id":"CCA-08","status":"fail","blocking":true,"reason":"guardian approval is required and not recorded"},{"id":"CCA-05","status":"insufficient_evidence","blocking":true,"reason":"the PR body records no TDD command output for this path"}],"acceptance":[{"criterion":"A1","status":"pass","evidence":["fixture"]}],"mergegate":{},"confidence":0.5}'
    _MISSING_REQUIRED = '{"verdict":"complete"}'
    _REPAIR_NO_PACKET = '{"verdict":"repair_required","summary":"needs repair","goal_id":"G-0001","task_id":"T-0001","pr":1,"head_sha":"abc123","can_merge":false,"checklist":[{"id":"CCA-01","status":"fail","blocking":true}],"acceptance":[{"criterion":"A1","status":"fail","evidence":["fixture"]}],"mergegate":{},"confidence":0.5,"repair_packet":null}'
    _SHORT_CIRCUIT = '{"verdict":"blocked","summary":"short circuit","goal_id":"G-0001","task_id":"T-0001","pr":1,"head_sha":"abc123","can_merge":false,"checklist":[{"id":"CCA-08","status":"fail","blocking":true,"reason":"guardian approval is required and not recorded"},{"id":"CCA-05","status":"insufficient_evidence","blocking":true,"reason":"already blocked; not evaluated"}],"acceptance":[{"criterion":"A1","status":"insufficient_evidence","evidence":["n/a"],"reason":"verdict already determined"}],"mergegate":{},"confidence":0.5}'

    def test_five_greps_survive_byte_identical(self):
        cases = [
            (self._MISSING_REQUIRED, "missing required property", 1),
            (self._REPAIR_NO_PACKET, "repair_required verdict must include a non-null object", 1),
            (self._VALID_COMPLETE, "CCA verdict complete", 0),
            (self._SHORT_CIRCUIT, "blocking evaluation short-circuited", 1),
            (self._VALID_BLOCKED, "CCA verdict is blocked; MergeGate is blocked", 1),
        ]
        for structured, needle, expected_code in cases:
            with self.subTest(needle=needle):
                with tempfile.TemporaryDirectory() as d:
                    code, output, _, _ = _run_enforcer(structured, Path(d))
                    self.assertEqual(code, expected_code, needle)
                    self.assertIn(needle, output)


class RefusalPathAgreementTests(unittest.TestCase):
    """The enforcer's refusal-record basename and the loop's ``CCA_REFUSAL_BASENAME``
    must agree so the resolver reads exactly what the enforcer wrote."""

    def test_basenames_agree(self):
        derived = enforce_cca_verdict.refusal_record_path(Path("cca-verdict.json")).name
        self.assertEqual(derived, shiki_loop.CCA_REFUSAL_BASENAME)
        self.assertNotEqual(derived, "cca-verdict.json")


if __name__ == "__main__":
    unittest.main()
