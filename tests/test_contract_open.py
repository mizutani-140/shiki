"""End-to-end tests for `shiki contract open` and the MergeGate contract mode.

These drive the REAL CLI and the REAL mergegate_check.py via subprocess against a
temporary GitHub-first target. Each test CONSTRUCTS ITS OWN spec-frozen,
multi-task plan fixture inside that temporary directory (see ``_write_frozen_plan``)
— no test copies from or writes to ``.shiki/plans/`` or any other real mirror
path, so the suite adds no approval record to real mirror state (ADR 0015: an
approved contract belongs on a real branch behind a Guardian, never seeded by a
test).

They cover the task's acceptance checks: the command produces a branch carrying
only .shiki goal/task/DAG registration; a contract-mode PR with implementation or
a frozen-definition mismatch is blocked; a high-risk contract PR is blocked
without Guardian approval and passes with it; the CLI never applies the
contract-mode label; and the multi-task frozen plan registers into a branch
MergeGate accepts.
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

REPO_ROOT = shiki_test_support.REPO_ROOT
SCRIPTS = REPO_ROOT / "scripts"
HEAD_SHA = "1" * 40
REGISTRATION_DIRS = (".shiki/goals/", ".shiki/tasks/", ".shiki/dag/", ".shiki/ledger/")
CONTROL_DIRS = (
    "goals", "tasks", "dag", "ledger", "plans", "gha", "schemas", "migrations",
    "locks", "worktrees", "repairs", "reports", "runs", "inbox", "handoffs",
    "runner", "smoke", "starts", "memories",
)

# A synthetic plan id. The plan is written INTO the temporary target only; it is
# never read from or written to the real .shiki mirror.
FROZEN_PLAN_ID = "P-20260727T005125000000Z-0015c0de"

# Frozen plan task definitions. B depends on A (by title). Each carries the full
# governance-field set the CLI-registered task must match exactly — the fields
# MergeGate binds a registered task against its frozen plan definition (scope,
# non_goals, required_skills, risk_level, locks, acceptance_checks, and
# runtime -> assigned_runtime). The plan is HIGH risk so the Contract PR exercises
# the Guardian gate, and multi-task with a declared dependency so DAG coverage and
# edge binding are exercised.
_FROZEN_TASKS = [
    {
        "title": "Pre-dispatch contract gate",
        "scope": "scope-a",
        "non_goals": ["ng-a"],
        "required_skills": ["tdd", "code-review"],
        "risk_level": "high",
        "locks": ["path:scripts/a.py"],
        "acceptance_checks": ["a"],
        "runtime": "claude-code",
    },
    {
        "title": "Contract immutability",
        "scope": "scope-b",
        "non_goals": ["ng-b"],
        "required_skills": ["tdd", "code-review"],
        "risk_level": "high",
        "locks": ["path:scripts/b.py"],
        "acceptance_checks": ["b"],
        "runtime": "claude-code",
        "dependencies": ["Pre-dispatch contract gate"],
    },
]


def _git(target: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)


def _shiki_files(target: Path) -> set[str]:
    root = target / ".shiki"
    return {str(p.relative_to(target)) for p in root.rglob("*") if p.is_file()}


def _write_frozen_plan(target: Path, plan_id: str = FROZEN_PLAN_ID) -> None:
    """Write a spec-frozen, high-risk, multi-task plan into the TEMP target.

    This is the required fixture shape (a spec-frozen plan a Contract PR
    registers), constructed inside a temporary directory so no test writes to
    .shiki/plans/ or any other real mirror path. The plan carries no approved_by:
    the Guardian approval a Contract PR needs is recorded on the PR, never in the
    plan fixture."""
    (target / ".shiki" / "plans" / f"{plan_id}.json").write_text(
        json.dumps(
            {
                "id": plan_id,
                "title": "Contract approval before dispatch",
                "outcome": "A spec-frozen Goal's task contracts are approved before implementation runs.",
                "risk_level": "high",
                "non_goals": ["Implement the registered tasks in this Contract PR"],
                "required_skills": ["grill-with-docs"],
                "grill_with_docs": {"status": "complete"},
                "spec_freeze": {"status": "frozen"},
                "tasks": _FROZEN_TASKS,
            }
        ),
        encoding="utf-8",
    )


def _setup_target(tmp: Path) -> Path:
    """A GitHub-first target seeded with the real support files + a temp-local plan.

    Control dirs are pre-created with .gitkeep so `ensure_control_dirs` is a no-op
    and the only files `contract open` adds are the registration files — matching
    a real target where .shiki is already committed. Support files (manifest,
    guardian policy, config, workflows) are copied FROM the repo into the temp
    target (read-only), but the spec-frozen plan is CONSTRUCTED in the temp target
    (``_write_frozen_plan``), never read from the real mirror."""
    target = tmp
    _git(target, "init")
    _git(target, "remote", "add", "origin", "https://github.com/example/target.git")
    for name in CONTROL_DIRS:
        d = target / ".shiki" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".gitkeep").write_text("", encoding="utf-8")
    (target / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    # Real support files: manifest, guardian policy, config, workflow job names.
    shutil.copy2(REPO_ROOT / ".shiki" / "manifest.json", target / ".shiki" / "manifest.json")
    shutil.copy2(REPO_ROOT / ".shiki" / "guardian-policy.json", target / ".shiki" / "guardian-policy.json")
    shutil.copy2(REPO_ROOT / ".shiki" / "config.yaml", target / ".shiki" / "config.yaml")
    for wf in (REPO_ROOT / ".github" / "workflows").glob("*.y*ml"):
        shutil.copy2(wf, target / ".github" / "workflows" / wf.name)
    # The spec-frozen plan the Contract PR registers is built here, in the temp
    # target — never copied from or written to the real .shiki mirror.
    _write_frozen_plan(target)
    return target


def _run_cli_contract_open(target: Path, plan: str = FROZEN_PLAN_ID) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "shiki.py"), "contract", "open", "--target", str(target), "--plan", plan],
        capture_output=True, text=True,
    )


def _write_guardian_evidence(target: Path, *, approved: bool) -> None:
    gha = target / ".shiki" / "gha"
    gha.mkdir(parents=True, exist_ok=True)
    (gha / "live-guardian-timeline.json").write_text("[]", encoding="utf-8")
    if not approved:
        # Missing comments/events evidence => the high-risk gate blocks.
        for name in ("live-guardian-comments.json", "live-guardian-events.json"):
            path = gha / name
            if path.exists():
                path.unlink()
        return
    (gha / "live-guardian-events.json").write_text(
        json.dumps([{"event": "labeled", "actor": {"login": "mizutani-140"},
                     "label": {"name": "guardian:approved"}}]),
        encoding="utf-8",
    )
    (gha / "live-guardian-comments.json").write_text(
        json.dumps([{"author": {"login": "mizutani-140"},
                     "body": f"Guardian approval granted for head {HEAD_SHA}"}]),
        encoding="utf-8",
    )


def _write_contract_pr(target: Path, goal_id: str, *, approved_label: bool, contract_label: bool = True) -> None:
    labels = []
    if contract_label:
        labels.append({"name": "mergegate:contract"})
    if approved_label:
        labels.append({"name": "guardian:approved"})
    body = (
        f"Contract PR registering task contracts for Goal {goal_id}.\n\n"
        "<!-- shiki:contract -->\n\n"
        "## Scope\nRegister the frozen Goal/task/DAG contracts.\n"
        "## Acceptance\nMergeGate contract mode accepts the registration.\n"
        "## Evidence\nGuardian approval on this Contract PR.\n"
        "## MergeGate\nContract mode; Guardian forced from the Goal risk.\n"
    )
    pr = {
        "number": 900,
        "body": body,
        "author": {"login": "mizutani-140"},
        "headRefName": "shiki/contract-adr-0015",
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
    (gha / "changed-files.txt").write_text("".join(f"{p}\n" for _, p in entries), encoding="utf-8")
    (gha / "changed-files-status.txt").write_text("".join(f"{s}\t{p}\n" for s, p in entries), encoding="utf-8")


def _run_mergegate(target: Path) -> dict:
    result_file = target / ".shiki" / "gha" / "mergegate-result.json"
    gha = target / ".shiki" / "gha"
    subprocess.run(
        [
            sys.executable, str(SCRIPTS / "mergegate_check.py"),
            "--target", str(target),
            "--allow-missing-cca",
            "--pr-json", str(gha / "pr.json"),
            "--changed-files", str(gha / "changed-files.txt"),
            "--changed-files-status", str(gha / "changed-files-status.txt"),
            "--guardian-policy", str(target / ".shiki" / "guardian-policy.json"),
            "--guardian-comments", str(gha / "live-guardian-comments.json"),
            "--guardian-events", str(gha / "live-guardian-events.json"),
            "--guardian-timeline", str(gha / "live-guardian-timeline.json"),
            "--result-file", str(result_file),
        ],
        capture_output=True, text=True,
    )
    return json.loads(result_file.read_text(encoding="utf-8"))


def _registration_entries(target: Path, new_files: set[str]) -> list[tuple[str, str]]:
    return [("A", p) for p in sorted(new_files) if p.startswith(REGISTRATION_DIRS)]


class ContractOpenCliTests(unittest.TestCase):
    def test_branch_contains_only_shiki_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            before = _shiki_files(target)
            proc = _run_cli_contract_open(target)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            new_files = _shiki_files(target) - before
            self.assertTrue(new_files, "contract open wrote no registration files")
            for path in new_files:
                self.assertTrue(
                    path.startswith(REGISTRATION_DIRS),
                    f"contract open wrote a non-registration file: {path}",
                )
            # Exactly the goal, its DAG, N tasks, and their ledgers — no implementation.
            self.assertTrue(any(p.startswith(".shiki/goals/") for p in new_files))
            self.assertTrue(any(p.startswith(".shiki/dag/") for p in new_files))
            self.assertGreaterEqual(sum(p.startswith(".shiki/tasks/") for p in new_files), 2)

    def test_cli_does_not_apply_label_and_reports_maintainer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            proc = _run_cli_contract_open(target)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertFalse(report["label_applied_by_cli"])
            self.assertEqual(report["contract_label"], "mergegate:contract")
            self.assertIn("maintainer must apply", report["maintainer_action_required"].lower())

    def test_second_open_for_same_plan_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            self.assertEqual(_run_cli_contract_open(target).returncode, 0)
            proc = _run_cli_contract_open(target)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("already registered", proc.stderr)


class ContractMergeGateSimulationTests(unittest.TestCase):
    def _open(self, target: Path) -> str:
        before = _shiki_files(target)
        proc = _run_cli_contract_open(target)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self._new_files = _shiki_files(target) - before
        return report["goal_id"]

    def test_frozen_plan_registers_and_mergegate_accepts(self) -> None:
        # Acceptance check 6: a multi-task goal's contracts register into a branch
        # that a local MergeGate simulation accepts (with Guardian approval, since
        # the frozen plan is high risk).
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            goal_id = self._open(target)
            self.assertGreaterEqual(len([p for p in self._new_files if p.startswith(".shiki/tasks/")]), 2)
            _write_changed_files(target, _registration_entries(target, self._new_files))
            _write_contract_pr(target, goal_id, approved_label=True)
            _write_guardian_evidence(target, approved=True)
            result = _run_mergegate(target)
            self.assertEqual(result["mergegate"], "ready", result["blocking_reasons"])

    def test_high_risk_contract_blocked_without_guardian(self) -> None:
        # Acceptance check 4 (first half): high-risk contract PR blocked without
        # Guardian approval.
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            goal_id = self._open(target)
            _write_changed_files(target, _registration_entries(target, self._new_files))
            _write_contract_pr(target, goal_id, approved_label=False)
            _write_guardian_evidence(target, approved=False)
            result = _run_mergegate(target)
            self.assertEqual(result["mergegate"], "blocked")
            self.assertTrue(any("Guardian" in b for b in result["blocking_reasons"]), result["blocking_reasons"])

    def test_implementation_file_blocked(self) -> None:
        # Acceptance check 2: a contract-mode PR with an implementation file blocks.
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            goal_id = self._open(target)
            entries = _registration_entries(target, self._new_files) + [("M", "scripts/shiki_loop.py")]
            _write_changed_files(target, entries)
            _write_contract_pr(target, goal_id, approved_label=True)
            _write_guardian_evidence(target, approved=True)
            result = _run_mergegate(target)
            self.assertEqual(result["mergegate"], "blocked")
            self.assertTrue(any("non-Shiki" in b for b in result["blocking_reasons"]), result["blocking_reasons"])

    def test_frozen_definition_mismatch_blocked(self) -> None:
        # Acceptance check 3: a registered task diverging from its frozen plan
        # definition on a governance field is blocked.
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            goal_id = self._open(target)
            task_files = [p for p in self._new_files if p.startswith(".shiki/tasks/")]
            tampered = target / task_files[0]
            data = json.loads(tampered.read_text(encoding="utf-8"))
            data["risk_level"] = "low"  # frozen plan says high
            tampered.write_text(json.dumps(data), encoding="utf-8")
            _write_changed_files(target, _registration_entries(target, self._new_files))
            _write_contract_pr(target, goal_id, approved_label=True)
            _write_guardian_evidence(target, approved=True)
            result = _run_mergegate(target)
            self.assertEqual(result["mergegate"], "blocked")
            self.assertTrue(
                any("risk_level" in b and "frozen plan definition" in b for b in result["blocking_reasons"]),
                result["blocking_reasons"],
            )

    def test_contract_marker_without_label_fails_closed(self) -> None:
        # Acceptance check 5 (validator side): the marker alone (no maintainer
        # label) must not grant contract mode.
        with tempfile.TemporaryDirectory() as tmp:
            target = _setup_target(Path(tmp))
            goal_id = self._open(target)
            _write_changed_files(target, _registration_entries(target, self._new_files))
            _write_contract_pr(target, goal_id, approved_label=True, contract_label=False)
            _write_guardian_evidence(target, approved=True)
            result = _run_mergegate(target)
            self.assertEqual(result["mergegate"], "blocked")
            self.assertTrue(
                any("mergegate:contract" in b and "label" in b for b in result["blocking_reasons"]),
                result["blocking_reasons"],
            )


if __name__ == "__main__":
    unittest.main()
