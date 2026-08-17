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
                    "body": f"Guardian approval granted\n\n{HEAD}",
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


def run_mergegate(target: pathlib.Path, *, expected_head: str = HEAD, expected_repository: str = "", use_manifest: bool = False, allow_missing_cca: bool = False, base_shiki: str = "", merged_prs: str = "") -> subprocess.CompletedProcess:
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
    if base_shiki:
        cmd.extend(["--base-shiki", base_shiki])
    if merged_prs:
        cmd.extend(["--merged-prs", merged_prs])
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
    comments=[{"author": {"login": "mizutani-140"}, "body": f"Guardian approval granted\n\n{HEAD}"}],
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
# A negated marker that embeds the exact approval phrase mid-sentence must still
# block the FULL gate (exit 1): line-initial + negation detection means it can
# never satisfy Guardian approval.
expect_block(
    make_fixture(
        "mg-guardian-negated-marker",
        guardian_comments=[
            {
                "author": {"login": "mizutani-140"},
                "body": f"No Guardian approval granted for head {HEAD}. This critical change is NOT authorized to merge.",
            }
        ],
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


# Group A2 (T-20260801T003238841526Z-9f3f6f63): the Guardian requirement SURVIVES a
# self-lowered risk_level on the NORMAL task-PR path. The gate resolves the task's
# risk never-weaker-than-base, so a PR that drops its OWN risk_level critical -> low
# still forces Guardian evaluation and blocks for missing approval. Assert the
# guardian OUTCOME (a required-but-unapproved block), not merely that the PR blocks,
# so a future change that blocks for an unrelated reason cannot mask a regression.
# The suite's guardian-policy applies_to_risk = [high, critical].
_NO_GUARDIAN_APPROVAL = [{"author": {"login": "mizutani-140"}, "body": "no Guardian approval evidence is present"}]
_GUARDIAN_REQUIRED_NEEDLE = "Guardian approval requires a configured Guardian review or current-head Guardian comment"


def _with_base_task(target: pathlib.Path, *, base_risk: str) -> str:
    base_root = target / ".shiki" / "gha" / "base-shiki" / ".shiki"
    write_json(
        base_root / "tasks" / f"{TASK_ID}.json",
        {**task_payload(locks=["path:scripts/test_shiki_governance_evidence.sh"]), "risk_level": base_risk, "status": "planned"},
    )
    return str(base_root)


# Head lowers its own risk critical -> low, own task file changed, non-approving
# guardian evidence: the gate resolves at the base (critical) risk and blocks for
# missing approval, and the immutability gate independently blocks the change.
_dg = make_fixture(
    "mg-risk-downgrade-guardian-survives",
    task={**task_payload(locks=["path:scripts/test_shiki_governance_evidence.sh"]), "risk_level": "low"},
    changed=[f".shiki/tasks/{TASK_ID}.json"],
    status_entries=[f"M\t.shiki/tasks/{TASK_ID}.json"],
    guardian_comments=_NO_GUARDIAN_APPROVAL,
    guardian_events=[],
)
_dg_res = run_mergegate(_dg, base_shiki=_with_base_task(_dg, base_risk="critical"))
if _dg_res.returncode == 0:
    raise AssertionError(f"expected the self-lowered-risk PR to block:\n{_dg_res.stdout}")
assert_contains(_dg_res.stdout, _GUARDIAN_REQUIRED_NEEDLE)  # guardian resolved at base (critical) risk
assert_contains(_dg_res.stdout, "risk_level")              # immutability gate blocked the change
# Control: a low-risk head that MATCHES a low-risk base does NOT force a Guardian
# (policy applies_to_risk = [high, critical]) — the guardian-required block is
# ABSENT — proving the never-weaker resolution (not the head risk) keeps the
# requirement alive above.
_ctrl = make_fixture(
    "mg-risk-low-matched-no-guardian",
    task={**task_payload(locks=["path:scripts/test_shiki_governance_evidence.sh"]), "risk_level": "low"},
    changed=[f".shiki/tasks/{TASK_ID}.json"],
    status_entries=[f"M\t.shiki/tasks/{TASK_ID}.json"],
    guardian_comments=_NO_GUARDIAN_APPROVAL,
    guardian_events=[],
)
_ctrl_res = run_mergegate(_ctrl, base_shiki=_with_base_task(_ctrl, base_risk="low"))
if _GUARDIAN_REQUIRED_NEEDLE in _ctrl_res.stdout:
    raise AssertionError(f"control: a low-risk head matching a low-risk base must not force a Guardian:\n{_ctrl_res.stdout}")


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


# Group B2: a complete CCA verdict must agree with its own evidence. A failing
# acceptance entry and can_merge != true each become a blocking reason (can_merge
# is no longer a mere warning); both appear together on the same contradictory
# verdict.
contradictory = make_fixture(
    "cca-complete-contradictory",
    cca={
        **cca_payload(),
        "can_merge": False,
        "acceptance": [
            {"criterion": "A1", "status": "pass", "evidence": ["ok"]},
            {"criterion": "A2-regressed", "status": "fail", "evidence": ["behaviour regressed"]},
        ],
    },
)
contradictory_result = run_mergegate(contradictory)
assert contradictory_result.returncode != 0, contradictory_result.stdout
assert_contains(contradictory_result.stdout, "A2-regressed")
assert_contains(contradictory_result.stdout, "can_merge=true")

# An insufficient_evidence acceptance entry is equally contradictory on complete.
expect_block(
    make_fixture(
        "cca-complete-unproven-acceptance",
        cca={
            **cca_payload(),
            "acceptance": [
                {"criterion": "A9", "status": "insufficient_evidence", "evidence": [], "reason": "no output recorded"},
            ],
        },
    ),
    "complete CCA verdict contains failing acceptance criteria: A9",
)


# Group B3: the task's cca_checklist_profile must be covered by terminal statuses.
profile_task = {**task_payload(locks=["path:scripts/test_shiki_governance_evidence.sh"]), "cca_checklist_profile": ["CCA-01"]}
# Every profile id present and judged pass (a terminal status) -> ready.
expect_ready(make_fixture("cca-profile-covered", task=profile_task))
# An empty checklist against a non-empty profile leaves the required id uncovered -> block.
expect_block(
    make_fixture("cca-profile-empty-checklist", task=profile_task, cca={**cca_payload(), "checklist": []}),
    "CCA checklist profile id CCA-01 is missing from the verdict checklist",
)
# A required id present only at insufficient_evidence is non-terminal -> block.
expect_block(
    make_fixture(
        "cca-profile-non-terminal",
        task=profile_task,
        cca={**cca_payload(), "checklist": [{"id": "CCA-01", "status": "insufficient_evidence", "blocking": False, "reason": "no proof"}]},
    ),
    "CCA checklist profile id CCA-01 has non-terminal status",
)


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
        guardian_comments=[{"author": {"login": "mizutani-140"}, "body": f"Guardian approval granted\n\n{HEAD}"}],
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


# ============================================================================
# SADR-0017: bookkeeping-closeout Guardian exemption smuggling cases.
#
# A closeout PR carries only a merged task's terminal state (task done, lock
# released, and — when it completes the goal — goal complete + exactly one
# scorecard) with zero implementation, so a PROVEN bookkeeping closeout is
# evaluated as if the task were low risk at the single Guardian decision point.
# Each smuggling case below is a CRITICAL-risk closeout with ONE defect that must
# disqualify it, so the task re-inherits its risk and — with NO approval evidence
# present — the Guardian approval requirement fires. We assert the SPECIFIC
# Guardian approval fallback, never merely that the PR blocks, so an unrelated
# future block cannot mask a classifier regression. The clean control proves the
# classifier CAN exempt (the same critical PR, with no approval, does NOT block on
# Guardian).
# ============================================================================
CLOSE_TASK = "T-20260731T004111132162Z-c105e001"
CLOSE_GOAL = "G-20260731T004111130646Z-c105e0a1"
CLOSE_IMPL_PR = 940
CLOSE_PR = 941
CLOSE_HEAD = "c" * 40
CLOSE_BRANCH = "shiki/t-c105e001-closeout"
CLOSE_COMP = "L-20260731T010000000000Z-c105ec3d"
CLOSE_PULL = "L-20260731T010000000001Z-c105e91d"
CLOSE_REPORT_ID = "R-20260731T010000000002Z-c105e5cd"
CLOSE_REPORT_REL = f".shiki/reports/{CLOSE_REPORT_ID}.json"
CLOSE_URL = f"https://github.com/mizutani-140/shiki/pull/{CLOSE_PR}"
GUARDIAN_APPROVAL_FALLBACK = "Guardian approval requires a configured Guardian review or current-head Guardian comment"

CLOSE_GOVERNANCE = {
    "title": "Closeout smuggling fixture",
    "scope": "Push the merged task's terminal state to main.",
    "non_goals": ["No code change."],
    "dependencies": [],
    "locks": ["path:scripts/close_fixture_placeholder.py"],
    "assigned_runtime": "claude-code",
    "risk_level": "critical",
    "required_skills": [],
    "acceptance_checks": ["task done, lock released, goal complete"],
    "test_command": "python3 -m unittest discover -s tests",
    "expected_branch": CLOSE_BRANCH,
}


def close_pr() -> dict:
    return {
        "number": CLOSE_PR,
        "title": "Closeout smuggling fixture",
        "body": "\n".join([
            "## Scope", f"Task {CLOSE_TASK} for Goal {CLOSE_GOAL}.",
            "## Acceptance", "Terminal state pushed to main.",
            "## Evidence", f"Closeout PR #{CLOSE_PR}; {CLOSE_URL}.",
            "## MergeGate", "Ready when checks pass.",
        ]),
        "author": {"login": "mizutani-140"},
        "headRefName": CLOSE_BRANCH,
        "baseRefName": "main",
        "headRefOid": CLOSE_HEAD,
        "isCrossRepository": False,
        "labels": [],
        "reviews": [{"state": "APPROVED", "author": {"login": "github-actions"}}],
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {"name": "Validate Shiki mirror", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": CLOSE_HEAD},
            {"name": "MergeGate metadata check", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": CLOSE_HEAD},
        ],
    }


def close_cca() -> dict:
    return {
        "verdict": "complete", "summary": "closeout complete",
        "goal_id": CLOSE_GOAL, "task_id": CLOSE_TASK, "pr": CLOSE_PR, "head_sha": CLOSE_HEAD,
        "can_merge": True,
        "checklist": [{"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "closeout"}],
        "acceptance": [{"criterion": "terminal state", "status": "pass", "evidence": ["closeout"]}],
        "mergegate": {"closeout": "pass"}, "confidence": 1.0, "repair_packet": None,
    }


def build_closeout(
    name: str,
    *,
    completes: bool = True,
    head_mutate=None,
    goal_head_status: str = "complete",
    lock_head_state: str = "released",
    base_lock_state: str = "active",
    extra_changed: list[tuple[str, str]] | None = None,
    comp_evidence: list[str] | None = None,
    extra_target_files: dict[str, dict] | None = None,
) -> pathlib.Path:
    target = tmp_root / name
    if target.exists():
        shutil.rmtree(target)
    copy_base_support(target)
    S = target / ".shiki"
    B = S / "gha" / "base-shiki" / ".shiki"

    write_json(B / "tasks" / f"{CLOSE_TASK}.json",
               {"id": CLOSE_TASK, "goal_id": CLOSE_GOAL, **CLOSE_GOVERNANCE,
                "status": "review", "expected_pr": CLOSE_IMPL_PR, "ledger_evidence": ["L-close-base"]})
    write_json(B / "locks" / f"{CLOSE_TASK}.json",
               {"task_id": CLOSE_TASK, "goal_id": CLOSE_GOAL, "state": base_lock_state, "owner": "shiki-run"})
    write_json(B / "goals" / f"{CLOSE_GOAL}.json",
               {"id": CLOSE_GOAL, "status": "planned", "title": "g", "risk_level": "critical",
                "ledger_evidence": []})
    (B / "ledger").mkdir(parents=True, exist_ok=True)
    (B / "reports").mkdir(parents=True, exist_ok=True)
    close_base_ledger = {"id": "L-close-base", "goal_id": CLOSE_GOAL, "task_id": CLOSE_TASK,
                         "type": "check", "summary": "implementation merged", "evidence": []}
    write_json(B / "ledger" / "L-close-base.json", close_base_ledger)
    write_json(S / "ledger" / "L-close-base.json", close_base_ledger)

    head_ledgers = ["L-close-base", CLOSE_COMP, CLOSE_PULL] if completes else ["L-close-base", CLOSE_PULL]
    head_task = {"id": CLOSE_TASK, "goal_id": CLOSE_GOAL, **CLOSE_GOVERNANCE,
                 "status": "done", "expected_pr": CLOSE_PR,
                 "ledger_evidence": head_ledgers}
    if head_mutate:
        head_mutate(head_task)
    write_json(S / "tasks" / f"{CLOSE_TASK}.json", head_task)
    write_json(S / "locks" / f"{CLOSE_TASK}.json",
               {"task_id": CLOSE_TASK, "goal_id": CLOSE_GOAL, "state": lock_head_state, "owner": "shiki-run"})
    write_json(S / "goals" / f"{CLOSE_GOAL}.json",
               {"id": CLOSE_GOAL, "status": goal_head_status if completes else "planned", "title": "g",
                "risk_level": "critical", "ledger_evidence": [CLOSE_COMP] if completes else []})
    write_json(S / "ledger" / f"{CLOSE_PULL}.json",
               {"id": CLOSE_PULL, "goal_id": CLOSE_GOAL, "task_id": CLOSE_TASK, "type": "lock",
                "summary": f"closeout /pull PR #{CLOSE_PR}",
                "evidence": [f".shiki/tasks/{CLOSE_TASK}.json", f".shiki/locks/{CLOSE_TASK}.json"],
                "links": [CLOSE_URL]})

    if completes:
        write_json(S / "dag" / f"{CLOSE_GOAL}.json", {"goal_id": CLOSE_GOAL, "nodes": [CLOSE_TASK], "edges": []})
        write_json(S / "ledger" / f"{CLOSE_COMP}.json",
                   {"id": CLOSE_COMP, "goal_id": CLOSE_GOAL, "task_id": None, "type": "completion",
                    "summary": "goal complete", "evidence": comp_evidence or [CLOSE_REPORT_REL]})
        write_json(target / CLOSE_REPORT_REL, {
            "id": CLOSE_REPORT_ID, "goal_id": CLOSE_GOAL, "status": "complete",
            "evidence": [f".shiki/tasks/{CLOSE_TASK}.json"], "blocking_reasons": [],
            "mergegate": {"dependencies": "pass", "locks": "pass", "checks": "pass",
                          "review": "recorded", "ledger": "pass", "risk": "critical"},
            "scorecard": {"goal_id": CLOSE_GOAL,
                          "tasks": {"completed": 1, "failed": 0, "total": 1}},
        })
        changed = [
            ("M", f".shiki/tasks/{CLOSE_TASK}.json"),
            ("M", f".shiki/locks/{CLOSE_TASK}.json"),
            ("M", f".shiki/goals/{CLOSE_GOAL}.json"),
            ("A", CLOSE_REPORT_REL),
            ("A", f".shiki/ledger/{CLOSE_COMP}.json"),
            ("A", f".shiki/ledger/{CLOSE_PULL}.json"),
        ]
    else:
        write_json(S / "dag" / f"{CLOSE_GOAL}.json",
                   {"goal_id": CLOSE_GOAL, "nodes": [CLOSE_TASK, f"{CLOSE_TASK}-sib"], "edges": []})
        write_json(S / "tasks" / f"{CLOSE_TASK}-sib.json",
                   {"id": f"{CLOSE_TASK}-sib", "goal_id": CLOSE_GOAL, "status": "running", "title": "s"})
        changed = [
            ("M", f".shiki/tasks/{CLOSE_TASK}.json"),
            ("M", f".shiki/locks/{CLOSE_TASK}.json"),
            ("A", f".shiki/ledger/{CLOSE_PULL}.json"),
        ]

    if extra_target_files:
        for rel, payload in extra_target_files.items():
            write_json(S / rel, payload)
    if extra_changed:
        changed = changed + extra_changed

    write_json(S / "gha" / "pr.json", close_pr())
    write_json(S / "gha" / "cca-verdict.json", close_cca())
    # No Guardian approval evidence: a non-exempted critical PR must fall back to
    # REQUIRING approval and block for its absence; an exempted one skips the gate.
    for gname in ("live-guardian-comments.json", "live-guardian-events.json", "live-guardian-timeline.json"):
        write_json(S / "gha" / gname, [])
    files = [path for _, path in changed]
    (S / "gha" / "changed-files.txt").write_text("\n".join(files) + "\n", encoding="utf-8")
    (S / "gha" / "changed-files-status.txt").write_text("\n".join(f"{s}\t{p}" for s, p in changed) + "\n", encoding="utf-8")
    return target


def close_run(target: pathlib.Path, *, merged: str = str(CLOSE_IMPL_PR)) -> subprocess.CompletedProcess:
    return run_mergegate(
        target,
        expected_head=CLOSE_HEAD,
        base_shiki=str(target / ".shiki" / "gha" / "base-shiki" / ".shiki"),
        merged_prs=merged,
    )


def expect_guardian_fallback(target: pathlib.Path, *, merged: str = str(CLOSE_IMPL_PR)) -> None:
    result = close_run(target, merged=merged)
    if result.returncode == 0:
        raise AssertionError(f"expected the Guardian approval fallback to block, got success:\n{result.stdout}")
    assert_contains(result.stdout, GUARDIAN_APPROVAL_FALLBACK)


def expect_exempted(target: pathlib.Path, *, merged: str = str(CLOSE_IMPL_PR)) -> None:
    result = close_run(target, merged=merged)
    if GUARDIAN_APPROVAL_FALLBACK in result.stdout:
        raise AssertionError(f"bookkeeping exemption did not fire; Guardian block present:\n{result.stdout}")


# Clean control: a proven completing closeout of a CRITICAL task with NO Guardian
# approval evidence is exempted, so the approval requirement does NOT fire.
expect_exempted(build_closeout("close-clean-completing"))
expect_exempted(build_closeout("close-clean-noncompleting", completes=False))

# Smuggling 1: one non-.shiki file (covered by a declared lock, so ONLY the
# under-.shiki rule can reject it).
expect_guardian_fallback(
    build_closeout("close-smuggle-nonshiki", extra_changed=[("M", "scripts/close_fixture_placeholder.py")])
)
# Smuggling 2: editing a governance field (acceptance_checks) of the task contract.
expect_guardian_fallback(
    build_closeout("close-smuggle-governance",
                   head_mutate=lambda t: t.__setitem__("acceptance_checks", ["a smuggled criterion"]))
)
# Smuggling 3: deleting a mirror record.
expect_guardian_fallback(
    build_closeout("close-smuggle-delete", extra_changed=[("D", f".shiki/worktrees/{CLOSE_TASK}.json")])
)
# Smuggling 4: setting a status outside the terminal set (goal -> blocked).
expect_guardian_fallback(build_closeout("close-smuggle-status", goal_head_status="blocked"))
# Smuggling 5: adding a second scorecard report (covered by the completion ledger
# so ONLY the one-report rule can reject it).
_second_report = ".shiki/reports/R-20260731T010000000003Z-c105e5ce.json"
expect_guardian_fallback(
    build_closeout(
        "close-smuggle-second-report",
        comp_evidence=[CLOSE_REPORT_REL, _second_report],
        extra_target_files={_second_report.split(".shiki/", 1)[1]: {"id": "R-20260731T010000000003Z-c105e5ce", "goal_id": CLOSE_GOAL}},
        extra_changed=[("A", _second_report)],
    )
)
# Smuggling 6: the task's implementation PR is not proven merged.
expect_guardian_fallback(build_closeout("close-smuggle-unmerged"), merged="")


# ============================================================================
# Amendment mode (SADR-0009/SADR-0015): a Spec Amendment PR cannot lower its OWN
# Guardian gate. The gate is forced from the STRONGER of the goal's base-plan and
# head-plan risk, so an amendment that WEAKENS its own plan (critical -> low)
# still resolves at the base (critical) risk and blocks for missing approval.
# End-to-end through the real gate: a well-formed, weakened amendment WITHOUT
# guardian approval blocks; the SAME amendment WITH approval is ready — proving
# the only thing gating the fires-case is the Guardian requirement the amendment
# tried to shed, not a malformed contract.
# ============================================================================
AMEND_GOAL = "G-20260804T111111111111Z-0000a0d1"
AMEND_PLAN = "P-20260804T111111111111Z-0000a0d2"
AMEND_T_A = "T-20260804T111111111111Z-0000a0a1"
AMEND_T_B = "T-20260804T111111111111Z-0000a0b2"
AMEND_LEDGER = "L-20260804T111111111111Z-0000a0c3"
AMEND_MARKER = "<!-- shiki:amendment -->"
NARROW_A = ["path:scripts/amend_a.py"]
WIDE_A = ["path:scripts/amend_a.py", "path:scripts/amend_a2.py"]
_A1 = {"id": "AMD-0001", "reason": "initial freeze"}
_A2 = {"id": "AMD-0002", "reason": "widen T_A locks"}


def _amend_plan(*, amendments, risk, a_locks):
    return {
        "id": AMEND_PLAN,
        "title": "amendment plan",
        "outcome": "corrected contract",
        "risk_level": risk,
        "spec_freeze": {"status": "frozen", "approved_by": "operator", "prd": "PRD-1", "source": "grill", "amendments": amendments},
        "tasks": [
            {"title": "Widen the narrow lock", "scope": "scope-a", "non_goals": ["ng-a"],
             "required_skills": ["tdd", "code-review"], "risk_level": "high", "locks": a_locks,
             "acceptance_checks": ["a"], "runtime": "claude-code"},
            {"title": "Downstream slice", "scope": "scope-b", "non_goals": ["ng-b"],
             "required_skills": ["tdd", "code-review"], "risk_level": "high", "locks": ["path:scripts/amend_b.py"],
             "acceptance_checks": ["b"], "runtime": "claude-code", "dependencies": ["Widen the narrow lock"]},
        ],
    }


def _amend_task_a(*, locks, status):
    return {
        "id": AMEND_T_A, "goal_id": AMEND_GOAL, "title": "Widen the narrow lock", "status": status,
        "scope": "scope-a", "non_goals": ["ng-a"], "required_skills": ["tdd", "code-review"],
        "risk_level": "high", "locks": locks, "acceptance_checks": ["a"], "assigned_runtime": "claude-code",
        "dependencies": [], "expected_pr": 100, "closeout_pr": None, "expected_branch": "shiki/amend-a",
        "ledger_evidence": ["L-20260804T111111111111Z-0000ba51"],
    }


def _amend_task_b():
    return {
        "id": AMEND_T_B, "goal_id": AMEND_GOAL, "title": "Downstream slice", "status": "planned",
        "scope": "scope-b", "non_goals": ["ng-b"], "required_skills": ["tdd", "code-review"],
        "risk_level": "high", "locks": ["path:scripts/amend_b.py"], "acceptance_checks": ["b"],
        "assigned_runtime": "claude-code", "dependencies": [AMEND_T_A], "expected_pr": 101,
        "closeout_pr": None, "expected_branch": "shiki/amend-b",
        "ledger_evidence": ["L-20260804T111111111111Z-0000ba52"],
    }


def _amend_dag():
    return {"goal_id": AMEND_GOAL, "nodes": [AMEND_T_A, AMEND_T_B],
            "edges": [{"from": AMEND_T_A, "to": AMEND_T_B, "reason": "declared plan dependency"}]}


def _amend_goal():
    return {"id": AMEND_GOAL, "status": "planned", "source_plan": AMEND_PLAN, "risk_level": "low", "title": "g"}


def make_amendment_fixture(name: str, *, approved: bool) -> pathlib.Path:
    """A well-formed amendment that WEAKENS its own plan risk (base critical ->
    head low). The Guardian evidence is the only knob: ``approved`` toggles a
    current-head approval vs a non-approving comment."""
    target = tmp_root / name
    if target.exists():
        shutil.rmtree(target)
    copy_base_support(target)
    # Head (the amendment): plan weakened to low, amendments appended, T_A locks widened + re-opened.
    write_json(target / ".shiki" / "goals" / f"{AMEND_GOAL}.json", _amend_goal())
    write_json(target / ".shiki" / "plans" / f"{AMEND_PLAN}.json", _amend_plan(amendments=[_A1, _A2], risk="low", a_locks=WIDE_A))
    write_json(target / ".shiki" / "tasks" / f"{AMEND_T_A}.json", _amend_task_a(locks=WIDE_A, status="planned"))
    write_json(target / ".shiki" / "tasks" / f"{AMEND_T_B}.json", _amend_task_b())
    write_json(target / ".shiki" / "dag" / f"{AMEND_GOAL}.json", _amend_dag())
    write_json(target / ".shiki" / "ledger" / f"{AMEND_LEDGER}.json",
               {"id": AMEND_LEDGER, "goal_id": AMEND_GOAL, "task_id": None, "type": "contract-amended",
                "actor": "operator", "timestamp": "2026-08-04T00:00:00Z", "summary": "Spec Amendment", "evidence": ["PR #777"]})
    # Base snapshot (pre-amendment): plan CRITICAL with one amendment, narrow T_A locks.
    base_root = target / ".shiki" / "gha" / "base-shiki" / ".shiki"
    write_json(base_root / "goals" / f"{AMEND_GOAL}.json", _amend_goal())
    write_json(base_root / "plans" / f"{AMEND_PLAN}.json", _amend_plan(amendments=[_A1], risk="critical", a_locks=NARROW_A))
    write_json(base_root / "tasks" / f"{AMEND_T_A}.json", _amend_task_a(locks=NARROW_A, status="review"))
    write_json(base_root / "tasks" / f"{AMEND_T_B}.json", _amend_task_b())
    write_json(base_root / "dag" / f"{AMEND_GOAL}.json", _amend_dag())
    # A goal-scoped Amendment PR (marker + label, no single task id).
    pr = {
        "number": PR_NUMBER,
        "title": "shiki: amend the frozen contract",
        "body": "\n".join([
            "## Scope", f"Amend Goal {AMEND_GOAL} source plan and re-open its task.",
            "## Acceptance", "The widened lock is registered.",
            "## Evidence", f"Ledger {AMEND_LEDGER}; PR #{PR_NUMBER}.",
            "## MergeGate", "Ready when the Guardian approves.", AMEND_MARKER,
        ]),
        "author": {"login": "mizutani-140"},
        "headRefName": "shiki/amend-goal", "baseRefName": "main", "headRefOid": HEAD,
        # The maintainer-applied mergegate:amendment label authorizes the mode; the
        # guardian:approved label is present only when the Guardian actually signed off.
        "labels": ([{"name": "mergegate:amendment"}, {"name": "guardian:approved"}]
                   if approved else [{"name": "mergegate:amendment"}]),
        "reviews": [{"state": "APPROVED", "author": {"login": "github-actions"}}],
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {"name": "Validate Shiki mirror", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": HEAD},
            {"name": "MergeGate metadata check", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": HEAD},
        ],
    }
    write_json(target / ".shiki" / "gha" / "pr.json", pr)
    changed = [f".shiki/plans/{AMEND_PLAN}.json", f".shiki/tasks/{AMEND_T_A}.json", f".shiki/ledger/{AMEND_LEDGER}.json"]
    (target / ".shiki" / "gha" / "changed-files.txt").write_text("\n".join(changed) + "\n", encoding="utf-8")
    status_lines = [f"M\t.shiki/plans/{AMEND_PLAN}.json", f"M\t.shiki/tasks/{AMEND_T_A}.json", f"A\t.shiki/ledger/{AMEND_LEDGER}.json"]
    (target / ".shiki" / "gha" / "changed-files-status.txt").write_text("\n".join(status_lines) + "\n", encoding="utf-8")
    if approved:
        comments = [{"author": {"login": "mizutani-140"}, "body": f"Guardian approval granted\n\n{HEAD}"}]
        events = [{"event": "labeled", "actor": {"login": "mizutani-140"}, "label": {"name": "guardian:approved"}}]
    else:
        comments = _NO_GUARDIAN_APPROVAL
        events = []
    write_json(target / ".shiki" / "gha" / "live-guardian-comments.json", comments)
    write_json(target / ".shiki" / "gha" / "live-guardian-events.json", events)
    write_json(target / ".shiki" / "gha" / "live-guardian-timeline.json", [])
    return target


_amend_base = str(tmp_root / "amend-guardian-fires" / ".shiki" / "gha" / "base-shiki" / ".shiki")
_amend_fires = make_amendment_fixture("amend-guardian-fires", approved=False)
_amend_fires_res = run_mergegate(_amend_fires, allow_missing_cca=True, base_shiki=_amend_base)
if _amend_fires_res.returncode == 0:
    raise AssertionError(f"expected the weakened amendment to block for missing Guardian approval:\n{_amend_fires_res.stdout}")
# The gate resolved at the base (critical) risk despite the head plan being low.
assert_contains(_amend_fires_res.stdout, _GUARDIAN_REQUIRED_NEEDLE)
# Control: the SAME amendment WITH a current-head Guardian approval is ready, so
# the fires-case blocked ONLY on the Guardian requirement, not a malformed contract.
_amend_ok_base = str(tmp_root / "amend-guardian-ok" / ".shiki" / "gha" / "base-shiki" / ".shiki")
expect_ready(make_amendment_fixture("amend-guardian-ok", approved=True), allow_missing_cca=True, base_shiki=_amend_ok_base)


print("governance evidence adversarial fixtures passed")
PY
