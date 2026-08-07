"""Task-less CCA evidence manifest: an empty verdict.task_id is schema-valid.

Regression coverage for the contract-mode (SADR-0015) defect where the CCA
evidence manifest schema wrapped the verdict more strictly than the document it
wraps. ``.shiki/schemas/cca-verdict.schema.json`` declares ``task_id`` as
``{"type": "string"}`` with no length floor, but the manifest schema required
``verdict.task_id`` to have ``minLength: 1``. A legitimate contract-mode PR that
has no single task therefore carried an empty ``task_id`` and was rejected at
``MergeGate policy check`` as ``$.verdict.task_id: expected string length >= 1``
— a schema rejection of a valid governance document, not a governance failure.

The identity binding that actually protects ``task_id`` lives in
``scripts/shiki_evidence.py``: ``validate_cca_evidence_manifest`` rejects an
empty or mismatched ``task_id`` whenever the PR resolves to a real task id, and
only then. These tests pin both halves so the two schemas cannot silently
diverge again: the manifest schema accepts an empty ``task_id`` (task-less
mode), while the binding still rejects laundering one on a task-ful PR.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)
from shiki_test_support import REPO_ROOT

import shiki_evidence
from shiki_jsonschema import JsonSchemaError, validate_json_schema


REPO = "OWNER/REPO"
PR = 182
HEAD = "a" * 40
GOAL_ID = "G-real"
REAL_TASK = "T-real"
OTHER_TASK = "T-other"

MANIFEST_SCHEMA_PATH = REPO_ROOT / ".shiki" / "schemas" / "cca-evidence-manifest.schema.json"
VERDICT_SCHEMA_PATH = REPO_ROOT / ".shiki" / "schemas" / "cca-verdict.schema.json"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_manifest(evidence_dir: Path, *, task_id: str) -> dict:
    """Build a fully valid manifest whose wrapped verdict carries ``task_id``.

    The digests in the manifest are computed over the real evidence files, so
    the only variable under test is ``verdict.task_id``.
    """
    verdict = {
        "verdict": "complete",
        "summary": "fixture",
        "goal_id": GOAL_ID,
        "task_id": task_id,
        "pr": PR,
        "head_sha": HEAD,
        "can_merge": True,
        "checklist": [],
        "acceptance": [{"criterion": "fixture", "status": "pass", "evidence": ["fixture"]}],
        "mergegate": {},
        "confidence": 1,
    }
    _write(evidence_dir / "cca-verdict.json", json.dumps(verdict))
    _write(evidence_dir / "pr.json", json.dumps({"number": PR, "headRefOid": HEAD}))
    _write(evidence_dir / "changed-files.txt", "scripts/shiki_evidence.py\n")
    _write(evidence_dir / "changed-files-status.txt", "M\tscripts/shiki_evidence.py\n")
    manifest = shiki_evidence.build_cca_evidence_manifest(
        repository=REPO,
        pr=PR,
        head_sha=HEAD,
        workflow_name="Shiki CCA Completion",
        run_id="1",
        run_attempt="1",
        event_name="pull_request",
        artifact_name="shiki-cca-evidence",
        evidence_dir=evidence_dir,
    )
    manifest["created_at"] = "2026-07-29T00:00:00Z"
    return manifest


def _validate(manifest: dict, evidence_dir: Path, *, expected_task_id: str | None) -> list[str]:
    return shiki_evidence.validate_cca_evidence_manifest(
        manifest=manifest,
        evidence_dir=evidence_dir,
        expected_repo=REPO,
        expected_pr=PR,
        expected_head_sha=HEAD,
        expected_task_id=expected_task_id,
        expected_goal_id=GOAL_ID,
    )


class TasklessManifestBindingTests(unittest.TestCase):
    """The four identity-binding cases from the task contract."""

    def test_empty_task_id_accepted_when_no_task_expected(self) -> None:
        # Contract mode: the PR resolves to no single task, so an empty
        # manifest task_id is the correct, accepted document.
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp) / ".shiki" / "gha"
            manifest = _build_manifest(evidence_dir, task_id="")
            self.assertEqual(_validate(manifest, evidence_dir, expected_task_id=None), [])

    def test_empty_task_id_rejected_when_task_expected(self) -> None:
        # A task-ful PR that submits an empty task_id must still be rejected by
        # the identity binding, not silently accepted.
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp) / ".shiki" / "gha"
            manifest = _build_manifest(evidence_dir, task_id="")
            errors = _validate(manifest, evidence_dir, expected_task_id=REAL_TASK)
            self.assertTrue(any("task_id" in error for error in errors), errors)

    def test_mismatched_task_id_rejected_when_task_expected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp) / ".shiki" / "gha"
            manifest = _build_manifest(evidence_dir, task_id=OTHER_TASK)
            errors = _validate(manifest, evidence_dir, expected_task_id=REAL_TASK)
            self.assertTrue(any("task_id" in error for error in errors), errors)

    def test_matching_task_id_accepted_when_task_expected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp) / ".shiki" / "gha"
            manifest = _build_manifest(evidence_dir, task_id=REAL_TASK)
            self.assertEqual(_validate(manifest, evidence_dir, expected_task_id=REAL_TASK), [])


class ManifestSchemaTasklessTests(unittest.TestCase):
    """The manifest schema itself must accept an empty ``verdict.task_id``."""

    def _manifest_schema(self) -> dict:
        return json.loads(MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_empty_verdict_task_id_validates_against_manifest_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp) / ".shiki" / "gha"
            manifest = _build_manifest(evidence_dir, task_id="")
            self.assertEqual(manifest["verdict"]["task_id"], "")
            try:
                validate_json_schema(manifest, self._manifest_schema())
            except JsonSchemaError as error:  # pragma: no cover - failure path
                self.fail(f"empty verdict.task_id must satisfy the manifest schema: {error}")

    def test_manifest_schema_task_id_is_required_without_minlength(self) -> None:
        verdict_props = self._manifest_schema()["properties"]["verdict"]
        # task_id stays required (presence is still a contract) but must carry
        # no length floor.
        self.assertIn("task_id", verdict_props["required"])
        self.assertNotIn("minLength", verdict_props["properties"]["task_id"])

    def test_manifest_schema_task_id_matches_wrapped_verdict_schema(self) -> None:
        manifest_task_id = self._manifest_schema()["properties"]["verdict"]["properties"]["task_id"]
        verdict_task_id = json.loads(VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))["properties"]["task_id"]
        # The wrapper must not constrain task_id length more tightly than the
        # document it wraps; that mismatch is the defect this suite guards.
        self.assertEqual("minLength" in manifest_task_id, "minLength" in verdict_task_id)


if __name__ == "__main__":
    unittest.main()
