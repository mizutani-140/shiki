"""CCA evidence manifest and ledger evidence-reference helpers."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


CCA_EVIDENCE_MANIFEST_KIND = "shiki-cca-evidence-manifest"
CCA_EVIDENCE_MANIFEST_VERSION = 1
CCA_EVIDENCE_MANIFEST_PATH = ".shiki/gha/cca-evidence-manifest.json"
CCA_EVIDENCE_ARTIFACT_NAME = "shiki-cca-evidence"
REQUIRED_CCA_EVIDENCE_FILES = (
    ".shiki/gha/cca-verdict.json",
    ".shiki/gha/pr.json",
    ".shiki/gha/changed-files.txt",
    ".shiki/gha/changed-files-status.txt",
)
REQUIRED_CCA_VERDICT_FIELDS = ("verdict", "goal_id", "task_id", "pr", "head_sha")


class EvidenceError(ValueError):
    """Raised when Shiki evidence references or manifests are invalid."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise EvidenceError(f"{path}: expected a JSON object")
    return data


def _relative_evidence_path(evidence_dir: Path, relative: str) -> Path:
    prefix = ".shiki/gha/"
    if not relative.startswith(prefix):
        raise EvidenceError(f"CCA evidence file path must start with {prefix}: {relative}")
    return evidence_dir / relative[len(prefix) :]


def build_cca_evidence_manifest(
    *,
    repository: str,
    pr: int,
    head_sha: str,
    workflow_name: str,
    run_id: str,
    run_attempt: str,
    event_name: str,
    artifact_name: str,
    evidence_dir: Path,
) -> dict[str, Any]:
    evidence_dir = evidence_dir.resolve()
    verdict_path = _relative_evidence_path(evidence_dir, ".shiki/gha/cca-verdict.json")
    verdict = _load_json_object(verdict_path)
    missing_verdict_fields = [field for field in REQUIRED_CCA_VERDICT_FIELDS if field not in verdict]
    if missing_verdict_fields:
        raise EvidenceError("CCA verdict is missing required fields for manifest: " + ", ".join(missing_verdict_fields))

    files: list[dict[str, Any]] = []
    for relative in REQUIRED_CCA_EVIDENCE_FILES:
        path = _relative_evidence_path(evidence_dir, relative)
        if not path.is_file():
            raise EvidenceError(f"required CCA evidence file is missing: {relative}")
        files.append({"path": relative, "sha256": sha256_file(path), "required": True})

    return {
        "version": CCA_EVIDENCE_MANIFEST_VERSION,
        "kind": CCA_EVIDENCE_MANIFEST_KIND,
        "repository": repository,
        "pr": int(pr),
        "head_sha": head_sha,
        "workflow": {
            "name": workflow_name,
            "run_id": str(run_id),
            "run_attempt": str(run_attempt),
            "job": "CCA verdict",
            "event_name": event_name,
        },
        "artifact": {
            "name": artifact_name,
            "path": ".shiki/gha",
            "uploaded_by": "github-actions[bot]",
        },
        "files": files,
        "verdict": {
            "verdict": verdict.get("verdict"),
            "goal_id": verdict.get("goal_id"),
            "task_id": verdict.get("task_id"),
            "pr": verdict.get("pr"),
            "head_sha": verdict.get("head_sha"),
        },
        "created_at": "",
    }


def validate_cca_evidence_manifest(
    *,
    manifest: dict[str, Any],
    evidence_dir: Path,
    expected_repo: str,
    expected_pr: int,
    expected_head_sha: str,
    expected_task_id: str | None = None,
    expected_goal_id: str | None = None,
) -> list[str]:
    errors: list[str] = []
    evidence_dir = evidence_dir.resolve()
    if manifest.get("version") != CCA_EVIDENCE_MANIFEST_VERSION:
        errors.append("CCA evidence manifest version must be 1")
    if manifest.get("kind") != CCA_EVIDENCE_MANIFEST_KIND:
        errors.append(f"CCA evidence manifest kind must be {CCA_EVIDENCE_MANIFEST_KIND}")
    if manifest.get("repository") != expected_repo:
        errors.append(f"CCA evidence manifest repository {manifest.get('repository')!r} does not match {expected_repo!r}")
    if manifest.get("pr") != expected_pr:
        errors.append(f"CCA evidence manifest pr {manifest.get('pr')!r} does not match PR #{expected_pr}")
    if manifest.get("head_sha") != expected_head_sha:
        errors.append("CCA evidence manifest head_sha does not match current PR headRefOid")

    workflow = manifest.get("workflow")
    if not isinstance(workflow, dict):
        errors.append("CCA evidence manifest workflow must be an object")
    else:
        for field in ("name", "run_id", "run_attempt", "job", "event_name"):
            if not workflow.get(field):
                errors.append(f"CCA evidence manifest workflow.{field} is required")
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        errors.append("CCA evidence manifest artifact must be an object")
    elif artifact.get("name") != CCA_EVIDENCE_ARTIFACT_NAME:
        errors.append(f"CCA evidence manifest artifact.name must be {CCA_EVIDENCE_ARTIFACT_NAME}")

    verdict = manifest.get("verdict")
    if not isinstance(verdict, dict):
        errors.append("CCA evidence manifest verdict must be an object")
    else:
        if verdict.get("pr") != expected_pr:
            errors.append(f"CCA evidence manifest verdict.pr {verdict.get('pr')!r} does not match PR #{expected_pr}")
        if verdict.get("head_sha") != expected_head_sha:
            errors.append("CCA evidence manifest verdict.head_sha does not match current PR headRefOid")
        if expected_task_id and verdict.get("task_id") != expected_task_id:
            errors.append(f"CCA evidence manifest verdict.task_id {verdict.get('task_id')!r} does not match {expected_task_id}")
        if expected_goal_id and verdict.get("goal_id") != expected_goal_id:
            errors.append(f"CCA evidence manifest verdict.goal_id {verdict.get('goal_id')!r} does not match {expected_goal_id}")

    files = manifest.get("files")
    if not isinstance(files, list):
        errors.append("CCA evidence manifest files must be an array")
        return errors
    by_path: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            errors.append(f"CCA evidence manifest files[{index}] must be an object")
            continue
        relative = entry.get("path")
        if not isinstance(relative, str):
            errors.append(f"CCA evidence manifest files[{index}].path must be a string")
            continue
        by_path[relative] = entry
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            errors.append(f"CCA evidence manifest {relative} sha256 must be a 64-character string")
            continue
        path = _relative_evidence_path(evidence_dir, relative)
        if not path.is_file():
            errors.append(f"CCA evidence manifest references missing file {relative}")
            continue
        actual = sha256_file(path)
        if actual != digest:
            errors.append(f"CCA evidence manifest digest mismatch for {relative}")

    for relative in REQUIRED_CCA_EVIDENCE_FILES:
        entry = by_path.get(relative)
        if entry is None:
            errors.append(f"CCA evidence manifest missing required file entry {relative}")
        elif entry.get("required") is not True:
            errors.append(f"CCA evidence manifest file entry {relative} must set required=true")
    return errors


def _canonical_ledger_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(entry)
    integrity = payload.get("ledger_integrity")
    if isinstance(integrity, dict):
        integrity.pop("canonical_digest", None)
        if not integrity:
            payload.pop("ledger_integrity", None)
    for ref in payload.get("evidence_refs") or []:
        if isinstance(ref, dict) and ref.get("kind") == "ledger-digest":
            ref.pop("digest", None)
    return payload


def ledger_entry_digest(entry: dict[str, Any]) -> str:
    payload = _canonical_ledger_payload(entry)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evidence_reference_for_ledger(
    *,
    ledger_entry: dict[str, Any],
    pr: int,
    head_sha: str,
    workflow_run_id: str | None,
    artifact_name: str | None,
) -> dict[str, Any]:
    references: list[dict[str, Any]] = [
        {"kind": "github-pr", "pr": int(pr), "head_sha": head_sha},
    ]
    if workflow_run_id:
        references.append(
            {
                "kind": "github-workflow-run",
                "workflow": "Shiki CCA Completion",
                "run_id": str(workflow_run_id),
            }
        )
    if artifact_name:
        references.append(
            {
                "kind": "github-artifact",
                "name": artifact_name,
                "manifest_path": CCA_EVIDENCE_MANIFEST_PATH,
            }
        )
    references.append({"kind": "ledger-digest", "algorithm": "sha256", "digest": ledger_entry_digest(ledger_entry)})
    return {"evidence_refs": references}


def validate_evidence_refs(refs: Any) -> list[str]:
    if refs is None:
        return []
    if not isinstance(refs, list):
        return ["evidence_refs must be an array"]
    errors: list[str] = []
    for index, ref in enumerate(refs):
        if not isinstance(ref, dict):
            errors.append(f"evidence_refs[{index}] must be an object")
            continue
        kind = ref.get("kind")
        if kind == "github-pr":
            if not isinstance(ref.get("pr"), int):
                errors.append(f"evidence_refs[{index}].pr must be an integer")
            if not isinstance(ref.get("head_sha"), str) or len(ref.get("head_sha", "")) < 7:
                errors.append(f"evidence_refs[{index}].head_sha must be a string")
        elif kind == "github-workflow-run":
            if not ref.get("workflow") or not ref.get("run_id"):
                errors.append(f"evidence_refs[{index}] workflow run references require workflow and run_id")
        elif kind == "github-artifact":
            if not ref.get("name") or not ref.get("manifest_path"):
                errors.append(f"evidence_refs[{index}] artifact references require name and manifest_path")
        elif kind == "ledger-digest":
            if ref.get("algorithm") != "sha256":
                errors.append(f"evidence_refs[{index}].algorithm must be sha256")
            digest = ref.get("digest")
            if not isinstance(digest, str) or len(digest) != 64:
                errors.append(f"evidence_refs[{index}].digest must be a 64-character string")
        elif kind == "github-issue":
            if not isinstance(ref.get("issue"), int):
                errors.append(f"evidence_refs[{index}].issue must be an integer")
        else:
            errors.append(f"evidence_refs[{index}].kind is unsupported: {kind!r}")
    return errors


def validate_ledger_integrity(entry: dict[str, Any]) -> list[str]:
    errors = validate_evidence_refs(entry.get("evidence_refs"))
    integrity = entry.get("ledger_integrity")
    if integrity is not None:
        if not isinstance(integrity, dict):
            errors.append("ledger_integrity must be an object")
        else:
            if integrity.get("algorithm") != "sha256":
                errors.append("ledger_integrity.algorithm must be sha256")
            digest = integrity.get("canonical_digest")
            if not isinstance(digest, str) or len(digest) != 64:
                errors.append("ledger_integrity.canonical_digest must be a 64-character string")
            elif digest != ledger_entry_digest(entry):
                errors.append("ledger_integrity.canonical_digest does not match canonical ledger digest")
    for index, ref in enumerate(entry.get("evidence_refs") or []):
        if isinstance(ref, dict) and ref.get("kind") == "ledger-digest":
            digest = ref.get("digest")
            if isinstance(digest, str) and len(digest) == 64 and digest != ledger_entry_digest(entry):
                errors.append(f"evidence_refs[{index}].digest does not match canonical ledger digest")
    return errors
