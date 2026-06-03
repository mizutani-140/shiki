#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-adversarial-state-test-$$"

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
import tempfile
import threading

root = pathlib.Path(sys.argv[1])
tmp_root = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root / "scripts"))

import shiki_locks
import shiki_state


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"expected {needle!r} in:\n{text}")


def write_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# Guards T-0034: collision-resistant state IDs and append-only ledger writes.
with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
    target = pathlib.Path(tmp)
    ledger_dir = target / ".shiki" / "ledger"
    task_dir = target / ".shiki" / "tasks"
    ledger_dir.mkdir(parents=True)
    task_dir.mkdir(parents=True)

    ids = {shiki_state.new_control_id("L") for _ in range(1000)}
    assert len(ids) == 1000

    for legacy in ["L-0001", "L-9999"]:
        write_json(ledger_dir / f"{legacy}.json", {"id": legacy})

    ledger_id = shiki_state.append_ledger_entry(
        target,
        lambda lid: {
            "id": lid,
            "timestamp": "2026-06-03T00:00:00Z",
            "goal_id": "G-0012",
            "task_id": "T-0037",
            "type": "check",
            "actor": "test",
            "summary": "append-only ledger test",
            "evidence": ["diagnose", "tdd", "PR #123"],
        },
    )
    assert not ledger_id.startswith("L-10000")
    assert (ledger_dir / f"{ledger_id}.json").exists()

    create_path = target / ".shiki" / "state" / "record.json"
    shiki_state.atomic_create_json(create_path, {"id": "record", "value": 1})
    before = create_path.read_text(encoding="utf-8")
    try:
        shiki_state.atomic_create_json(create_path, {"id": "record", "value": 2})
    except FileExistsError:
        pass
    else:
        raise AssertionError("atomic_create_json must fail on existing files")
    assert create_path.read_text(encoding="utf-8") == before
    assert not list(create_path.parent.glob("*.tmp"))

    original_new_control_id = shiki_state.new_control_id
    collision = "L-20260603T000000000000Z-deadbeef"
    retry = "L-20260603T000000000001Z-feedface"
    write_json(ledger_dir / f"{collision}.json", {"id": collision, "value": "original"})
    generated = iter([collision, retry])
    shiki_state.new_control_id = lambda prefix: next(generated)
    try:
        retry_id = shiki_state.append_ledger_entry(
            target,
            lambda lid: {
                "id": lid,
                "timestamp": "2026-06-03T00:00:00Z",
                "goal_id": "G-0012",
                "task_id": "T-0037",
                "type": "check",
                "actor": "test",
                "summary": "collision retry",
                "evidence": ["diagnose", "tdd", "PR #123"],
            },
            retries=2,
        )
    finally:
        shiki_state.new_control_id = original_new_control_id
    assert retry_id == retry
    assert read_json(ledger_dir / f"{collision}.json")["value"] == "original"

    shiki_state.new_control_id = lambda prefix: collision
    try:
        try:
            shiki_state.append_ledger_entry(
                target,
                lambda lid: {
                    "id": lid,
                    "timestamp": "2026-06-03T00:00:00Z",
                    "goal_id": "G-0012",
                    "task_id": "T-0037",
                    "type": "check",
                    "actor": "test",
                    "summary": "repeated collision",
                    "evidence": ["diagnose", "tdd", "PR #123"],
                },
                retries=3,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("repeated ledger ID collision must fail safely")
    finally:
        shiki_state.new_control_id = original_new_control_id
    assert read_json(ledger_dir / f"{collision}.json")["value"] == "original"

    created: list[str] = []
    created_lock = threading.Lock()

    def append_one() -> None:
        lid = shiki_state.append_ledger_entry(
            target,
            lambda candidate: {
                "id": candidate,
                "timestamp": "2026-06-03T00:00:00Z",
                "goal_id": "G-0012",
                "task_id": "T-0037",
                "type": "check",
                "actor": "test",
                "summary": f"parallel ledger append {candidate}",
                "evidence": ["diagnose", "tdd", "PR #123"],
            },
        )
        with created_lock:
            created.append(lid)

    threads = [threading.Thread(target=append_one) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(created) == len(set(created)) == 32
    for lid in created:
        payload = read_json(ledger_dir / f"{lid}.json")
        assert payload["id"] == lid

    task_ids = [shiki_state.new_control_id("T") for _ in range(2)]
    task_ledger_ids = [shiki_state.new_control_id("L") for _ in range(2)]
    assert len(set(task_ids)) == 2
    assert len(set(task_ledger_ids)) == 2
    for task_id in task_ids:
        shiki_state.atomic_create_json(task_dir / f"{task_id}.json", {"id": task_id})
    existing_task = task_dir / f"{task_ids[0]}.json"
    task_before = existing_task.read_text(encoding="utf-8")
    try:
        shiki_state.atomic_create_json(existing_task, {"id": task_ids[0], "mutated": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("task atomic create must not overwrite existing task file")
    assert existing_task.read_text(encoding="utf-8") == task_before


# Guards T-0035: exact, glob, prefix, and semantic lock overlap.
assert shiki_locks.locks_overlap("path:docs/**", "path:docs/agents/checklists.md")
assert shiki_locks.locks_overlap("path:scripts/*.py", "path:scripts/shiki.py")
assert shiki_locks.locks_overlap("path:.github/workflows/**", "path:.github/workflows/shiki-validate.yml")
assert shiki_locks.locks_overlap("shiki:state", "path:.shiki/tasks/T-0037.json")
assert shiki_locks.locks_overlap("shiki:governance", "path:scripts/mergegate_check.py")
assert shiki_locks.locks_overlap("shiki:workflows", "path:.github/workflows/shiki-validate.yml")
assert shiki_locks.locks_overlap("shiki:contracts", "path:AGENTS.md")
assert shiki_locks.locks_overlap("shiki:governance", "shiki:workflows")
assert not shiki_locks.locks_overlap("path:scripts/**", "path:docs/**")
assert not shiki_locks.locks_overlap("shiki:state", "path:docs/agents/checklists.md")

with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
    target = pathlib.Path(tmp)
    locks_dir = target / ".shiki" / "locks"
    locks_dir.mkdir(parents=True)

    def active_lock(task_id: str, locks: list[str]) -> None:
        write_json(
            locks_dir / f"{task_id}.json",
            {
                "task_id": task_id,
                "goal_id": "G-0012",
                "locks": locks,
                "state": "active",
                "owner": "fixture",
                "created_at": "2026-06-03T00:00:00Z",
            },
        )

    active_lock("T-9001", ["path:docs/**"])
    assert shiki_locks.active_lock_conflicts(target, "T-0037", ["path:docs/agents/checklists.md"], ["docs/agents/checklists.md"])
    assert not shiki_locks.active_lock_conflicts(target, "T-9001", ["path:docs/agents/checklists.md"], ["docs/agents/checklists.md"])

    (locks_dir / "T-9001.json").unlink()
    active_lock("T-9002", ["path:scripts/*.py"])
    assert shiki_locks.active_lock_conflicts(target, "T-0037", ["path:scripts/shiki.py"], ["scripts/shiki.py"])

    (locks_dir / "T-9002.json").unlink()
    active_lock("T-9003", ["shiki:state"])
    assert shiki_locks.active_lock_conflicts(target, "T-0037", ["path:.shiki/tasks/T-0037.json"], [".shiki/tasks/T-0037.json"])

    (locks_dir / "T-9003.json").unlink()
    active_lock("T-9004", ["shiki:governance"])
    assert shiki_locks.active_lock_conflicts(target, "T-0037", ["path:.github/workflows/shiki-validate.yml"], [".github/workflows/shiki-validate.yml"])
    assert not shiki_locks.active_lock_conflicts(target, "T-0037", ["path:docs/**"], ["docs/agents/checklists.md"])


HEAD = "abc123abc123abc123abc123abc123abc123abcd"
OTHER_HEAD = "def456def456def456def456def456def456def0"
PR_NUMBER = 123
TASK_ID = "T-0037"
GOAL_ID = "G-0012"
LEDGER_ID = "L-20260603T010203000000Z-a1b2c3d4"


def copy_base_support(target: pathlib.Path) -> None:
    shutil.copytree(root / ".shiki" / "schemas", target / ".shiki" / "schemas")
    shutil.copy2(root / ".shiki" / "config.yaml", target / ".shiki" / "config.yaml")
    shutil.copytree(root / ".github" / "workflows", target / ".github" / "workflows")
    if (root / ".github" / "CODEOWNERS").exists():
        shutil.copy2(root / ".github" / "CODEOWNERS", target / ".github" / "CODEOWNERS")


def goal_payload(goal_id: str = GOAL_ID) -> dict:
    return {
        "id": goal_id,
        "title": "Adversarial fixture goal",
        "outcome": "Adversarial fixtures pass",
        "acceptance_evidence": ["fixture"],
        "completion_conditions": ["fixture"],
        "non_goals": [],
        "required_skills": ["diagnose", "tdd"],
        "risk_level": "critical",
        "status": "planned",
    }


def ledger_payload(ledger_id: str, *, task_id: str | None = TASK_ID, goal_id: str = GOAL_ID, pr: int = PR_NUMBER) -> dict:
    payload = {
        "id": ledger_id,
        "timestamp": "2026-06-03T00:00:00Z",
        "goal_id": goal_id,
        "type": "check",
        "actor": "test",
        "summary": f"diagnose tdd adversarial fixture for PR #{pr}",
        "evidence": ["diagnose", "tdd", f"PR #{pr}", f"https://github.com/mizutani-140/shiki/pull/{pr}"],
        "links": [f"https://github.com/mizutani-140/shiki/pull/{pr}"],
    }
    if task_id is not None:
        payload["task_id"] = task_id
    return payload


def task_payload(
    *,
    locks: list[str],
    ledger_ids: list[str] | None = None,
    expected_pr: int | None = PR_NUMBER,
    expected_branch: str = "shiki/t-0037-adversarial-state-regression",
    status: str = "review",
) -> dict:
    return {
        "id": TASK_ID,
        "goal_id": GOAL_ID,
        "github_issue": 60,
        "title": "Add adversarial state / evidence / lock regression suite",
        "scope": "Add adversarial regression fixtures for P0.4.8.",
        "non_goals": ["No new policy behavior beyond bounded test-backed repair."],
        "dependencies": ["T-0036"],
        "locks": locks,
        "assigned_runtime": "codex",
        "risk_level": "critical",
        "required_skills": ["diagnose", "tdd"],
        "acceptance_checks": ["adversarial fixtures pass"],
        "expected_branch": expected_branch,
        "expected_pr": expected_pr,
        "ledger_evidence": ledger_ids or [LEDGER_ID],
        "status": status,
    }


def pr_payload(*, head: str = HEAD, number: int = PR_NUMBER, branch: str = "shiki/t-0037-adversarial-state-regression", checks: list[dict] | None = None) -> dict:
    return {
        "number": number,
        "title": "shiki: add adversarial state regression suite",
        "body": "\n".join(
            [
                "## Scope",
                f"Task {TASK_ID} for Goal {GOAL_ID}.",
                "## Acceptance",
                "Adversarial regression fixtures pass.",
                "## Evidence",
                f"Ledger {LEDGER_ID}; PR #{number}.",
                "## MergeGate",
                "Ready when checks pass.",
            ]
        ),
        "author": {"login": "mizutani-140"},
        "headRefName": branch,
        "baseRefName": "main",
        "headRefOid": head,
        "labels": [{"name": "guardian:approved"}],
        "reviews": [{"state": "APPROVED", "author": {"login": "github-actions"}}],
        "reviewDecision": "APPROVED",
        "statusCheckRollup": checks
        if checks is not None
        else [
            {"name": "Validate Shiki mirror", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": head},
            {"name": "MergeGate metadata check", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": head},
        ],
    }


def cca_payload(*, head: str = HEAD, number: int = PR_NUMBER, task_id: str = TASK_ID, goal_id: str = GOAL_ID) -> dict:
    return {
        "verdict": "complete",
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
    locks: list[str],
    changed: list[str],
    status_entries: list[str] | None = None,
    task: dict | None = None,
    ledger_entries: dict[str, dict] | None = None,
    pr: dict | None = None,
    cca: dict | None = None,
    base: dict[str, dict[str, dict]] | None = None,
) -> pathlib.Path:
    target = tmp_root / name
    if target.exists():
        shutil.rmtree(target)
    copy_base_support(target)
    write_json(target / ".shiki" / "goals" / f"{GOAL_ID}.json", goal_payload())
    write_json(
        target / ".shiki" / "tasks" / "T-0036.json",
        {
            "id": "T-0036",
            "goal_id": GOAL_ID,
            "title": "Completed dependency fixture",
            "scope": "Fixture dependency for T-0037 adversarial tests.",
            "non_goals": [],
            "dependencies": [],
            "locks": ["path:fixture"],
            "assigned_runtime": "codex",
            "risk_level": "critical",
            "required_skills": ["diagnose", "tdd"],
            "acceptance_checks": ["fixture"],
            "expected_branch": "main",
            "expected_pr": 59,
            "ledger_evidence": [LEDGER_ID],
            "status": "done",
        },
    )
    task_data = task or task_payload(locks=locks)
    write_json(target / ".shiki" / "tasks" / f"{TASK_ID}.json", task_data)
    ledgers = ledger_entries or {LEDGER_ID: ledger_payload(LEDGER_ID)}
    for lid, payload in ledgers.items():
        write_json(target / ".shiki" / "ledger" / f"{lid}.json", payload)
    write_json(target / ".shiki" / "gha" / "pr.json", pr or pr_payload())
    (target / ".shiki" / "gha" / "changed-files.txt").write_text("\n".join(changed) + ("\n" if changed else ""), encoding="utf-8")
    status_lines = status_entries if status_entries is not None else [f"M\t{path}" for path in changed]
    (target / ".shiki" / "gha" / "changed-files-status.txt").write_text("\n".join(status_lines) + ("\n" if status_lines else ""), encoding="utf-8")
    if cca is not None:
        write_json(target / ".shiki" / "gha" / "cca-verdict.json", cca)
    if base is not None:
        base_root = target / ".shiki" / "gha" / "base-shiki" / ".shiki"
        for subdir, records in base.items():
            for record_id, payload in records.items():
                write_json(base_root / subdir / f"{record_id}.json", payload)
    return target


def run_mergegate(target: pathlib.Path, *, allow_missing_cca: bool = True, expected_head: str = HEAD) -> subprocess.CompletedProcess:
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
        "--base-shiki",
        str(target / ".shiki" / "gha" / "base-shiki" / ".shiki"),
        "--result-file",
        str(target / ".shiki" / "gha" / "mergegate-result.json"),
    ]
    if allow_missing_cca:
        cmd.append("--allow-missing-cca")
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def expect_ready(target: pathlib.Path, *, allow_missing_cca: bool = True) -> None:
    result = run_mergegate(target, allow_missing_cca=allow_missing_cca)
    if result.returncode != 0:
        raise AssertionError(result.stdout)
    assert read_json(target / ".shiki" / "gha" / "mergegate-result.json")["mergegate"] == "ready"


def expect_block(target: pathlib.Path, needle: str, *, allow_missing_cca: bool = True, expected_head: str = HEAD) -> None:
    result = run_mergegate(target, allow_missing_cca=allow_missing_cca, expected_head=expected_head)
    if result.returncode == 0:
        raise AssertionError(f"expected MergeGate block containing {needle!r}, got success")
    assert_contains(result.stdout, needle)


def with_active_lock(target: pathlib.Path, task_id: str, locks: list[str]) -> None:
    write_json(
        target / ".shiki" / "locks" / f"{task_id}.json",
        {
            "task_id": task_id,
            "goal_id": GOAL_ID,
            "locks": locks,
            "state": "active",
            "owner": "fixture",
            "created_at": "2026-06-03T00:00:00Z",
        },
    )


# Guards T-0035: MergeGate and dispatch use the same lock conflict semantics.
fixture = make_fixture("mg-lock-docs", locks=["path:docs/agents/checklists.md"], changed=["docs/agents/checklists.md"])
with_active_lock(fixture, "T-9001", ["path:docs/**"])
expect_block(fixture, "Lock conflict: path:docs/** held by T-9001")

fixture = make_fixture("mg-lock-governance", locks=["shiki:governance"], changed=["scripts/mergegate_check.py"])
with_active_lock(fixture, "T-9002", ["path:scripts/mergegate_check.py"])
expect_block(fixture, "Lock conflict: path:scripts/mergegate_check.py held by T-9002")

expect_ready(make_fixture("mg-governance-covered", locks=["shiki:governance"], changed=["scripts/mergegate_check.py"]))
expect_ready(make_fixture("mg-workflows-covered", locks=["shiki:workflows"], changed=[".github/workflows/shiki-validate.yml"]))
expect_ready(make_fixture("mg-contracts-covered", locks=["shiki:contracts"], changed=["AGENTS.md"]))
expect_block(
    make_fixture("mg-outside-locks", locks=["path:docs/**"], changed=["scripts/mergegate_check.py"]),
    "Changed file scripts/mergegate_check.py is outside declared task locks",
)


# Guards T-0036: runtime CCA and MergeGate evidence must not be PR-controlled.
for evidence_path in [
    ".shiki/gha/pr.json",
    ".shiki/gha/cca-verdict.json",
    ".shiki/gha/mergegate-result.json",
    ".shiki/cca/cca-verdict.json",
    ".shiki/somewhere/cca-verdict.json",
    ".shiki/somewhere/mergegate-result.json",
]:
    expect_block(
        make_fixture(f"forged-runtime-{evidence_path.replace('/', '-').replace('.', '_')}", locks=["path:.shiki/**"], changed=[evidence_path]),
        "must come from workflow artifacts, not PR files",
    )


# Guards T-0036: protected task, goal, ledger, lock, and repair evidence cannot be forged.
expect_ready(make_fixture("current-task-change", locks=["shiki:state"], changed=[f".shiki/tasks/{TASK_ID}.json"]))
expect_block(
    make_fixture("unrelated-task-change", locks=["shiki:state"], changed=[".shiki/tasks/T-9999.json"]),
    "PR changes unrelated Shiki task file .shiki/tasks/T-9999.json",
)
expect_block(
    make_fixture("task-delete", locks=["shiki:state"], changed=[".shiki/tasks/T-9999.json"], status_entries=["D\t.shiki/tasks/T-9999.json"]),
    "PR must not delete Shiki task file .shiki/tasks/T-9999.json",
)
expect_ready(make_fixture("current-goal-change", locks=["shiki:state"], changed=[f".shiki/goals/{GOAL_ID}.json"]))
expect_block(
    make_fixture("unrelated-goal-change", locks=["shiki:state"], changed=[".shiki/goals/G-9999.json"]),
    "PR changes unrelated Shiki goal file .shiki/goals/G-9999.json",
)
expect_block(
    make_fixture("goal-delete", locks=["shiki:state"], changed=[".shiki/goals/G-9999.json"], status_entries=["D\t.shiki/goals/G-9999.json"]),
    "PR must not delete Shiki goal file .shiki/goals/G-9999.json",
)

new_ledger = "L-20260603T010203000001Z-b1b2c3d4"
expect_ready(
    make_fixture(
        "new-listed-ledger",
        locks=["shiki:state"],
        changed=[f".shiki/ledger/{new_ledger}.json"],
        task=task_payload(locks=["shiki:state"], ledger_ids=[LEDGER_ID, new_ledger]),
        ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID), new_ledger: ledger_payload(new_ledger)},
    )
)
expect_block(
    make_fixture(
        "new-unlisted-ledger",
        locks=["shiki:state"],
        changed=[f".shiki/ledger/{new_ledger}.json"],
        ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID), new_ledger: ledger_payload(new_ledger)},
    ),
    f"PR changes ledger {new_ledger} not listed in current task ledger_evidence",
)

base_ledger = "L-20260603T010203000002Z-c1b2c3d4"
base_payload = ledger_payload(base_ledger)
modified_payload = copy.deepcopy(base_payload)
modified_payload["summary"] = "mutated existing base ledger"
expect_block(
    make_fixture(
        "modified-base-ledger",
        locks=["shiki:state"],
        changed=[f".shiki/ledger/{base_ledger}.json"],
        task=task_payload(locks=["shiki:state"], ledger_ids=[LEDGER_ID, base_ledger]),
        ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID), base_ledger: modified_payload},
        base={"ledger": {base_ledger: base_payload}},
    ),
    f"PR must not modify existing base ledger file .shiki/ledger/{base_ledger}.json",
)
target = make_fixture(
    "deleted-base-ledger",
    locks=["shiki:state"],
    changed=[f".shiki/ledger/{base_ledger}.json"],
    status_entries=[f"D\t.shiki/ledger/{base_ledger}.json"],
    task=task_payload(locks=["shiki:state"], ledger_ids=[LEDGER_ID, base_ledger]),
    ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID)},
    base={"ledger": {base_ledger: base_payload}},
)
expect_block(target, f"PR must not delete Shiki ledger file .shiki/ledger/{base_ledger}.json")

mismatch = "L-20260603T010203000003Z-d1b2c3d4"
mismatch_payload = ledger_payload(mismatch)
mismatch_payload["id"] = "L-20260603T010203000004Z-e1b2c3d4"
expect_block(
    make_fixture(
        "ledger-id-mismatch",
        locks=["shiki:state"],
        changed=[f".shiki/ledger/{mismatch}.json"],
        task=task_payload(locks=["shiki:state"], ledger_ids=[LEDGER_ID, mismatch]),
        ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID), mismatch: mismatch_payload},
    ),
    "does not match JSON id",
)

wrong_task_ledger = "L-20260603T010203000005Z-f1b2c3d4"
expect_block(
    make_fixture(
        "ledger-task-mismatch",
        locks=["shiki:state"],
        changed=[f".shiki/ledger/{wrong_task_ledger}.json"],
        task=task_payload(locks=["shiki:state"], ledger_ids=[LEDGER_ID, wrong_task_ledger]),
        ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID), wrong_task_ledger: ledger_payload(wrong_task_ledger, task_id="T-9999")},
    ),
    f"Ledger {wrong_task_ledger} is not scoped to task {TASK_ID} or goal {GOAL_ID}",
)

goal_ledger = "L-20260603T010203000006Z-a2b2c3d4"
expect_ready(
    make_fixture(
        "goal-ledger-no-task",
        locks=["shiki:state"],
        changed=[f".shiki/ledger/{goal_ledger}.json"],
        task=task_payload(locks=["shiki:state"], ledger_ids=[LEDGER_ID, goal_ledger]),
        ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID), goal_ledger: ledger_payload(goal_ledger, task_id=None)},
    )
)
expect_block(
    make_fixture(
        "goal-ledger-conflicting-task",
        locks=["shiki:state"],
        changed=[f".shiki/ledger/{goal_ledger}.json"],
        task=task_payload(locks=["shiki:state"], ledger_ids=[LEDGER_ID, goal_ledger]),
        ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID), goal_ledger: ledger_payload(goal_ledger, task_id="T-9999")},
    ),
    f"Ledger {goal_ledger} is not scoped to task {TASK_ID} or goal {GOAL_ID}",
)

expect_ready(make_fixture("current-lock-change", locks=["shiki:state"], changed=[f".shiki/locks/{TASK_ID}.json"]))
expect_block(
    make_fixture("unrelated-lock-change", locks=["shiki:state"], changed=[".shiki/locks/T-9999.json"]),
    "PR changes unrelated Shiki lock file .shiki/locks/T-9999.json",
)
expect_block(
    make_fixture("unrelated-lock-delete", locks=["shiki:state"], changed=[".shiki/locks/T-9999.json"], status_entries=["D\t.shiki/locks/T-9999.json"]),
    "PR changes unrelated Shiki lock file .shiki/locks/T-9999.json",
)

repair_id = "RP-0001"
repair_path = f".shiki/repairs/{repair_id}.json"
repair_payload = {
    "repair_id": repair_id,
    "goal_id": GOAL_ID,
    "task_id": TASK_ID,
    "pr": PR_NUMBER,
    "attempt": 1,
    "failing_checklist_items": ["fixture"],
    "failing_acceptance_criteria": ["fixture"],
    "minimal_required_changes": ["fixture"],
    "prohibited_changes": ["broaden scope"],
    "required_skill": "tdd",
    "verification_commands": ["bash scripts/test_shiki_adversarial_state.sh"],
    "evidence_required": ["fixture"],
    "stop_condition": "fixture passes",
}
target = make_fixture("repair-current", locks=[f"path:{repair_path}"], changed=[repair_path])
write_json(target / repair_path, repair_payload)
expect_ready(target)

target = make_fixture("repair-other-task", locks=[f"path:{repair_path}"], changed=[repair_path])
bad_repair = copy.deepcopy(repair_payload)
bad_repair["task_id"] = "T-9999"
write_json(target / repair_path, bad_repair)
expect_block(target, f"Repair packet {repair_id} is not scoped to task {TASK_ID}")

target = make_fixture("repair-other-pr", locks=[f"path:{repair_path}"], changed=[repair_path])
bad_repair = copy.deepcopy(repair_payload)
bad_repair["pr"] = 999
write_json(target / repair_path, bad_repair)
expect_block(target, f"Repair packet {repair_id} does not reference current PR #{PR_NUMBER}")

target = make_fixture("repair-id-mismatch", locks=[f"path:{repair_path}"], changed=[repair_path])
bad_repair = copy.deepcopy(repair_payload)
bad_repair["repair_id"] = "RP-9999"
write_json(target / repair_path, bad_repair)
expect_block(target, f"Repair filename {repair_path} does not match JSON id")


# Guards T-0036: MergeGate must evaluate live PR state and reject stale metadata.
expect_block(
    make_fixture("stale-pr-head", locks=["path:scripts/test_shiki_adversarial_state.sh"], changed=["scripts/test_shiki_adversarial_state.sh"], pr=pr_payload(head=OTHER_HEAD), cca=cca_payload(head=OTHER_HEAD)),
    "does not match expected checked-out HEAD",
    allow_missing_cca=False,
)
expect_block(
    make_fixture("task-expected-pr-mismatch", locks=["path:scripts/test_shiki_adversarial_state.sh"], changed=["scripts/test_shiki_adversarial_state.sh"], task=task_payload(locks=["path:scripts/test_shiki_adversarial_state.sh"], expected_pr=999), cca=cca_payload()),
    "Task expected_pr 999 does not match PR #123",
    allow_missing_cca=False,
)
expect_block(
    make_fixture("task-expected-branch-mismatch", locks=["path:scripts/test_shiki_adversarial_state.sh"], changed=["scripts/test_shiki_adversarial_state.sh"], task=task_payload(locks=["path:scripts/test_shiki_adversarial_state.sh"], expected_branch="shiki/wrong-branch"), cca=cca_payload()),
    "does not match PR head",
    allow_missing_cca=False,
)
expect_block(
    make_fixture(
        "required-check-stale-head",
        locks=["path:scripts/test_shiki_adversarial_state.sh"],
        changed=["scripts/test_shiki_adversarial_state.sh"],
        pr=pr_payload(
            checks=[
                {"name": "Validate Shiki mirror", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": OTHER_HEAD},
                {"name": "MergeGate metadata check", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": HEAD},
            ]
        ),
        cca=cca_payload(),
    ),
    "Required check Validate Shiki mirror head SHA",
    allow_missing_cca=False,
)
expect_block(
    make_fixture("cca-head-mismatch", locks=["path:scripts/test_shiki_adversarial_state.sh"], changed=["scripts/test_shiki_adversarial_state.sh"], cca=cca_payload(head=OTHER_HEAD)),
    "CCA head_sha does not match the current PR headRefOid",
    allow_missing_cca=False,
)
expect_block(
    make_fixture("cca-pr-mismatch", locks=["path:scripts/test_shiki_adversarial_state.sh"], changed=["scripts/test_shiki_adversarial_state.sh"], cca=cca_payload(number=999)),
    "CCA pr 999 does not match PR #123",
    allow_missing_cca=False,
)


# Guards T-0036: workflow runtime evidence hygiene remains wired into CI.
cca_workflow = (root / ".github" / "workflows" / "shiki-cca-completion.yml").read_text(encoding="utf-8")
mergegate_workflow = (root / ".github" / "workflows" / "shiki-mergegate.yml").read_text(encoding="utf-8")
assert "rm -rf .shiki/gha" in cca_workflow
assert "Refresh live PR state" in cca_workflow
assert "live-pr.json" in cca_workflow
assert "live-changed-files.txt" in cca_workflow
assert "live-changed-files-status.txt" in cca_workflow
assert "--expected-head-sha" in cca_workflow
assert "--base-shiki .shiki/gha/base-shiki/.shiki" in cca_workflow
assert "author,headRefName,baseRefName,headRefOid,labels,files,reviews,reviewDecision,statusCheckRollup" in mergegate_workflow

print("adversarial state/evidence/lock regression suite passed")
PY
