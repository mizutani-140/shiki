#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-evidence-integrity-test-$$"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path.cwd() / "scripts"))

from shiki_evidence import (
    build_cca_evidence_manifest,
    ledger_entry_digest,
    validate_cca_evidence_manifest,
    validate_ledger_integrity,
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def expect_fail(name: str, fn, needle: str) -> None:
    errors = fn()
    if not errors:
        raise SystemExit(f"{name}: expected failure")
    if not any(needle in error for error in errors):
        raise SystemExit(f"{name}: expected {needle!r}, got {errors}")


with tempfile.TemporaryDirectory(prefix="shiki-evidence-") as tmp:
    evidence_dir = Path(tmp) / ".shiki" / "gha"
    head = "a" * 40
    verdict = {
        "verdict": "complete",
        "summary": "fixture",
        "goal_id": "G-0012",
        "task_id": "T-0047",
        "pr": 123,
        "head_sha": head,
        "can_merge": True,
        "checklist": [],
        "acceptance": [{"criterion": "fixture", "status": "pass", "evidence": ["fixture"]}],
        "mergegate": {},
        "confidence": 1,
    }
    pr = {"number": 123, "headRefOid": head}
    write(evidence_dir / "cca-verdict.json", json.dumps(verdict))
    write(evidence_dir / "pr.json", json.dumps(pr))
    write(evidence_dir / "changed-files.txt", "scripts/shiki_evidence.py\n")
    write(evidence_dir / "changed-files-status.txt", "A\tscripts/shiki_evidence.py\n")

    manifest = build_cca_evidence_manifest(
        repository="OWNER/REPO",
        pr=123,
        head_sha=head,
        workflow_name="Shiki CCA Completion",
        run_id="987",
        run_attempt="1",
        event_name="pull_request",
        artifact_name="shiki-cca-evidence",
        evidence_dir=evidence_dir,
    )
    manifest["created_at"] = "2026-06-05T00:00:00Z"
    errors = validate_cca_evidence_manifest(
        manifest=manifest,
        evidence_dir=evidence_dir,
        expected_repo="OWNER/REPO",
        expected_pr=123,
        expected_head_sha=head,
        expected_task_id="T-0047",
        expected_goal_id="G-0012",
    )
    if errors:
        raise SystemExit(f"valid manifest failed: {errors}")
    if not any(entry["path"] == ".shiki/gha/cca-verdict.json" and entry["sha256"] for entry in manifest["files"]):
        raise SystemExit("manifest missing verdict digest")

    expect_fail(
        "PR mismatch",
        lambda: validate_cca_evidence_manifest(
            manifest={**manifest, "pr": 124},
            evidence_dir=evidence_dir,
            expected_repo="OWNER/REPO",
            expected_pr=123,
            expected_head_sha=head,
            expected_task_id="T-0047",
            expected_goal_id="G-0012",
        ),
        "does not match PR",
    )
    expect_fail(
        "head mismatch",
        lambda: validate_cca_evidence_manifest(
            manifest={**manifest, "head_sha": "b" * 40},
            evidence_dir=evidence_dir,
            expected_repo="OWNER/REPO",
            expected_pr=123,
            expected_head_sha=head,
            expected_task_id="T-0047",
            expected_goal_id="G-0012",
        ),
        "head_sha",
    )
    bad_task = json.loads(json.dumps(manifest))
    bad_task["verdict"]["task_id"] = "T-9999"
    expect_fail(
        "task mismatch",
        lambda: validate_cca_evidence_manifest(
            manifest=bad_task,
            evidence_dir=evidence_dir,
            expected_repo="OWNER/REPO",
            expected_pr=123,
            expected_head_sha=head,
            expected_task_id="T-0047",
            expected_goal_id="G-0012",
        ),
        "task_id",
    )
    bad_goal = json.loads(json.dumps(manifest))
    bad_goal["verdict"]["goal_id"] = "G-9999"
    expect_fail(
        "goal mismatch",
        lambda: validate_cca_evidence_manifest(
            manifest=bad_goal,
            evidence_dir=evidence_dir,
            expected_repo="OWNER/REPO",
            expected_pr=123,
            expected_head_sha=head,
            expected_task_id="T-0047",
            expected_goal_id="G-0012",
        ),
        "goal_id",
    )
    bad_digest = json.loads(json.dumps(manifest))
    bad_digest["files"][0]["sha256"] = "0" * 64
    expect_fail(
        "digest mismatch",
        lambda: validate_cca_evidence_manifest(
            manifest=bad_digest,
            evidence_dir=evidence_dir,
            expected_repo="OWNER/REPO",
            expected_pr=123,
            expected_head_sha=head,
            expected_task_id="T-0047",
            expected_goal_id="G-0012",
        ),
        "digest mismatch",
    )
    missing_file = json.loads(json.dumps(manifest))
    missing_file["files"] = missing_file["files"][1:]
    expect_fail(
        "missing required file",
        lambda: validate_cca_evidence_manifest(
            manifest=missing_file,
            evidence_dir=evidence_dir,
            expected_repo="OWNER/REPO",
            expected_pr=123,
            expected_head_sha=head,
            expected_task_id="T-0047",
            expected_goal_id="G-0012",
        ),
        "missing required file entry",
    )

    ledger = {
        "id": "L-20260605T000000000000Z-00000000",
        "timestamp": "2026-06-05T00:00:00Z",
        "goal_id": "G-0012",
        "task_id": "T-0047",
        "type": "check",
        "actor": "fixture",
        "summary": "fixture",
        "evidence": ["fixture"],
        "evidence_refs": [{"kind": "github-pr", "pr": 123, "head_sha": head}],
        "ledger_integrity": {"algorithm": "sha256"},
    }
    digest = ledger_entry_digest(ledger)
    ledger["ledger_integrity"]["canonical_digest"] = digest
    if validate_ledger_integrity(ledger):
        raise SystemExit("valid ledger integrity should pass")
    malformed = {**ledger, "evidence_refs": [{"kind": "github-pr", "head_sha": head}]}
    if not validate_ledger_integrity(malformed):
        raise SystemExit("malformed evidence_refs should fail")

print("evidence manifest and ledger helper fixtures passed")
PY

MG="$TMP_ROOT/mergegate"
mkdir -p "$MG/.shiki/tasks" "$MG/.shiki/goals" "$MG/.shiki/ledger" "$MG/.shiki/gha" "$MG/.github/workflows"
cp .shiki/manifest.json "$MG/.shiki/manifest.json"
cp .shiki/config.yaml "$MG/.shiki/config.yaml"
cp .shiki/guardian-policy.json "$MG/.shiki/guardian-policy.json"
cp -R .shiki/schemas "$MG/.shiki/schemas"
cp .github/workflows/shiki-validate.yml "$MG/.github/workflows/shiki-validate.yml"
cp .github/workflows/shiki-cca-completion.yml "$MG/.github/workflows/shiki-cca-completion.yml"
cp .github/workflows/shiki-mergegate.yml "$MG/.github/workflows/shiki-mergegate.yml"
cp .github/workflows/shiki-claude-review.yml "$MG/.github/workflows/shiki-claude-review.yml"
cp .github/workflows/shiki-orchestrator.yml "$MG/.github/workflows/shiki-orchestrator.yml"
cat >"$MG/.shiki/goals/G-0012.json" <<'JSON'
{"id":"G-0012","status":"planned"}
JSON
cat >"$MG/.shiki/tasks/T-0047.json" <<'JSON'
{
  "id": "T-0047",
  "goal_id": "G-0012",
  "status": "review",
  "risk_level": "critical",
  "locks": ["scripts/**"],
  "ledger_evidence": ["L-0047"],
  "required_skills": []
}
JSON
cat >"$MG/.shiki/ledger/L-0047.json" <<'JSON'
{
  "id": "L-0047",
  "goal_id": "G-0012",
  "task_id": "T-0047",
  "type": "check",
  "summary": "PR #123 evidence",
  "evidence": ["PR #123"],
  "links": ["https://github.com/OWNER/REPO/pull/123"],
  "evidence_refs": [{"kind": "github-pr", "pr": 123, "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]
}
JSON
cat >"$MG/.shiki/gha/live-pr.json" <<'JSON'
{
  "number": 123,
  "body": "T-0047\nG-0012\n\n## Scope\nx\n\n## Acceptance\nx\n\n## Evidence\nx\n\n## MergeGate\nx",
  "author": {"login": "mizutani-140"},
  "headRefName": "shiki/t-0047",
  "headRefOid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "labels": [{"name": "risk:critical"}, {"name": "guardian:approved"}],
  "reviews": [{"state": "APPROVED", "author": {"login": "github-actions[bot]"}}],
  "reviewDecision": "APPROVED",
  "statusCheckRollup": [
    {"name":"Validate Shiki mirror","status":"COMPLETED","conclusion":"SUCCESS","headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    {"name":"MergeGate metadata check","status":"COMPLETED","conclusion":"SUCCESS","headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    {"name":"Claude review","status":"COMPLETED","conclusion":"SUCCESS","headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ]
}
JSON
cp "$MG/.shiki/gha/live-pr.json" "$MG/.shiki/gha/pr.json"
cat >"$MG/.shiki/gha/cca-verdict.json" <<'JSON'
{
  "verdict": "complete",
  "summary": "fixture",
  "goal_id": "G-0012",
  "task_id": "T-0047",
  "pr": 123,
  "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "can_merge": true,
  "checklist": [],
  "acceptance": [{"criterion":"fixture","status":"pass","evidence":["fixture"]}],
  "mergegate": {},
  "confidence": 1
}
JSON
touch "$MG/.shiki/gha/live-changed-files.txt" "$MG/.shiki/gha/live-changed-files-status.txt"
touch "$MG/.shiki/gha/changed-files.txt" "$MG/.shiki/gha/changed-files-status.txt"
cat >"$MG/.shiki/gha/live-guardian-comments.json" <<'JSON'
[{"user":{"login":"mizutani-140"},"body":"Guardian approval granted aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]
JSON
cat >"$MG/.shiki/gha/live-guardian-events.json" <<'JSON'
[{"event":"labeled","label":{"name":"guardian:approved"},"actor":{"login":"mizutani-140"}}]
JSON
printf '[]\n' >"$MG/.shiki/gha/live-guardian-timeline.json"

python3 scripts/build_cca_evidence_manifest.py \
  --repo OWNER/REPO \
  --pr 123 \
  --head-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --workflow-name "Shiki CCA Completion" \
  --run-id 987 \
  --run-attempt 1 \
  --event-name pull_request \
  --artifact-name shiki-cca-evidence \
  --evidence-dir "$MG/.shiki/gha" \
  --output "$MG/.shiki/gha/cca-evidence-manifest.json" >/dev/null

MERGEGATE_ARGS=(
  --target "$MG"
  --pr-json "$MG/.shiki/gha/live-pr.json"
  --cca-verdict "$MG/.shiki/gha/cca-verdict.json"
  --cca-evidence-manifest "$MG/.shiki/gha/cca-evidence-manifest.json"
  --expected-repository OWNER/REPO
  --changed-files "$MG/.shiki/gha/live-changed-files.txt"
  --changed-files-status "$MG/.shiki/gha/live-changed-files-status.txt"
  --result-file "$MG/.shiki/gha/mergegate-result.json"
  --guardian-policy .shiki/guardian-policy.json
  --guardian-comments .shiki/gha/live-guardian-comments.json
  --guardian-events .shiki/gha/live-guardian-events.json
  --guardian-timeline .shiki/gha/live-guardian-timeline.json
)
python3 scripts/mergegate_check.py "${MERGEGATE_ARGS[@]}" >/tmp/shiki-evidence-mergegate-pass.out
grep '"mergegate": "ready"' /tmp/shiki-evidence-mergegate-pass.out >/dev/null

rm "$MG/.shiki/gha/cca-evidence-manifest.json"
if python3 scripts/mergegate_check.py "${MERGEGATE_ARGS[@]}" >/tmp/shiki-evidence-mergegate-missing.out 2>&1; then
  echo "MergeGate should block missing CCA evidence manifest" >&2
  exit 1
fi
grep "CCA evidence manifest file not found" /tmp/shiki-evidence-mergegate-missing.out >/dev/null

python3 scripts/build_cca_evidence_manifest.py \
  --repo OWNER/REPO \
  --pr 123 \
  --head-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --workflow-name "Shiki CCA Completion" \
  --run-id 987 \
  --run-attempt 1 \
  --event-name pull_request \
  --artifact-name shiki-cca-evidence \
  --evidence-dir "$MG/.shiki/gha" \
  --output "$MG/.shiki/gha/cca-evidence-manifest.json" >/dev/null
python3 - <<PY
from pathlib import Path
path = Path("$MG/.shiki/gha/cca-verdict.json")
path.write_text(path.read_text(encoding="utf-8").replace("fixture", "tampered", 1), encoding="utf-8")
PY
if python3 scripts/mergegate_check.py "${MERGEGATE_ARGS[@]}" >/tmp/shiki-evidence-mergegate-digest.out 2>&1; then
  echo "MergeGate should block CCA evidence digest mismatch" >&2
  exit 1
fi
grep "digest mismatch" /tmp/shiki-evidence-mergegate-digest.out >/dev/null

grep "Build CCA evidence manifest" .github/workflows/shiki-cca-completion.yml >/dev/null
grep -- "--cca-evidence-manifest .shiki/gha/cca-evidence-manifest.json" .github/workflows/shiki-cca-completion.yml >/dev/null
grep ".shiki/gha/cca-evidence-manifest.json" docs/agents/evidence-integrity.md >/dev/null

echo "shiki evidence integrity tests passed"
