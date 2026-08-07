"""A Contract PR may carry its Goal's own spec-frozen source plan (SADR-0015 gap).

SADR-0015 has a Contract PR carry "only Goal, task, and DAG registration" and
assumes the Goal's spec-frozen source plan already sits on the default branch.
Nothing puts it there: `shiki contract open` never writes a plan, and MergeGate
contract mode blocked both halves of the only path that could — "source_plan not
found" when the plan is absent from the judged tree, and "must not change" when
the PR carries it. Every new Goal was therefore permanently unregisterable.

These tests exercise the narrow escape from that deadlock: contract mode now
permits ADDING the Goal's own source_plan when its content is a spec-frozen plan,
while still rejecting a MODIFIED plan, a plan that is not the Goal's source_plan,
an unfrozen carried plan, and any plan carried by a goal_reconcile PR (which
validates against the frozen plan as immutable base authority). Guardian approval
forced from the frozen-plan risk is what authorizes the carried freeze; the
content check only proves the file is a well-formed frozen source plan.

Every fixture is constructed inside a temporary directory. No test reads from or
writes to the real `.shiki/plans/` (or any other real mirror path): a spec-frozen
plan is an operator approval record and belongs on a real branch behind a
Guardian, never seeded into or judged against real mirror state.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from mergegate_check import (
    ChangedFile,
    enforce_contract,
    enforce_goal_reconcile,
)

REPO_ROOT = shiki_test_support.REPO_ROOT
SCRIPTS = REPO_ROOT / "scripts"

# Synthetic ids, distinct from the real mirror and from other test suites. The
# plan id is never a real `.shiki/plans/` id.
GOAL = "G-20260728T000000000000Z-0000a1b2"
PLAN = "P-20260728T000000000000Z-0000cafe"
OTHER_PLAN = "P-20260728T000000000000Z-0000d00d"
T_A = "T-20260728T000000000000Z-0000a001"
T_B = "T-20260728T000000000000Z-0000b002"
GOAL_LEDGER = "L-20260728T000000000000Z-0000g003"
LEDGER = "L-20260728T000000000000Z-0000l004"
TITLE_A = "Carry the source plan"
TITLE_B = "Register the contract"
PLAN_FILE = f".shiki/plans/{PLAN}.json"

FROZEN_A = {
    "title": TITLE_A,
    "scope": "scope-a",
    "non_goals": ["ng-a"],
    "required_skills": ["tdd", "code-review"],
    "risk_level": "high",
    "locks": ["path:scripts/a.py"],
    "acceptance_checks": ["a"],
    "runtime": "claude-code",
}
FROZEN_B = {
    "title": TITLE_B,
    "scope": "scope-b",
    "non_goals": ["ng-b"],
    "required_skills": ["tdd", "code-review"],
    "risk_level": "high",
    "locks": ["path:scripts/b.py"],
    "acceptance_checks": ["b"],
    "runtime": "claude-code",
    "dependencies": [TITLE_A],
}
_TITLE_TO_ID = {TITLE_A: T_A, TITLE_B: T_B}


def _plan_payload(plan_id: str = PLAN, *, frozen: bool = True) -> dict:
    return {
        "id": plan_id,
        "title": "Contract approval before dispatch",
        "risk_level": "high",
        "spec_freeze": {"status": "frozen" if frozen else "draft"},
        "tasks": [FROZEN_A, FROZEN_B],
    }


def _seed_plan(root: Path, *, plan_id: str = PLAN, frozen: bool = True) -> None:
    for d in ("goals", "plans", "tasks", "dag", "ledger"):
        (root / ".shiki" / d).mkdir(parents=True, exist_ok=True)
    (root / ".shiki" / "plans" / f"{plan_id}.json").write_text(
        json.dumps(_plan_payload(plan_id, frozen=frozen)), encoding="utf-8"
    )


def _write_goal(root: Path, *, source_plan: str = PLAN, risk: str = "high", status: str = "planned") -> None:
    (root / ".shiki" / "goals" / f"{GOAL}.json").write_text(
        json.dumps({"id": GOAL, "status": status, "source_plan": source_plan, "risk_level": risk, "title": "g"}),
        encoding="utf-8",
    )


def _write_task(root: Path, tid: str, *, title: str, status: str = "planned") -> None:
    frozen = {TITLE_A: FROZEN_A, TITLE_B: FROZEN_B}[title]
    deps = [_TITLE_TO_ID[d] for d in frozen.get("dependencies", [])]
    task = {
        "id": tid,
        "goal_id": GOAL,
        "title": title,
        "status": status,
        "scope": frozen["scope"],
        "non_goals": frozen["non_goals"],
        "required_skills": frozen["required_skills"],
        "risk_level": frozen["risk_level"],
        "locks": frozen["locks"],
        "acceptance_checks": frozen["acceptance_checks"],
        "assigned_runtime": frozen["runtime"],
        "dependencies": deps,
    }
    (root / ".shiki" / "tasks" / f"{tid}.json").write_text(json.dumps(task), encoding="utf-8")


def _write_ledger(root: Path, lid: str, *, ltype: str) -> None:
    (root / ".shiki" / "ledger" / f"{lid}.json").write_text(
        json.dumps(
            {
                "id": lid,
                "goal_id": GOAL,
                "task_id": None,
                "type": ltype,
                "actor": "shiki-cli",
                "timestamp": "2026-07-28T00:00:00+00:00",
                "summary": "contract registration",
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )


def _dag(root: Path, *, nodes, edges) -> None:
    (root / ".shiki" / "dag" / f"{GOAL}.json").write_text(
        json.dumps({"goal_id": GOAL, "nodes": nodes, "edges": edges}), encoding="utf-8"
    )


def _seed_full(root: Path, *, frozen: bool = True) -> None:
    """A valid full contract registration on disk (HEAD): plan, goal, both tasks,
    the DAG and the two registration ledgers."""
    _seed_plan(root, frozen=frozen)
    _write_goal(root)
    _write_task(root, T_A, title=TITLE_A)
    _write_task(root, T_B, title=TITLE_B)
    _write_ledger(root, GOAL_LEDGER, ltype="goal-created")
    _write_ledger(root, LEDGER, ltype="task-registered")
    _dag(root, nodes=[T_A, T_B], edges=[{"from": T_A, "to": T_B, "reason": "declared plan dependency"}])


def _registration_changes() -> list[ChangedFile]:
    return [
        ChangedFile("A", f".shiki/goals/{GOAL}.json"),
        ChangedFile("A", f".shiki/tasks/{T_A}.json"),
        ChangedFile("A", f".shiki/tasks/{T_B}.json"),
        ChangedFile("A", f".shiki/dag/{GOAL}.json"),
        ChangedFile("A", f".shiki/ledger/{GOAL_LEDGER}.json"),
        ChangedFile("A", f".shiki/ledger/{LEDGER}.json"),
    ]


def _run_contract(root: Path, changes: list[ChangedFile], *, allow_source_plan_carry: bool = True) -> list[str]:
    # The plan-carry relaxation is an explicit opt-in; the contract flow (main)
    # enables it, so these direct-validator tests do too. See
    # ``mergegate_check._validate_contract_source_plan``.
    blocking: list[str] = []
    enforce_contract(
        target=root,
        goal_id=GOAL,
        changed_files_status=changes,
        blocking=blocking,
        warnings=[],
        allow_source_plan_carry=allow_source_plan_carry,
    )
    return blocking


class ContractSourcePlanRegistrationTests(unittest.TestCase):
    def test_adds_spec_frozen_source_plan_is_accepted(self) -> None:
        # Acceptance: a contract-mode PR that ADDS an unmodified, spec-frozen
        # source plan for its goal (alongside the goal/task/DAG registration) is
        # accepted. This is exactly the shape the deadlock made impossible.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            blocking = _run_contract(root, _registration_changes() + [ChangedFile("A", PLAN_FILE)])
        self.assertEqual(blocking, [])

    def test_carry_denied_without_opt_in(self) -> None:
        # The plan-carry relaxation is never automatic: without the explicit
        # opt-in, even contract mode rejects the plan ADD (deny-by-default), so the
        # frozen plan can never be introduced into a registration by accident.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            blocking = _run_contract(
                root,
                _registration_changes() + [ChangedFile("A", PLAN_FILE)],
                allow_source_plan_carry=False,
            )
        self.assertTrue(any("must not change" in b and PLAN_FILE in b for b in blocking), blocking)

    def test_deadlock_first_half_gone_when_plan_carried(self) -> None:
        # The absent-plan half of the deadlock ("source_plan not found") is
        # resolved by carrying the plan: with the plan present in the judged tree
        # (as an ADD would put it), `_frozen_plan_tasks` resolves and the carry is
        # admitted, so neither half of the contradiction fires.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            blocking = _run_contract(root, _registration_changes() + [ChangedFile("A", PLAN_FILE)])
        self.assertFalse(any("source_plan" in b and "not found" in b for b in blocking), blocking)
        self.assertFalse(any("must not change" in b and PLAN_FILE in b for b in blocking), blocking)

    def test_modified_plan_is_blocked(self) -> None:
        # A MODIFY of a plan file would author or alter a spec_freeze block on an
        # already-present plan; only ADDING the source plan whole is allowed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            blocking = _run_contract(root, _registration_changes() + [ChangedFile("M", PLAN_FILE)])
        self.assertTrue(any("only ADD" in b and PLAN_FILE in b for b in blocking), blocking)

    def test_deleted_plan_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            blocking = _run_contract(root, _registration_changes() + [ChangedFile("D", PLAN_FILE)])
        self.assertTrue(any("must not delete" in b and PLAN_FILE in b for b in blocking), blocking)

    def test_plan_that_is_not_source_plan_is_blocked(self) -> None:
        # A contract PR carrying a plan whose id is not the Goal's source_plan is
        # blocked, even when it is itself a spec-frozen plan — the relaxation is
        # bound to the Goal's own frozen authority, not to any frozen plan.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            (root / ".shiki" / "plans" / f"{OTHER_PLAN}.json").write_text(
                json.dumps(_plan_payload(OTHER_PLAN)), encoding="utf-8"
            )
            blocking = _run_contract(
                root, _registration_changes() + [ChangedFile("A", f".shiki/plans/{OTHER_PLAN}.json")]
            )
        self.assertTrue(
            any("source_plan" in b and OTHER_PLAN in b for b in blocking), blocking
        )

    def test_carried_plan_that_is_not_spec_frozen_is_blocked(self) -> None:
        # Acceptance: a plan carried without a frozen spec_freeze block is blocked.
        # (`_frozen_plan_tasks` also rejects an unfrozen source plan; this asserts
        # the carry validator itself refuses the unfrozen file, so the relaxation
        # can never admit a draft plan even if the frozen-plan gate were reordered.)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root, frozen=False)
            blocking = _run_contract(root, _registration_changes() + [ChangedFile("A", PLAN_FILE)])
        self.assertTrue(
            any("not spec-frozen" in b for b in blocking) or any("spec-frozen" in b and PLAN_FILE in b for b in blocking),
            blocking,
        )

    def test_carried_plan_filename_must_match_id(self) -> None:
        # A carried plan whose filename does not match its own id is blocked, so a
        # PR cannot register content under the source_plan name that identifies
        # itself as a different plan.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            (root / ".shiki" / "plans" / f"{PLAN}.json").write_text(
                json.dumps(_plan_payload(OTHER_PLAN)), encoding="utf-8"  # id != filename
            )
            blocking = _run_contract(root, _registration_changes() + [ChangedFile("A", PLAN_FILE)])
        self.assertTrue(any("does not match its id" in b and PLAN_FILE in b for b in blocking), blocking)

    def test_goal_reconcile_must_not_carry_a_plan(self) -> None:
        # goal_reconcile validates registered tasks against the frozen plan as the
        # immutable base authority; it must never carry (introduce) that plan. The
        # relaxation is contract-mode only.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_full(root)
            blocking: list[str] = []
            enforce_goal_reconcile(
                target=root,
                goal_id=GOAL,
                changed_files_status=[ChangedFile("A", PLAN_FILE)],
                blocking=blocking,
                warnings=[],
            )
        self.assertTrue(
            any("must not change" in b and PLAN_FILE in b for b in blocking), blocking
        )


# --- End-to-end: the metadata-job scenario (change a + change b together) -------

HEAD_SHA = "2" * 40
CONTROL_DIRS = (
    "goals", "tasks", "dag", "ledger", "plans", "gha", "schemas", "migrations",
    "locks", "worktrees", "repairs", "reports", "runs", "inbox", "handoffs",
    "runner", "smoke", "starts", "memories",
)


def _git(target: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)


def _setup_target(tmp: Path) -> Path:
    """A GitHub-first target with the real support files + a temp-local plan.

    Support files (manifest, guardian policy, config, workflow job names) are
    copied read-only from the repo; the spec-frozen plan is CONSTRUCTED here, in
    the temp target, never read from or written to the real mirror."""
    target = tmp
    _git(target, "init")
    _git(target, "remote", "add", "origin", "https://github.com/example/target.git")
    for name in CONTROL_DIRS:
        d = target / ".shiki" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gitkeep").write_text("", encoding="utf-8")
    (target / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / ".shiki" / "manifest.json", target / ".shiki" / "manifest.json")
    shutil.copy2(REPO_ROOT / ".shiki" / "guardian-policy.json", target / ".shiki" / "guardian-policy.json")
    shutil.copy2(REPO_ROOT / ".shiki" / "config.yaml", target / ".shiki" / "config.yaml")
    for wf in (REPO_ROOT / ".github" / "workflows").glob("*.y*ml"):
        shutil.copy2(wf, target / ".github" / "workflows" / wf.name)
    _seed_full(target)
    return target


def _write_contract_pr(target: Path, *, approved_label: bool) -> None:
    labels = [{"name": "mergegate:contract"}]
    if approved_label:
        labels.append({"name": "guardian:approved"})
    body = (
        f"Contract PR registering task contracts for Goal {GOAL}.\n\n"
        "<!-- shiki:contract -->\n\n"
        "## Scope\nRegister the frozen Goal/task/DAG contracts and carry the source plan.\n"
        "## Acceptance\nMergeGate contract mode accepts the registration.\n"
        "## Evidence\nGuardian approval on this Contract PR.\n"
        "## MergeGate\nContract mode; Guardian forced from the Goal risk.\n"
    )
    pr = {
        "number": 901,
        "body": body,
        "author": {"login": "mizutani-140"},
        "headRefName": "shiki/contract-plan-carry",
        "headRefOid": HEAD_SHA,
        "labels": labels,
        "reviewDecision": "APPROVED",
        "reviews": [],
        "reviewThreads": [],
        "statusCheckRollup": [],
    }
    (target / ".shiki" / "gha" / "pr.json").write_text(json.dumps(pr), encoding="utf-8")


def _write_changed_files(target: Path, entries: list[tuple[str, str]]) -> None:
    gha = target / ".shiki" / "gha"
    gha.mkdir(parents=True, exist_ok=True)
    (gha / "changed-files.txt").write_text("".join(f"{p}\n" for _, p in entries), encoding="utf-8")
    (gha / "changed-files-status.txt").write_text("".join(f"{s}\t{p}\n" for s, p in entries), encoding="utf-8")


def _write_guardian_evidence(target: Path, *, approved: bool) -> None:
    gha = target / ".shiki" / "gha"
    gha.mkdir(parents=True, exist_ok=True)
    # The metadata job (change b) always writes these files, empty when there is
    # no approval — so a high-risk contract PR blocks on the APPROVAL decision,
    # never on a missing input file.
    (gha / "live-guardian-timeline.json").write_text("[]", encoding="utf-8")
    if approved:
        (gha / "live-guardian-events.json").write_text(
            json.dumps([{"event": "labeled", "actor": {"login": "mizutani-140"},
                         "label": {"name": "guardian:approved"}}]),
            encoding="utf-8",
        )
        (gha / "live-guardian-comments.json").write_text(
            json.dumps([{"author": {"login": "mizutani-140"},
                         "body": f"Guardian approval granted\n\n{HEAD_SHA}"}]),
            encoding="utf-8",
        )
    else:
        (gha / "live-guardian-events.json").write_text("[]", encoding="utf-8")
        (gha / "live-guardian-comments.json").write_text("[]", encoding="utf-8")


def _run_metadata_mergegate(target: Path) -> dict:
    """Invoke mergegate_check.py with exactly the metadata job's argv (change b):
    --allow-missing-cca plus the guardian-policy and three guardian evidence
    flags, no base snapshot (that stays with R-06)."""
    gha = target / ".shiki" / "gha"
    result_file = gha / "mergegate-result.json"
    subprocess.run(
        [
            sys.executable, str(SCRIPTS / "mergegate_check.py"),
            "--target", str(target),
            "--allow-missing-cca",
            "--pr-json", str(gha / "pr.json"),
            "--changed-files", str(gha / "changed-files.txt"),
            "--changed-files-status", str(gha / "changed-files-status.txt"),
            "--expected-head-sha", HEAD_SHA,
            "--guardian-policy", str(target / ".shiki" / "guardian-policy.json"),
            "--guardian-comments", str(gha / "live-guardian-comments.json"),
            "--guardian-events", str(gha / "live-guardian-events.json"),
            "--guardian-timeline", str(gha / "live-guardian-timeline.json"),
            "--result-file", str(result_file),
        ],
        capture_output=True, text=True,
    )
    return json.loads(result_file.read_text(encoding="utf-8"))


class ContractPlanEndToEndTests(unittest.TestCase):
    def test_high_risk_contract_carrying_source_plan_with_guardian_is_ready(self) -> None:
        # Acceptance (change a + b): a high-risk contract-mode PR carrying its
        # spec-frozen source plan, with live Guardian approval evidence present,
        # reaches "ready" through the metadata job argv.
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            _write_changed_files(target, [(e.status, e.path) for e in _registration_changes()] + [("A", PLAN_FILE)])
            _write_contract_pr(target, approved_label=True)
            _write_guardian_evidence(target, approved=True)
            result = _run_metadata_mergegate(target)
        self.assertEqual(result["mergegate"], "ready", result["blocking_reasons"])

    def test_high_risk_contract_blocks_on_approval_not_missing_evidence(self) -> None:
        # Acceptance (change b): with the evidence files PRESENT but empty (as the
        # metadata job now always writes them), a high-risk contract PR blocks on
        # the missing APPROVAL, never on a missing evidence-file INPUT.
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            _write_changed_files(target, [(e.status, e.path) for e in _registration_changes()] + [("A", PLAN_FILE)])
            _write_contract_pr(target, approved_label=False)
            _write_guardian_evidence(target, approved=False)
            result = _run_metadata_mergegate(target)
        self.assertEqual(result["mergegate"], "blocked")
        self.assertFalse(
            any("evidence file is missing" in b for b in result["blocking_reasons"]),
            result["blocking_reasons"],
        )
        self.assertTrue(
            any("Guardian" in b for b in result["blocking_reasons"]),
            result["blocking_reasons"],
        )


if __name__ == "__main__":
    unittest.main()
