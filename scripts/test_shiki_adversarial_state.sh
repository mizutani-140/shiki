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
    (target / ".shiki").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / ".shiki" / "manifest.json", target / ".shiki" / "manifest.json")
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


def run_mergegate(target: pathlib.Path, *, allow_missing_cca: bool = True, expected_head: str = HEAD, merged_prs: str = "") -> subprocess.CompletedProcess:
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
        "--merged-prs",
        merged_prs,
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
# ADR 0015 Contract immutability on the NORMAL task-PR path (T-20260801…9f3f6f63):
# a PR may move only its own task file's bookkeeping (the mutable set); its frozen
# governance contract is bound to the base snapshot, and its risk_level may never
# resolve weaker than base. The base snapshot registers the task with the same
# governance contract, so a mutable-only change passes.
_immut_base = {"tasks": {TASK_ID: task_payload(locks=["shiki:state"], status="planned")}}
# A mutable-only change (planned -> review) with an identical governance base: ready.
expect_ready(make_fixture("current-task-change", locks=["shiki:state"],
                          changed=[f".shiki/tasks/{TASK_ID}.json"], base=_immut_base))
# A governance field (scope) diverging from the base contract is blocked.
expect_block(
    make_fixture("current-task-governance-frozen", locks=["shiki:state"],
                 changed=[f".shiki/tasks/{TASK_ID}.json"],
                 task={**task_payload(locks=["shiki:state"]), "scope": "a smuggled scope rewrite"},
                 base=_immut_base),
    "frozen governance field 'scope'",
)
# A changed own task file with NO base snapshot to check it against fails closed.
expect_block(
    make_fixture("current-task-no-base", locks=["shiki:state"], changed=[f".shiki/tasks/{TASK_ID}.json"]),
    "no base .shiki snapshot is available to verify",
)
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
        status_entries=[f"A\t.shiki/ledger/{new_ledger}.json"],
        task=task_payload(locks=["shiki:state"], ledger_ids=[LEDGER_ID, new_ledger]),
        ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID), new_ledger: ledger_payload(new_ledger)},
    )
)
expect_block(
    make_fixture(
        "new-unlisted-ledger",
        locks=["shiki:state"],
        changed=[f".shiki/ledger/{new_ledger}.json"],
        status_entries=[f"A\t.shiki/ledger/{new_ledger}.json"],
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
        status_entries=[f"A\t.shiki/ledger/{mismatch}.json"],
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
        status_entries=[f"A\t.shiki/ledger/{wrong_task_ledger}.json"],
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
        status_entries=[f"A\t.shiki/ledger/{goal_ledger}.json"],
        task=task_payload(locks=["shiki:state"], ledger_ids=[LEDGER_ID, goal_ledger]),
        ledger_entries={LEDGER_ID: ledger_payload(LEDGER_ID), goal_ledger: ledger_payload(goal_ledger, task_id=None)},
    )
)
expect_block(
    make_fixture(
        "goal-ledger-conflicting-task",
        locks=["shiki:state"],
        changed=[f".shiki/ledger/{goal_ledger}.json"],
        status_entries=[f"A\t.shiki/ledger/{goal_ledger}.json"],
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
assert "live-guardian-comments.json" in cca_workflow
assert "live-guardian-events.json" in cca_workflow
assert "--expected-head-sha" in cca_workflow
assert "--base-shiki .shiki/gha/base-shiki/.shiki" in cca_workflow
assert "--guardian-comments .shiki/gha/live-guardian-comments.json" in cca_workflow
assert "--guardian-events .shiki/gha/live-guardian-events.json" in cca_workflow
assert "author,headRefName,baseRefName,headRefOid,labels,files,reviews,reviewDecision,statusCheckRollup" in mergegate_workflow


# Guards T-20260729T092307427377Z-c8531f7e: the CCA evidence manifest schema now
# accepts an empty verdict.task_id (contract-mode / task-less PRs), so the
# identity binding in validate_cca_evidence_manifest — not a blanket schema
# floor — is the only thing standing between a task-ful PR and a laundered empty
# task_id. Assert it holds: the same empty-task_id manifest is accepted when the
# PR resolves to no task and rejected the moment it resolves to a real one.
import shiki_evidence

launder_dir = tmp_root / "cca-manifest-launder" / ".shiki" / "gha"
if launder_dir.exists():
    shutil.rmtree(launder_dir)
launder_dir.mkdir(parents=True)
write_json(launder_dir / "cca-verdict.json", cca_payload(task_id=""))
write_json(launder_dir / "pr.json", pr_payload())
(launder_dir / "changed-files.txt").write_text("scripts/test_shiki_adversarial_state.sh\n", encoding="utf-8")
(launder_dir / "changed-files-status.txt").write_text("M\tscripts/test_shiki_adversarial_state.sh\n", encoding="utf-8")
launder_manifest = shiki_evidence.build_cca_evidence_manifest(
    repository="OWNER/REPO",
    pr=PR_NUMBER,
    head_sha=HEAD,
    workflow_name="Shiki CCA Completion",
    run_id="1",
    run_attempt="1",
    event_name="pull_request",
    artifact_name="shiki-cca-evidence",
    evidence_dir=launder_dir,
)
launder_manifest["created_at"] = "2026-07-29T00:00:00Z"
assert launder_manifest["verdict"]["task_id"] == "", launder_manifest["verdict"]


def validate_launder(expected_task_id):
    return shiki_evidence.validate_cca_evidence_manifest(
        manifest=launder_manifest,
        evidence_dir=launder_dir,
        expected_repo="OWNER/REPO",
        expected_pr=PR_NUMBER,
        expected_head_sha=HEAD,
        expected_task_id=expected_task_id,
        expected_goal_id=GOAL_ID,
    )


taskful_errors = validate_launder(TASK_ID)
assert any("task_id" in error for error in taskful_errors), (
    f"task-ful PR must reject a laundered empty task_id, got {taskful_errors!r}"
)
taskless_errors = validate_launder(None)
assert not taskless_errors, f"task-less manifest must validate, got {taskless_errors!r}"


# ============================================================================
# ADR 0017: bookkeeping-closeout Guardian exemption smuggling cases.
#
# A closeout PR carries only a merged task's terminal state (task done, lock
# released, and — when it completes the goal — goal complete + exactly one
# scorecard) with zero implementation, so a PROVEN bookkeeping closeout is
# evaluated as if the task were low risk at the single Guardian decision point.
# Each smuggling case below is a CRITICAL-risk closeout with ONE defect that must
# disqualify it, so the task re-inherits its risk and the Guardian requirement
# fires. We assert the SPECIFIC Guardian fallback block (this suite passes no
# guardian policy, so a required high/critical PR blocks on the missing policy),
# never merely that the PR blocks, so an unrelated future block cannot mask a
# classifier regression. The clean control proves the classifier CAN exempt.
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
GUARDIAN_POLICY_FALLBACK = "Guardian policy is required for high/critical risk PRs"

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


def close_pr(**over) -> dict:
    pr = {
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
        "labels": [],
        "reviews": [{"state": "APPROVED", "author": {"login": "github-actions"}}],
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {"name": "Validate Shiki mirror", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": CLOSE_HEAD},
            {"name": "MergeGate metadata check", "status": "COMPLETED", "conclusion": "SUCCESS", "headSha": CLOSE_HEAD},
        ],
    }
    pr.update(over)
    return pr


def close_cca(**over) -> dict:
    cca = {
        "verdict": "complete", "summary": "closeout complete",
        "goal_id": CLOSE_GOAL, "task_id": CLOSE_TASK, "pr": CLOSE_PR, "head_sha": CLOSE_HEAD,
        "can_merge": True,
        "checklist": [{"id": "CCA-01", "status": "pass", "blocking": True, "evidence": "closeout"}],
        "acceptance": [{"criterion": "terminal state", "status": "pass", "evidence": ["closeout"]}],
        "mergegate": {"closeout": "pass"}, "confidence": 1.0, "repair_packet": None,
    }
    cca.update(over)
    return cca


def build_closeout(
    name: str,
    *,
    completes: bool = True,
    head_mutate=None,
    base_mutate=None,
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

    base_task = {"id": CLOSE_TASK, "goal_id": CLOSE_GOAL, **CLOSE_GOVERNANCE,
                 "status": "review", "expected_pr": CLOSE_IMPL_PR, "ledger_evidence": ["L-close-base"]}
    if base_mutate:
        base_mutate(base_task)
    write_json(B / "tasks" / f"{CLOSE_TASK}.json", base_task)
    write_json(B / "locks" / f"{CLOSE_TASK}.json",
               {"task_id": CLOSE_TASK, "goal_id": CLOSE_GOAL, "state": base_lock_state, "owner": "shiki-run"})
    write_json(B / "goals" / f"{CLOSE_GOAL}.json",
               {"id": CLOSE_GOAL, "status": "in-progress", "title": "g", "risk_level": "critical"})

    head_ledgers = [CLOSE_COMP, CLOSE_PULL] if completes else [CLOSE_PULL]
    head_task = {"id": CLOSE_TASK, "goal_id": CLOSE_GOAL, **CLOSE_GOVERNANCE,
                 "status": "done", "expected_pr": CLOSE_PR, "closeout_pr": CLOSE_PR,
                 "ledger_evidence": head_ledgers}
    if head_mutate:
        head_mutate(head_task)
    write_json(S / "tasks" / f"{CLOSE_TASK}.json", head_task)
    write_json(S / "locks" / f"{CLOSE_TASK}.json",
               {"task_id": CLOSE_TASK, "goal_id": CLOSE_GOAL, "state": lock_head_state, "owner": "shiki-run"})
    write_json(S / "goals" / f"{CLOSE_GOAL}.json",
               {"id": CLOSE_GOAL, "status": goal_head_status, "title": "g", "risk_level": "critical"})
    write_json(S / "ledger" / f"{CLOSE_PULL}.json",
               {"id": CLOSE_PULL, "goal_id": CLOSE_GOAL, "task_id": CLOSE_TASK, "type": "lock",
                "summary": f"closeout /pull PR #{CLOSE_PR}", "evidence": [f".shiki/tasks/{CLOSE_TASK}.json"], "links": [CLOSE_URL]})

    if completes:
        write_json(S / "dag" / f"{CLOSE_GOAL}.json", {"goal_id": CLOSE_GOAL, "nodes": [CLOSE_TASK], "edges": []})
        write_json(S / "ledger" / f"{CLOSE_COMP}.json",
                   {"id": CLOSE_COMP, "goal_id": CLOSE_GOAL, "task_id": None, "type": "completion",
                    "summary": "goal complete", "evidence": comp_evidence or [CLOSE_REPORT_REL]})
        write_json(S / CLOSE_REPORT_REL, {"id": CLOSE_REPORT_ID, "goal_id": CLOSE_GOAL, "status": "complete"})
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
    files = [path for _, path in changed]
    (S / "gha" / "changed-files.txt").write_text("\n".join(files) + "\n", encoding="utf-8")
    (S / "gha" / "changed-files-status.txt").write_text("\n".join(f"{s}\t{p}" for s, p in changed) + "\n", encoding="utf-8")
    return target


def expect_guardian_fallback(target: pathlib.Path, *, merged: str = str(CLOSE_IMPL_PR)) -> None:
    result = run_mergegate(target, allow_missing_cca=False, expected_head=CLOSE_HEAD, merged_prs=merged)
    if result.returncode == 0:
        raise AssertionError(f"expected the Guardian fallback to block, got success:\n{result.stdout}")
    assert_contains(result.stdout, GUARDIAN_POLICY_FALLBACK)


def expect_exempted(target: pathlib.Path, *, merged: str = str(CLOSE_IMPL_PR)) -> None:
    result = run_mergegate(target, allow_missing_cca=False, expected_head=CLOSE_HEAD, merged_prs=merged)
    if GUARDIAN_POLICY_FALLBACK in result.stdout:
        raise AssertionError(f"bookkeeping exemption did not fire; Guardian block present:\n{result.stdout}")


# Clean control: a proven completing closeout of a CRITICAL task is exempted, so
# the Guardian requirement does NOT fire (this suite passes no guardian policy, so
# a non-exempted critical PR would block on the missing policy — it does not).
expect_exempted(build_closeout("close-clean-completing"))
# A proven non-completing closeout (a sibling still runs) is also exempted.
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
# T-20260729T065622763684Z-ee01f3c4: MergeGate scope enforcement sees the WHOLE
# diff. Renames are judged through both legs, the eleven generic mirror
# directories block foreign/deleted records, the runtime-evidence match is exact
# (so the CCA schema is mergeable), and no PR may author a spec_freeze block.
# ============================================================================

# (a) A rename that moves a file OUT of locked scope is seen through its source
# (delete) leg, identically to the explicit delete+add form.
expect_block(
    make_fixture(
        "mg-rename-outside-locks",
        locks=["path:docs/**"],
        changed=["docs/moved.py"],
        status_entries=["R100\ttests/moved.py\tdocs/moved.py"],
    ),
    "Changed file tests/moved.py is outside declared task locks",
)
expect_block(
    make_fixture(
        "mg-delete-add-outside-locks",
        locks=["path:docs/**"],
        changed=["docs/moved.py", "tests/moved.py"],
        status_entries=["A\tdocs/moved.py", "D\ttests/moved.py"],
    ),
    "Changed file tests/moved.py is outside declared task locks",
)
# A rename entirely inside the declared locks is clean (no false positive).
expect_ready(
    make_fixture(
        "mg-rename-inside-locks",
        locks=["path:docs/**"],
        changed=["docs/b.py"],
        status_entries=["R100\tdocs/a.py\tdocs/b.py"],
    )
)

# (b) The generic mirror-class rule. A broad lock (path:.shiki/**) satisfies
# files-outside-locks, so ONLY the mirror-class rule can reject these.
# Deleting a foreign goal's DAG blocks (base snapshot absent here — the head-diff
# rule alone must catch it).
expect_block(
    make_fixture(
        "mg-delete-foreign-dag",
        locks=["path:.shiki/**"],
        changed=[".shiki/dag/G-9999.json"],
        status_entries=["D\t.shiki/dag/G-9999.json"],
    ),
    "PR must not delete Shiki dag record .shiki/dag/G-9999.json",
)
# And with a base snapshot present too.
expect_block(
    make_fixture(
        "mg-delete-foreign-dag-based",
        locks=["path:.shiki/**"],
        changed=[".shiki/dag/G-9999.json"],
        status_entries=["D\t.shiki/dag/G-9999.json"],
        base={"dag": {"G-9999": {"goal_id": "G-9999", "nodes": [], "edges": []}}},
    ),
    "PR must not delete Shiki dag record .shiki/dag/G-9999.json",
)
# Modifying a foreign report/worktree/runner record blocks (not own-scoped).
mg_foreign_report = make_fixture(
    "mg-foreign-report-modify",
    locks=["path:.shiki/**"],
    changed=[".shiki/reports/R-foreign.json"],
    status_entries=["M\t.shiki/reports/R-foreign.json"],
)
write_json(mg_foreign_report / ".shiki" / "reports" / "R-foreign.json", {"id": "R-foreign", "goal_id": "G-9999"})
expect_block(mg_foreign_report, f"not scoped to task {TASK_ID} or goal {GOAL_ID}")
expect_block(
    make_fixture(
        "mg-foreign-worktree-delete",
        locks=["path:.shiki/**"],
        changed=[".shiki/worktrees/T-9999.json"],
        status_entries=["D\t.shiki/worktrees/T-9999.json"],
    ),
    "PR must not delete Shiki worktrees record .shiki/worktrees/T-9999.json",
)
# The base-snapshot loop now delete-protects every mirror directory: a base
# runner record absent at head blocks even with no diff entry naming it.
expect_block(
    make_fixture(
        "mg-base-runner-delete",
        locks=["path:scripts/test_shiki_adversarial_state.sh"],
        changed=["scripts/test_shiki_adversarial_state.sh"],
        base={"runner": {"EXEC-BASE": {"id": "EXEC-BASE", "goal_id": GOAL_ID, "task_id": TASK_ID}}},
    ),
    "PR must not delete base Shiki runner record .shiki/runner/EXEC-BASE.json",
)
# The PR's own goal/task-scoped mirror records still pass.
mg_own_worktree = make_fixture(
    "mg-own-worktree-change",
    locks=["path:scripts/test_shiki_adversarial_state.sh"],
    changed=[f".shiki/worktrees/{TASK_ID}.json"],
    status_entries=[f"M\t.shiki/worktrees/{TASK_ID}.json"],
)
write_json(mg_own_worktree / ".shiki" / "worktrees" / f"{TASK_ID}.json", {"id": TASK_ID, "task_id": TASK_ID, "goal_id": GOAL_ID})
expect_ready(mg_own_worktree)

# (c) The CCA verdict SCHEMA is a real repository contract, not forged runtime
# evidence — it must be mergeable under a schemas lock.
expect_ready(
    make_fixture(
        "mg-cca-schema-mergeable",
        locks=["path:.shiki/schemas/**"],
        changed=[".shiki/schemas/cca-verdict.schema.json"],
        status_entries=["M\t.shiki/schemas/cca-verdict.schema.json"],
    )
)

# Spec Freeze is operator-only (ADR 0009). A new plan that is not the goal's
# source_plan is blocked; authoring or amending a spec_freeze block on the goal's
# OWN source plan is blocked; touching the source plan without altering
# spec_freeze is unaffected.
SOURCE_PLAN = "P-20260731T010000000000Z-50c3ce0f"


def with_source_plan(target: pathlib.Path) -> None:
    goal = read_json(target / ".shiki" / "goals" / f"{GOAL_ID}.json")
    goal["source_plan"] = SOURCE_PLAN
    write_json(target / ".shiki" / "goals" / f"{GOAL_ID}.json", goal)


mg_foreign_plan = make_fixture(
    "mg-foreign-plan-add",
    locks=["path:.shiki/**"],
    changed=[".shiki/plans/P-OTHER.json"],
    status_entries=["A\t.shiki/plans/P-OTHER.json"],
)
with_source_plan(mg_foreign_plan)
write_json(mg_foreign_plan / ".shiki" / "plans" / "P-OTHER.json", {"id": "P-OTHER"})
expect_block(mg_foreign_plan, f"is not goal {GOAL_ID}'s source_plan")

mg_self_freeze = make_fixture(
    "mg-spec-freeze-self-grant",
    locks=["path:.shiki/**"],
    changed=[f".shiki/plans/{SOURCE_PLAN}.json"],
    status_entries=[f"M\t.shiki/plans/{SOURCE_PLAN}.json"],
    base={"plans": {SOURCE_PLAN: {"id": SOURCE_PLAN, "title": "p"}}},
)
with_source_plan(mg_self_freeze)
write_json(
    mg_self_freeze / ".shiki" / "plans" / f"{SOURCE_PLAN}.json",
    {"id": SOURCE_PLAN, "title": "p", "spec_freeze": {"status": "frozen", "approved_by": "not-the-operator"}},
)
expect_block(mg_self_freeze, f"must not create or modify the spec_freeze block of .shiki/plans/{SOURCE_PLAN}.json")

mg_amend_freeze = make_fixture(
    "mg-spec-freeze-amend",
    locks=["path:.shiki/**"],
    changed=[f".shiki/plans/{SOURCE_PLAN}.json"],
    status_entries=[f"M\t.shiki/plans/{SOURCE_PLAN}.json"],
    base={"plans": {SOURCE_PLAN: {"id": SOURCE_PLAN, "spec_freeze": {"status": "frozen", "amendments": []}}}},
)
with_source_plan(mg_amend_freeze)
write_json(
    mg_amend_freeze / ".shiki" / "plans" / f"{SOURCE_PLAN}.json",
    {"id": SOURCE_PLAN, "spec_freeze": {"status": "frozen", "amendments": ["smuggled amendment"]}},
)
expect_block(mg_amend_freeze, f"must not create or modify the spec_freeze block of .shiki/plans/{SOURCE_PLAN}.json")

_FROZEN = {"status": "frozen", "approved_by": "operator"}
mg_plan_touch = make_fixture(
    "mg-plan-touch-no-freeze-change",
    locks=[f"path:.shiki/plans/{SOURCE_PLAN}.json"],
    changed=[f".shiki/plans/{SOURCE_PLAN}.json"],
    status_entries=[f"M\t.shiki/plans/{SOURCE_PLAN}.json"],
    base={"plans": {SOURCE_PLAN: {"id": SOURCE_PLAN, "title": "p", "spec_freeze": _FROZEN, "notes": "old"}}},
)
with_source_plan(mg_plan_touch)
write_json(
    mg_plan_touch / ".shiki" / "plans" / f"{SOURCE_PLAN}.json",
    {"id": SOURCE_PLAN, "title": "p", "spec_freeze": _FROZEN, "notes": "new"},
)
expect_ready(mg_plan_touch)


# ============================================================================
# Amendment mode (ADR 0009/0015) is two-factor (marker + mergegate:amendment
# label), exactly like contract/goal_reconcile. NEITHER factor alone relaxes the
# normal-path prohibitions: the operator-only spec_freeze gate behaves EXACTLY as
# before unless BOTH factors are present (a maintainer applies the label, not
# untrusted PR text). Prove marker-alone fails closed AND the spec_freeze
# prohibition still fires, and label-alone does not even enter the mode.
# ============================================================================
AMEND_MARKER = "<!-- shiki:amendment -->"
_SPEC_FREEZE_BLOCK = f"must not create or modify the spec_freeze block of .shiki/plans/{SOURCE_PLAN}.json"


def _amend_smuggle_fixture(name, *, body_marker, labels):
    fx = make_fixture(
        name,
        locks=["path:.shiki/**"],
        changed=[f".shiki/plans/{SOURCE_PLAN}.json"],
        status_entries=[f"M\t.shiki/plans/{SOURCE_PLAN}.json"],
        base={"plans": {SOURCE_PLAN: {"id": SOURCE_PLAN, "spec_freeze": {"status": "frozen", "amendments": []}}}},
    )
    with_source_plan(fx)
    # A normal implementation PR trying to append an amendment to its OWN source plan.
    write_json(
        fx / ".shiki" / "plans" / f"{SOURCE_PLAN}.json",
        {"id": SOURCE_PLAN, "spec_freeze": {"status": "frozen", "amendments": ["smuggled amendment"]}},
    )
    pr = pr_payload()
    if body_marker:
        pr["body"] = pr["body"] + "\n" + AMEND_MARKER
    pr["labels"] = labels
    write_json(fx / ".shiki" / "gha" / "pr.json", pr)
    return fx


# Marker WITHOUT the maintainer label: fails closed on the missing label AND the
# normal operator-only spec_freeze prohibition still fires (normal path unchanged).
_marker_res = run_mergegate(
    _amend_smuggle_fixture("mg-amend-marker-no-label", body_marker=True, labels=[{"name": "guardian:approved"}])
)
assert _marker_res.returncode != 0, _marker_res.stdout
assert_contains(_marker_res.stdout, "amendment mode requires the mergegate:amendment label")
assert_contains(_marker_res.stdout, _SPEC_FREEZE_BLOCK)

# Label WITHOUT the marker: does NOT enter amendment mode (no label-required
# error), and the normal spec_freeze prohibition is likewise untouched.
_label_res = run_mergegate(
    _amend_smuggle_fixture(
        "mg-amend-label-no-marker", body_marker=False,
        labels=[{"name": "guardian:approved"}, {"name": "mergegate:amendment"}],
    )
)
assert _label_res.returncode != 0, _label_res.stdout
assert "amendment mode requires the mergegate:amendment label" not in _label_res.stdout, _label_res.stdout
assert_contains(_label_res.stdout, _SPEC_FREEZE_BLOCK)


# ============================================================================
# T-20260801T003238841526Z-9f3f6f63: the Guardian requirement SURVIVES a
# self-lowered risk_level on the NORMAL task-PR path. The gate resolves the task's
# risk never-weaker-than-base, so a PR that drops its OWN risk_level critical ->
# low still forces the Guardian gate. Assert the guardian OUTCOME (the
# missing-policy fallback — this suite writes no guardian policy), not merely that
# the PR blocks, so a future change that blocks for an unrelated reason cannot mask
# a regression.
# ============================================================================
_risk_base = {"tasks": {TASK_ID: task_payload(locks=["shiki:state"], status="planned")}}  # risk_level critical
_downgraded_head = {**task_payload(locks=["shiki:state"]), "risk_level": "low"}
_downgrade_fx = make_fixture(
    "current-task-risk-downgrade-guardian-survives",
    locks=["shiki:state"], changed=[f".shiki/tasks/{TASK_ID}.json"],
    task=_downgraded_head, base=_risk_base, cca=cca_payload(),
)
_downgrade_res = run_mergegate(_downgrade_fx, allow_missing_cca=False)
assert _downgrade_res.returncode != 0, _downgrade_res.stdout
# The Guardian requirement resolved at the base (critical) risk, not the head's low.
assert_contains(_downgrade_res.stdout, GUARDIAN_POLICY_FALLBACK)
# And the immutability gate independently blocked the self-lowered risk_level.
assert_contains(_downgrade_res.stdout, "risk_level")
# Control: the head's OWN low risk does not force the gate. A low-risk head that
# MATCHES a low-risk base neither blocks on immutability nor forces a Guardian, so
# the fallback is ABSENT — proving the never-weaker resolution (not the head risk)
# is what keeps the Guardian requirement alive above.
_low_base = {"tasks": {TASK_ID: {**task_payload(locks=["shiki:state"], status="planned"), "risk_level": "low"}}}
_matched_fx = make_fixture(
    "current-task-low-matched-no-guardian",
    locks=["shiki:state"], changed=[f".shiki/tasks/{TASK_ID}.json"],
    task=_downgraded_head, base=_low_base, cca=cca_payload(),
)
_matched_res = run_mergegate(_matched_fx, allow_missing_cca=False)
if GUARDIAN_POLICY_FALLBACK in _matched_res.stdout:
    raise AssertionError("control: a low-risk head matching a low-risk base must not force a Guardian:\n" + _matched_res.stdout)


# ============================================================================
# T-20260801T085710529159Z-575d4080 (DEFECT A): no repair packet the loop can
# emit ever names the CCA verdict CHECK as its only failing item. Adversarially
# enumerate EVERY verdict value the loop's resolver can feed the engine for a
# sole-CCA-red PR whose rerun budget is spent, and prove the engine never routes
# such a verdict to a repair that names the CCA verdict check: needs_guardian /
# blocked / complete / unresolvable STOP (no packet at all), and repair_required /
# insufficient_evidence strip the CCA check from the repair items. Then drive the
# REAL _dispatch_repair effector to confirm the emitted packet never carries
# "required check failed: CCA verdict".
# ============================================================================
import contextlib
import io

import shiki_loop

_CCA_CHECK = shiki_loop.CCA_VERDICT_CHECK
_VERDICT_REQUIRED = ["Validate Shiki mirror", _CCA_CHECK, "MergeGate metadata check", "MergeGate policy check"]
_SOLE_CCA_RED = {name: "pass" for name in _VERDICT_REQUIRED}
_SOLE_CCA_RED[_CCA_CHECK] = "fail"
_VERDICT_TASK = {
    "id": "T-0099", "goal_id": "G-0099", "status": "review", "risk_level": "high",
    "expected_pr": 99, "assigned_runtime": "claude-code",
}


def _verdict_decision(cca_verdict):
    # Sole CCA red, rerun budget spent (cca_reruns == MAX_CCA_RERUNS) so the
    # decision reaches the verdict gate instead of another same-head rerun.
    return shiki_loop.decide_task_action(
        _VERDICT_TASK,
        checks=_SOLE_CCA_RED,
        pr_state={"merged": False, "head_sha": "deadbeef"},
        repair_attempts=0,
        repair_limit=3,
        required_checks=_VERDICT_REQUIRED,
        cca_reruns=shiki_loop.MAX_CCA_RERUNS,
        cca_verdict=cca_verdict,
    )


# Every value the resolver can return (the five verdicts + the unresolvable
# sentinel): the loop must never route ANY of them to a repair naming the CCA check.
for _verdict in sorted(shiki_loop.CCA_VERDICT_VALUES) + [shiki_loop.CCA_VERDICT_UNRESOLVED]:
    _decision = _verdict_decision(_verdict)
    if _decision["action"] == "dispatch_repair":
        assert _CCA_CHECK not in _decision.get("failed_checks", []), (_verdict, _decision)
    else:
        # A non-repair terminal decision emits no repair packet at all.
        assert _decision["action"] in {"stop_guardian", "stop_blocked"}, (_verdict, _decision)

# The two repairable verdicts must still dispatch a bounded repair (never silently
# swallow a real failure), with the CCA verdict check stripped from the items.
for _verdict in ("repair_required", "insufficient_evidence"):
    _decision = _verdict_decision(_verdict)
    assert _decision["action"] == "dispatch_repair", (_verdict, _decision)
    assert _decision["failed_checks"] == [], (_verdict, _decision)


def _mk_verdict_target(name):
    tgt = tmp_root / name
    tgt.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-b", "main", str(tgt)],
        ["git", "-C", str(tgt), "config", "user.email", "t@t"],
        ["git", "-C", str(tgt), "config", "user.name", "t"],
        ["git", "-C", str(tgt), "remote", "add", "origin", "https://github.com/o/r.git"],
    ):
        subprocess.run(cmd, check=True, capture_output=True)
    write_json(tgt / ".shiki" / "goals" / "G-0099.json",
               {"id": "G-0099", "status": "planned", "risk_level": "high", "ledger_evidence": []})
    write_json(tgt / ".shiki" / "tasks" / "T-0099.json",
               {"id": "T-0099", "goal_id": "G-0099", "status": "review", "title": "t", "scope": "s",
                "risk_level": "high", "assigned_runtime": "claude-code", "expected_branch": "b",
                "expected_pr": 99, "locks": ["path:.shiki/**"], "required_skills": ["tdd"],
                "acceptance_checks": ["a"], "ledger_evidence": []})
    return tgt


# Drive the REAL _dispatch_repair effector for the stripped decisions the engine
# produces (failed_checks == [], cca_verdict repairable) and confirm the EMITTED
# packet never names the CCA verdict check — it names the verdict's findings instead.
_orig_dispatch = shiki_loop._dispatch
shiki_loop._dispatch = lambda target, task, repair_id=None: 0
try:
    for _index, _verdict in enumerate(("repair_required", "insufficient_evidence")):
        _tgt = _mk_verdict_target(f"verdict-packet-{_index}")
        with contextlib.redirect_stdout(io.StringIO()):
            _info = shiki_loop._dispatch_repair(
                _tgt, shiki_loop.load_task(_tgt, "T-0099"), [], 1, cca_verdict=_verdict
            )
        _packet = read_json(_tgt / ".shiki" / "repairs" / f"{_info['repair_id']}.json")
        _items = _packet["failing_checklist_items"]
        assert _items != [f"required check failed: {_CCA_CHECK}"], (_verdict, _items)
        assert all(item != f"required check failed: {_CCA_CHECK}" for item in _items), (_verdict, _items)
        assert any(_verdict in item for item in _items), (_verdict, _items)
finally:
    shiki_loop._dispatch = _orig_dispatch


print("adversarial state/evidence/lock regression suite passed")
PY
