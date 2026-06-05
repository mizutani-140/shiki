#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-governance-evidence-test-$$"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

cd "$ROOT"
mkdir -p "$TMP_ROOT"

python3 - "$ROOT" "$TMP_ROOT" <<'PY'
import copy
import json
import pathlib
import shutil
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
tmp_root = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root / "scripts"))

from shiki_evidence import (
    CCA_EVIDENCE_ARTIFACT_NAME,
    build_cca_evidence_manifest,
    evidence_reference_for_ledger,
    ledger_entry_digest,
    validate_cca_evidence_manifest,
    validate_evidence_refs,
    validate_ledger_integrity,
)
from shiki_guardian import evaluate_guardian_approval, load_guardian_policy
from shiki_state_classes import class_policy, classify_shiki_path


HEAD = "1111111111111111111111111111111111111111"
OLD_HEAD = "2222222222222222222222222222222222222222"
PR_NUMBER = 777
TASK_ID = "T-0049"
GOAL_ID = "G-0012"
LEDGER_ID = "L-20260605T061500000000Z-6a4c9e31"
BRANCH = "shiki/t-0049-governance-evidence-tests"


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"expected {needle!r} in:\n{text}")


def assert_errors(errors: list[str], needle: str) -> None:
    joined = "\n".join(errors)
    assert_contains(joined, needle)


def write_json(path: pathlib.Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def copy_base_support(target: pathlib.Path) -> None:
    (target / ".shiki").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / ".shiki" / "manifest.json", target / ".shiki" / "manifest.json")
    shutil.copytree(root / ".shiki" / "schemas", target / ".shiki" / "schemas")
    shutil.copy2(root / ".shiki" / "config.yaml", target / ".shiki" / "config.yaml")
    shutil.copy2(root / ".shiki" / "guardian-policy.json", target / ".shiki" / "guardian-policy.json")
    shutil.copytree(root / ".github" / "workflows", target / ".github" / "workflows")


def goal_payload() -> dict:
    return {
        "id": GOAL_ID,
        "title": "Governance evidence fixture goal",
        "outcome": "Forged, stale, and missing governance evidence tests pass",
        "acceptance_evidence": ["fixture"],
        "completion_conditions": ["fixture"],
        "non_goals": [],
        "required_skills": ["diagnose", "tdd"],
        "risk_level": "critical",
        "status": "planned",
    }


def ledger_payload(ledger_id: str = LEDGER_ID, *, task_id: str | None = TASK_ID, pr: int = PR_NUMBER) -> dict:
    payload = {
        "id": ledger_id,
        "timestamp": "2026-06-05T06:15:00Z",
        "goal_id": GOAL_ID,
        "type": "check",
        "actor": "test",
        "summary": f"diagnose tdd governance evidence fixture for PR #{pr}",
        "evidence": [
            "diagnose",
            "tdd",
            "scripts/test_shiki_governance_evidence.sh",
            f"PR #{pr}",
            f"https://github.com/mizutani-140/shiki/pull/{pr}",
        ],
        "links": [f"https://github.com/mizutani-140/shiki/pull/{pr}"],
    }
    if task_id is not None:
        payload["task_id"] = task_id
    return payload


def task_payload(*, locks: list[str], ledger_ids: list[str] | None = None, expected_pr: int | None = PR_NUMBER, status: str = "review") -> dict:
    return {
        "id": TASK_ID,
        "goal_id": GOAL_ID,
        "github_issue": 96,
        "title": "Add forged / stale / missing governance evidence tests",
        "scope": "Add adversarial governance evidence regression coverage for Goal #30 P1.4.6.",
        "non_goals": ["No new Guardian policy semantics."],
        "dependencies": ["T-0048"],
        "locks": locks,
        "assigned_runtime": "codex",
        "risk_level": "critical",
        "required_skills": ["diagnose", "tdd"],
        "acceptance_checks": ["governance evidence fixtures pass"],
        "expected_branch": BRANCH,
        "expected_pr": expected_pr,
        "ledger_evidence": ledger_ids or [LEDGER_ID],
        "status": status,
    }


def pr_payload(*, head: str = HEAD, number: int = PR_NUMBER, body_suffix: str = "", checks: list[dict] | None = None, labels: list[dict] | None = None, reviews: list[dict] | None = None) -> dict:
    return {
        "number": number,
        "title": "shiki: add governance evidence adversarial tests",
        "body": "\n".join(
            [
                "## Scope",
                f"Task {TASK_ID} for Goal {GOAL_ID}.",
                "## Acceptance",
                "Forged, stale, and missing governance evidence fixtures pass.",
                "## Evidence",
                f"Ledger {LEDGER_ID}; PR #{number}.",
                "## MergeGate",
                "Ready when checks pass.",
                body_suffix,
            ]
        ),
        "author": {"login": "mizutani-140"},
        "headRefName": BRANCH,
        "baseRefName": "main",
        "headRefOid": head,
        "labels": labels if labels is not None else [{"name": "guardian:approved"}],
        "reviews": reviews if reviews is not None else [{"state": "APPROVED", "author": {"login": "github-actions"}}],
        "reviewDecision": "APPROVED",
        "statusCheckRollup": checks
        if checks is not None
        else [
            {"name": "Validate Shiki mirror", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": head},
            {"name": "MergeGate metadata check", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": head},
        ],
    }


def cca_payload(*, head: str = HEAD, number: int = PR_NUMBER, task_id: str = TASK_ID, goal_id: str = GOAL_ID, verdict: str = "complete") -> dict:
    return {
        "verdict": verdict,
        "summary": "fixture complete",
        "goal_id": goal_id,
        "task_id": task_id,
        "pr": number,
        "head_sha": head,
        "can_merge": True,
        "checklist": [{"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "fixture"}],
        "acceptance": [{"criterion": "fixture", "status": "pass", "evidence": ["fixture"]}],
        "mergegate": {"fixture": "pass"},
        "confidence": 1.0,
        "repair_packet": None,
    }


def make_fixture(
    name: str,
    *,
    locks: list[str] | None = None,
    changed: list[str] | None = None,
    status_entries: list[str] | None = None,
    task: dict | None = None,
    ledgers: dict[str, dict] | None = None,
    pr: dict | None = None,
    cca: dict | None = None,
    guardian_comments: list[dict] | None = None,
    guardian_events: list[dict] | None = None,
    guardian_timeline: list[dict] | None = None,
    write_guardian_files: bool = True,
    manifest: dict | None = None,
) -> pathlib.Path:
    target = tmp_root / name
    if target.exists():
        shutil.rmtree(target)
    copy_base_support(target)
    if manifest is not None:
        write_json(target / ".shiki" / "manifest.json", manifest)
    write_json(target / ".shiki" / "goals" / f"{GOAL_ID}.json", goal_payload())
    write_json(
        target / ".shiki" / "tasks" / "T-0048.json",
        {
            "id": "T-0048",
            "goal_id": GOAL_ID,
            "title": "Completed dependency fixture",
            "scope": "Fixture dependency for T-0049 governance evidence tests.",
            "non_goals": [],
            "dependencies": [],
            "locks": ["path:fixture"],
            "assigned_runtime": "codex",
            "risk_level": "critical",
            "required_skills": ["diagnose", "tdd"],
            "acceptance_checks": ["fixture"],
            "expected_branch": "main",
            "expected_pr": 95,
            "ledger_evidence": [LEDGER_ID],
            "status": "done",
        },
    )
    task_data = task or task_payload(locks=locks or ["path:scripts/test_shiki_governance_evidence.sh"])
    write_json(target / ".shiki" / "tasks" / f"{TASK_ID}.json", task_data)
    ledger_entries = ledgers or {LEDGER_ID: ledger_payload()}
    for ledger_id, payload in ledger_entries.items():
        write_json(target / ".shiki" / "ledger" / f"{ledger_id}.json", payload)
    write_json(target / ".shiki" / "gha" / "pr.json", pr or pr_payload())
    changed_files = changed or ["scripts/test_shiki_governance_evidence.sh"]
    (target / ".shiki" / "gha" / "changed-files.txt").write_text("\n".join(changed_files) + ("\n" if changed_files else ""), encoding="utf-8")
    status_lines = status_entries if status_entries is not None else [f"A\t{path}" for path in changed_files]
    (target / ".shiki" / "gha" / "changed-files-status.txt").write_text("\n".join(status_lines) + ("\n" if status_lines else ""), encoding="utf-8")
    write_json(target / ".shiki" / "gha" / "cca-verdict.json", cca or cca_payload())
    if write_guardian_files:
        write_json(
            target / ".shiki" / "gha" / "live-guardian-comments.json",
            guardian_comments
            if guardian_comments is not None
            else [
                {
                    "author": {"login": "mizutani-140"},
                    "body": f"Guardian approval granted for fixture head {HEAD}",
                }
            ],
        )
        write_json(
            target / ".shiki" / "gha" / "live-guardian-events.json",
            guardian_events
            if guardian_events is not None
            else [
                {
                    "event": "labeled",
                    "actor": {"login": "mizutani-140"},
                    "label": {"name": "guardian:approved"},
                }
            ],
        )
        write_json(target / ".shiki" / "gha" / "live-guardian-timeline.json", guardian_timeline if guardian_timeline is not None else [])
    return target


def add_cca_manifest(target: pathlib.Path, *, repository: str = "mizutani-140/shiki", pr: int = PR_NUMBER, head: str = HEAD) -> pathlib.Path:
    manifest = build_cca_evidence_manifest(
        repository=repository,
        pr=pr,
        head_sha=head,
        workflow_name="Shiki CCA Completion",
        run_id="123456",
        run_attempt="1",
        event_name="workflow_dispatch",
        artifact_name=CCA_EVIDENCE_ARTIFACT_NAME,
        evidence_dir=target / ".shiki" / "gha",
    )
    manifest["created_at"] = "2026-06-05T06:15:00Z"
    path = target / ".shiki" / "gha" / "cca-evidence-manifest.json"
    write_json(path, manifest)
    return path


def run_mergegate(target: pathlib.Path, *, expected_head: str = HEAD, expected_repository: str = "", use_manifest: bool = False, allow_missing_cca: bool = False) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(root / "scripts" / "mergegate_check.py"),
        "--target",
        str(target),
        "--pr-json",
        str(target / ".shiki" / "gha" / "pr.json"),
        "--changed-files",
        str(target / ".shiki" / "gha" / "changed-files.txt"),
        "--changed-files-status",
        str(target / ".shiki" / "gha" / "changed-files-status.txt"),
        "--cca-verdict",
        str(target / ".shiki" / "gha" / "cca-verdict.json"),
        "--expected-head-sha",
        expected_head,
        "--guardian-policy",
        str(target / ".shiki" / "guardian-policy.json"),
        "--guardian-comments",
        str(target / ".shiki" / "gha" / "live-guardian-comments.json"),
        "--guardian-events",
        str(target / ".shiki" / "gha" / "live-guardian-events.json"),
        "--guardian-timeline",
        str(target / ".shiki" / "gha" / "live-guardian-timeline.json"),
        "--result-file",
        str(target / ".shiki" / "gha" / "mergegate-result.json"),
    ]
    if expected_repository:
        cmd.extend(["--expected-repository", expected_repository])
    if use_manifest:
        cmd.extend(["--cca-evidence-manifest", str(target / ".shiki" / "gha" / "cca-evidence-manifest.json")])
    if allow_missing_cca:
        cmd.append("--allow-missing-cca")
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def expect_ready(target: pathlib.Path, **kwargs) -> None:
    result = run_mergegate(target, **kwargs)
    if result.returncode != 0:
        raise AssertionError(result.stdout)
    assert read_json(target / ".shiki" / "gha" / "mergegate-result.json")["mergegate"] == "ready"


def expect_block(target: pathlib.Path, needle: str, **kwargs) -> None:
    result = run_mergegate(target, **kwargs)
    if result.returncode == 0:
        raise AssertionError(f"expected MergeGate block containing {needle!r}, got success")
    assert_contains(result.stdout, needle)


# Group A: forged Guardian evidence must not satisfy policy-backed approval.
policy = load_guardian_policy(root)
guardian_pr = pr_payload()
valid_guardian = evaluate_guardian_approval(
    policy=policy,
    pr=guardian_pr,
    reviews=[],
    comments=[{"author": {"login": "mizutani-140"}, "body": f"Guardian approval granted for head {HEAD}"}],
    label_events=[{"event": "labeled", "actor": {"login": "mizutani-140"}, "label": {"name": "guardian:approved"}}],
    head_sha=HEAD,
)
assert valid_guardian.approved

for label, comments, events, reviews, pr in [
    ("label-only", [], [{"event": "labeled", "actor": {"login": "mizutani-140"}, "label": {"name": "guardian:approved"}}], [], guardian_pr),
    ("negative-text", [{"author": {"login": "mizutani-140"}, "body": "no Guardian approval evidence is present"}], [{"event": "labeled", "actor": {"login": "mizutani-140"}, "label": {"name": "guardian:approved"}}], [], guardian_pr),
    ("wrong-actor-comment", [{"author": {"login": "mallory"}, "body": f"Guardian approval granted for head {HEAD}"}], [{"event": "labeled", "actor": {"login": "mizutani-140"}, "label": {"name": "guardian:approved"}}], [], guardian_pr),
    ("wrong-actor-label", [{"author": {"login": "mizutani-140"}, "body": f"Guardian approval granted for head {HEAD}"}], [{"event": "labeled", "actor": {"login": "mallory"}, "label": {"name": "guardian:approved"}}], [], guardian_pr),
    ("stale-head-comment", [{"author": {"login": "mizutani-140"}, "body": f"Guardian approval granted for head {OLD_HEAD}"}], [{"event": "labeled", "actor": {"login": "mizutani-140"}, "label": {"name": "guardian:approved"}}], [], guardian_pr),
    ("review-bridge-only", [], [{"event": "labeled", "actor": {"login": "mizutani-140"}, "label": {"name": "guardian:approved"}}], [{"state": "APPROVED", "author": {"login": "github-actions"}}], guardian_pr),
    ("claude-review-only", [], [{"event": "labeled", "actor": {"login": "mizutani-140"}, "label": {"name": "guardian:approved"}}], [{"state": "APPROVED", "author": {"login": "claude-code"}}], guardian_pr),
]:
    result = evaluate_guardian_approval(policy=policy, pr=pr, reviews=reviews, comments=comments, label_events=events, head_sha=HEAD)
    if result.approved:
        raise AssertionError(f"forged Guardian fixture unexpectedly approved: {label}")

expect_block(
    make_fixture("mg-missing-guardian-files", write_guardian_files=False),
    "Guardian comments evidence file is missing",
)
expect_block(
    make_fixture(
        "mg-guardian-negative-text",
        guardian_comments=[{"author": {"login": "mizutani-140"}, "body": "no Guardian approval evidence is present"}],
    ),
    "Guardian approval requires a configured Guardian review or current-head Guardian comment",
)
expect_block(
    make_fixture(
        "mg-guardian-stale-head",
        guardian_comments=[{"author": {"login": "mizutani-140"}, "body": f"Guardian approval granted for head {OLD_HEAD}"}],
    ),
    "Guardian approval comment does not reference current head SHA",
)


# Group B: forged CCA verdict or manifest evidence must not satisfy MergeGate.
base = make_fixture("cca-valid-manifest")
add_cca_manifest(base)
expect_ready(base, expected_repository="mizutani-140/shiki", use_manifest=True)

for name, mutate, needle in [
    ("wrong-repository", lambda m: m.update({"repository": "attacker/repo"}), "CCA evidence manifest repository"),
    ("wrong-pr", lambda m: m.update({"pr": 778}), "CCA evidence manifest pr"),
    ("wrong-head", lambda m: m.update({"head_sha": OLD_HEAD}), "CCA evidence manifest head_sha does not match current PR headRefOid"),
    ("wrong-verdict-head", lambda m: m["verdict"].update({"head_sha": OLD_HEAD}), "CCA evidence manifest verdict.head_sha"),
    ("wrong-task", lambda m: m["verdict"].update({"task_id": "T-9999"}), "CCA evidence manifest verdict.task_id"),
    ("wrong-artifact", lambda m: m["artifact"].update({"name": "forged-artifact"}), "CCA evidence manifest artifact.name must be shiki-cca-evidence"),
    ("missing-entry", lambda m: m.update({"files": [entry for entry in m["files"] if entry["path"] != ".shiki/gha/pr.json"]}), "CCA evidence manifest missing required file entry .shiki/gha/pr.json"),
]:
    fixture = make_fixture(f"cca-{name}")
    manifest_path = add_cca_manifest(fixture)
    manifest = read_json(manifest_path)
    mutate(manifest)
    write_json(manifest_path, manifest)
    expect_block(fixture, needle, expected_repository="mizutani-140/shiki", use_manifest=True)

fixture = make_fixture("cca-digest-mismatch")
manifest_path = add_cca_manifest(fixture)
manifest = read_json(manifest_path)
manifest["files"][0]["sha256"] = "0" * 64
write_json(manifest_path, manifest)
expect_block(fixture, "CCA evidence manifest digest mismatch", expected_repository="mizutani-140/shiki", use_manifest=True)

expect_block(
    make_fixture("cca-missing-manifest"),
    "CCA evidence manifest file not found",
    expected_repository="mizutani-140/shiki",
    use_manifest=True,
)
expect_block(make_fixture("cca-noncomplete", cca=cca_payload(verdict="repair_required")), "CCA verdict is not complete")
expect_block(make_fixture("cca-stale-head", cca=cca_payload(head=OLD_HEAD)), "CCA head_sha does not match the current PR headRefOid")
expect_block(make_fixture("cca-wrong-pr", cca=cca_payload(number=778)), "CCA pr 778 does not match PR #777")


# Group C: forged ledger refs and integrity must fail validation or MergeGate.
ledger = ledger_payload()
refs = evidence_reference_for_ledger(
    ledger_entry=ledger,
    pr=PR_NUMBER,
    head_sha=HEAD,
    workflow_run_id="123456",
    artifact_name=CCA_EVIDENCE_ARTIFACT_NAME,
)
ledger.update(refs)
ledger["ledger_integrity"] = {"algorithm": "sha256", "canonical_digest": "0" * 64}
digest = ledger_entry_digest(ledger)
ledger["ledger_integrity"]["canonical_digest"] = digest
ledger["evidence_refs"][-1]["digest"] = digest
assert validate_evidence_refs(ledger["evidence_refs"]) == []
assert validate_ledger_integrity(ledger) == []

tampered = copy.deepcopy(ledger)
tampered["summary"] = "tampered after digest"
assert_errors(validate_ledger_integrity(tampered), "canonical ledger digest")
bad_ref = copy.deepcopy(ledger)
bad_ref["evidence_refs"][0]["pr"] = "777"
assert_errors(validate_ledger_integrity(bad_ref), "pr must be an integer")
bad_digest_ref = copy.deepcopy(ledger)
bad_digest_ref["evidence_refs"][-1]["digest"] = "f" * 64
assert_errors(validate_ledger_integrity(bad_digest_ref), "digest does not match canonical ledger digest")
unknown_ref = copy.deepcopy(ledger)
unknown_ref["evidence_refs"].append({"kind": "loose-text", "value": "PR #777"})
assert_errors(validate_ledger_integrity(unknown_ref), "kind is unsupported")

wrong_file_ledger_id = "L-20260605T061501000000Z-6a4c9e32"
expect_block(
    make_fixture(
        "ledger-filename-mismatch",
        changed=[f".shiki/ledger/{wrong_file_ledger_id}.json"],
        task=task_payload(locks=["path:.shiki/ledger/**"], ledger_ids=[wrong_file_ledger_id]),
        ledgers={wrong_file_ledger_id: {**ledger_payload(wrong_file_ledger_id), "id": "L-20260605T061502000000Z-6a4c9e33"}},
    ),
    "Ledger filename .shiki/ledger/L-20260605T061501000000Z-6a4c9e32.json does not match JSON id",
)
expect_block(
    make_fixture(
        "ledger-not-listed",
        changed=[".shiki/ledger/L-20260605T061503000000Z-6a4c9e34.json"],
        task=task_payload(locks=["path:.shiki/ledger/**"], ledger_ids=[LEDGER_ID]),
        ledgers={"L-20260605T061503000000Z-6a4c9e34": ledger_payload("L-20260605T061503000000Z-6a4c9e34")},
    ),
    "not listed in current task ledger_evidence",
)
expect_block(
    make_fixture(
        "ledger-unscoped",
        changed=[".shiki/ledger/L-20260605T061504000000Z-6a4c9e35.json"],
        task=task_payload(locks=["path:.shiki/ledger/**"], ledger_ids=["L-20260605T061504000000Z-6a4c9e35"]),
        ledgers={"L-20260605T061504000000Z-6a4c9e35": ledger_payload("L-20260605T061504000000Z-6a4c9e35", task_id="T-9999")},
    ),
    "is not scoped to task T-0049 or goal G-0012",
)


# Group D: stale mirror state cannot override current PR, task, branch, head, and checks.
expect_block(make_fixture("stale-expected-pr", task=task_payload(locks=["path:scripts/test_shiki_governance_evidence.sh"], expected_pr=778)), "Task expected_pr 778 does not match PR #777")
expect_block(
    make_fixture("stale-branch", task={**task_payload(locks=["path:scripts/test_shiki_governance_evidence.sh"]), "expected_branch": "old-branch"}),
    "Task expected_branch 'old-branch' does not match PR head",
)
expect_block(make_fixture("stale-expected-head"), "PR headRefOid", expected_head=OLD_HEAD)
expect_block(
    make_fixture(
        "stale-check-head",
        pr=pr_payload(
            checks=[
                {"name": "Validate Shiki mirror", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": OLD_HEAD},
                {"name": "MergeGate metadata check", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": HEAD},
            ]
        ),
    ),
    "Required check Validate Shiki mirror head SHA",
)
expect_block(
    make_fixture("stale-task-status", task=task_payload(locks=["path:scripts/test_shiki_governance_evidence.sh"], status="planned")),
    "Task status must be review or done after CCA verdict",
)


# Group E: untrusted PR mutations are blocked by state class, not trusted as evidence.
state_manifest = copy.deepcopy(read_json(root / ".shiki" / "manifest.json"))
state_manifest["directories"][".shiki/cache"] = {
    "kind": "cache",
    "state_class": "cache",
    "tracked": False,
    "required": False,
    "description": "Fixture cache directory.",
}
state_manifest["directories"][".shiki/local"] = {
    "kind": "local-only",
    "state_class": "local-only",
    "tracked": False,
    "required": False,
    "description": "Fixture local-only directory.",
}
expect_block(
    make_fixture("runtime-gha-mutation", locks=["path:.shiki/**"], changed=[".shiki/gha/pr.json"]),
    "must come from workflow artifacts, not PR files",
)
expect_block(
    make_fixture("cache-mutation", locks=["path:.shiki/cache/**"], changed=[".shiki/cache/local.json"], manifest=state_manifest),
    "PR must not change .shiki/cache/local.json; state_class=cache",
)
expect_block(
    make_fixture("local-only-mutation", locks=["path:.shiki/local/**"], changed=[".shiki/local/runtime.json"], manifest=state_manifest),
    "state_class=local-only",
)
expect_block(
    make_fixture("unknown-state-path", locks=["path:.shiki/**"], changed=[".shiki/unknown/evidence.json"]),
    "Unknown Shiki state path .shiki/unknown/evidence.json",
)
assert classify_shiki_path(".shiki/gha/pr.json", read_json(root / ".shiki" / "manifest.json")) == "workflow-runtime-evidence"
assert class_policy("workflow-runtime-evidence", read_json(root / ".shiki" / "manifest.json"))["pr_mutation"] == "forbidden"


# Group F: missing evidence remains blocking.
missing_cca = make_fixture("missing-cca")
(missing_cca / ".shiki" / "gha" / "cca-verdict.json").unlink()
expect_block(missing_cca, "CCA verdict file not found", allow_missing_cca=False)
expect_block(
    make_fixture("empty-acceptance", cca={**cca_payload(), "acceptance": []}),
    "CCA verdict acceptance evidence is empty",
)
expect_block(
    make_fixture("missing-ledger", task={**task_payload(locks=["path:scripts/test_shiki_governance_evidence.sh"]), "ledger_evidence": []}),
    "Task has no ledger evidence entries",
)
expect_block(
    make_fixture(
        "missing-required-check",
        pr=pr_payload(checks=[{"name": "Validate Shiki mirror", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": HEAD}]),
    ),
    "Required check MergeGate metadata check is missing from PR statusCheckRollup",
)
expect_block(
    make_fixture("missing-review", pr={**pr_payload(reviews=[], labels=[{"name": "guardian:approved"}], checks=None), "reviewDecision": ""}),
    "Required review is missing",
)


# Group G: exact Guardian evidence comments are required.
expect_block(
    make_fixture(
        "guardian-close-marker",
        guardian_comments=[{"author": {"login": "mizutani-140"}, "body": f"Guardian approved for head {HEAD}"}],
    ),
    "Guardian approval requires a configured Guardian review or current-head Guardian comment",
)
expect_ready(
    make_fixture(
        "guardian-exact-marker",
        guardian_comments=[{"author": {"login": "mizutani-140"}, "body": f"Guardian approval granted for head {HEAD}"}],
    )
)
expect_block(
    make_fixture(
        "guardian-label-missing",
        pr=pr_payload(labels=[]),
        guardian_comments=[{"author": {"login": "mizutani-140"}, "body": f"Guardian approval granted for head {HEAD}"}],
        guardian_events=[{"event": "labeled", "actor": {"login": "mizutani-140"}, "label": {"name": "guardian:approved"}}],
    ),
    "Guardian label 'guardian:approved' is missing",
)


# Group H: workflow and static contract checks keep governance evidence wired.
workflow_text = (root / ".github" / "workflows" / "shiki-cca-completion.yml").read_text(encoding="utf-8")
for needle in [
    "live-guardian-comments.json",
    "live-guardian-events.json",
    "live-guardian-timeline.json",
    "Build CCA evidence manifest",
    "shiki-cca-evidence",
    "--cca-evidence-manifest .shiki/gha/cca-evidence-manifest.json",
    "CCA Review Bridge approval",
]:
    assert_contains(workflow_text, needle)

mergegate_text = (root / "scripts" / "mergegate_check.py").read_text(encoding="utf-8")
for needle in [
    "validate_cca_evidence_manifest",
    "enforce_guardian_policy",
    "Runtime CCA/MergeGate evidence path",
    "Required review is missing",
    "statusCheckRollup",
]:
    assert_contains(mergegate_text, needle)

installer_text = (root / "scripts" / "shiki_installer.py").read_text(encoding="utf-8")
assert_contains(installer_text, "scripts/test_shiki_governance_evidence.sh")

print("governance evidence adversarial fixtures passed")
PY
