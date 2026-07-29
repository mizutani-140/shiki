"""Id-scoped mirror lock coverage, DERIVED at MergeGate judgment time.

A loop-executed task is dispatched into an isolated worktree where the goal loop
syncs the task's own .shiki evidence onto its branch (see
shiki_loop._evidence_relatives_for_task): its task file, worktree record, lock
record, its goal file and DAG, the ledger entries it owns, AND every
.shiki-prefixed path those ledgers' ``evidence`` arrays reference (transitively —
a ``type:check`` TDD-gate ledger points at ``.shiki/runner/EXEC-*.json``; a
completion ledger points at ``.shiki/reports/R-*.json``). MergeGate's
files-outside-locks gate then judges those .shiki mutations against the task's
locks.

The design here does NOT write mirror coverage into the stored locks. Injecting
at registration is stale by the time MergeGate reads it: the loop appends ledgers
and writes runner/EXEC records AFTER dispatch, none of which re-inject. So a
registered task's locks stay EXACTLY what the plan declared, and
``mergegate_check._derive_task_mirror_locks`` recomputes the task's id-scoped
mirror set at judgment time and unions it into the effective lock set passed to
``files_outside_locks``.

The derivation is BOUNDED so a PR-authored task file cannot widen its own
effective locks: it expands only the ledgers listed in the task's own
``ledger_evidence`` whose ``goal_id`` AND ``task_id`` match the task, and ignores
any other reference. Because nothing is injected, frozen-plan matching compares
locks exactly, with no injected-set tolerance.

This suite pins that model:
  * registration / lock-acquire / worktree-allocate inject nothing;
  * the derived set covers exactly the task's own synced evidence (incl. the
    transitive EXEC/report refs), with no shared-namespace glob;
  * a foreign ledger does not expand the derived set;
  * MergeGate still blocks a mirror file of another task or goal;
  * frozen-plan lock matching is exact.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_loop
from shiki_locks import (
    files_outside_locks,
    is_glob_pattern,
    split_lock,
)
from shiki_tasks import (
    allocate_worktree_record,
    append_ledger,
    load_task,
    lock_record,
    next_control_id,
    orchestrate_plan,
    register_task_from_plan,
    try_acquire_locks,
)
from shiki_process import shiki_path, utc_now, write_json
from mergegate_check import _derive_task_mirror_locks, _frozen_task_match_errors

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_DIRS = (
    "goals",
    "tasks",
    "dag",
    "ledger",
    "locks",
    "worktrees",
    "plans",
    "runs",
    "reports",
    "runner",
)


def _make_target() -> Path:
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(tmp)], check=True)
    for name in CONTROL_DIRS:
        (tmp / ".shiki" / name).mkdir(parents=True, exist_ok=True)
    return tmp


def _plan(*, tasks: list[dict], plan_id: str = "P-TEST", risk: str = "low") -> dict:
    return {
        "id": plan_id,
        "title": "mirror lock derivation plan",
        "outcome": "tasks carry only their declared product locks",
        "risk_level": risk,
        "grill_with_docs": {"status": "complete"},
        "spec_freeze": {"status": "frozen"},
        "tasks": tasks,
    }


def _task(title: str, *, locks: list[str]) -> dict:
    return {
        "title": title,
        "scope": f"scope for {title}",
        "acceptance_checks": ["works"],
        "locks": list(locks),
        "runtime": "claude-code",
    }


def _is_shiki_path_lock(lock: str) -> bool:
    parsed = split_lock(lock)
    return bool(parsed and parsed[0] == "path" and parsed[1].startswith(".shiki/"))


def _shared_namespace_glob(lock: str) -> bool:
    """True when ``lock`` is a directory glob over a shared .shiki namespace.

    e.g. ``path:.shiki/**`` or ``path:.shiki/ledger/**`` — a lock that grabs a
    whole mirror subtree shared by other tasks, rather than one id-scoped file.
    """
    parsed = split_lock(lock)
    if not parsed or parsed[0] != "path":
        return False
    value = parsed[1]
    return value.startswith(".shiki/") and is_glob_pattern(value)


def _append_ledger_to_task(
    target: Path,
    task: dict,
    *,
    ledger_type: str,
    evidence: list[str],
    goal_id: str | None = None,
    task_id: str | None = None,
) -> str:
    """Append a ledger (with the given ids) and record it on the task file.

    Defaults the ledger's ``goal_id``/``task_id`` to the task's own ids — the
    legitimate case. Pass overrides to forge a foreign ledger.
    """
    ledger_id = append_ledger(
        target,
        goal_id=goal_id if goal_id is not None else task["goal_id"],
        task_id=task_id if task_id is not None else task["id"],
        ledger_type=ledger_type,
        summary=f"{ledger_type} ledger",
        evidence=evidence,
    )
    stored = load_task(target, task["id"])
    stored.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), stored)
    return ledger_id


def _write_exec_record(target: Path, task: dict) -> str:
    """Write a runner/EXEC record like shiki_loop's TDD gate; return its rel path."""
    exec_id = next_control_id(target, "EXEC")
    exec_file = shiki_path(target, "runner", f"{exec_id}.json")
    write_json(
        exec_file,
        {
            "id": exec_id,
            "task_id": task["id"],
            "goal_id": task["goal_id"],
            "command": "python3 -m unittest discover -s tests",
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "created_at": utc_now(),
        },
    )
    return str(exec_file.relative_to(target))


def _write_report(target: Path, task: dict) -> str:
    report_id = next_control_id(target, "R")
    report_file = shiki_path(target, "reports", f"{report_id}.json")
    write_json(report_file, {"id": report_id, "goal_id": task["goal_id"], "status": "complete"})
    return str(report_file.relative_to(target))


class RegistrationInjectsNothingTests(unittest.TestCase):
    """Acceptance 1: a registered task's locks equal its frozen plan's locks
    exactly — nothing is injected at registration, lock acquisition or worktree
    allocation."""

    def test_registered_task_locks_equal_declared(self) -> None:
        target = _make_target()
        declared = ["path:scripts/one.py", "path:scripts/two.py"]
        result = orchestrate_plan(target, _plan(tasks=[_task("Only task", locks=declared)]))
        task = load_task(target, result["task_ids"][0])
        self.assertEqual(task["locks"], declared)
        self.assertFalse(any(_is_shiki_path_lock(lock) for lock in task["locks"]))

    def test_locks_unchanged_through_full_dispatch(self) -> None:
        target = _make_target()
        declared = ["path:scripts/a.py"]
        result = orchestrate_plan(target, _plan(tasks=[_task("Task A", locks=declared)]))
        task_id = result["dispatchable_task_ids"][0]

        # The task file, its lock record, and its worktree record all carry the
        # declared product locks verbatim — no .shiki path was injected anywhere.
        task = load_task(target, task_id)
        self.assertEqual(task["locks"], declared)
        record = lock_record(target, task_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["locks"], declared)
        worktree = json.loads(
            (target / ".shiki" / "worktrees" / f"{task_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(worktree["locks"], declared)

    def test_reacquire_and_reallocate_do_not_inject(self) -> None:
        # Directly drive the lower-level write sites (not only orchestrate_plan).
        target = _make_target()
        goal_id = "G-noinject"
        (target / ".shiki" / "goals" / f"{goal_id}.json").write_text(
            json.dumps({"id": goal_id, "risk_level": "low"}), encoding="utf-8"
        )
        declared = ["path:scripts/x.py"]
        task_id, _ = register_task_from_plan(
            target,
            goal_id=goal_id,
            task_plan=_task("Solo", locks=declared),
            dependencies=[],
        )
        self.assertEqual(load_task(target, task_id)["locks"], declared)
        try_acquire_locks(target, task_id)
        self.assertEqual(load_task(target, task_id)["locks"], declared)
        allocate_worktree_record(target, task_id)
        self.assertEqual(load_task(target, task_id)["locks"], declared)


class MirrorDerivationTests(unittest.TestCase):
    """The read-time derivation covers exactly a task's own synced .shiki paths."""

    def _bare_task(self, target: Path, *, goal_id: str = "G-xyz", task_id: str = "T-abc") -> dict:
        return {"id": task_id, "goal_id": goal_id, "ledger_evidence": [], "locks": []}

    def test_derives_direct_id_scoped_paths(self) -> None:
        target = _make_target()
        derived = _derive_task_mirror_locks(target, self._bare_task(target))
        self.assertIn("path:.shiki/tasks/T-abc.json", derived)
        self.assertIn("path:.shiki/worktrees/T-abc.json", derived)
        self.assertIn("path:.shiki/locks/T-abc.json", derived)
        self.assertIn("path:.shiki/goals/G-xyz.json", derived)
        self.assertIn("path:.shiki/dag/G-xyz.json", derived)

    def test_no_shared_namespace_glob(self) -> None:
        target = _make_target()
        derived = _derive_task_mirror_locks(target, self._bare_task(target))
        for lock in derived:
            self.assertFalse(_shared_namespace_glob(lock), f"{lock} is a shared-namespace glob")

    def test_omits_goal_paths_without_goal_id(self) -> None:
        target = _make_target()
        derived = _derive_task_mirror_locks(target, {"id": "T-abc", "goal_id": "", "ledger_evidence": []})
        self.assertFalse(any("goals" in lock or "dag" in lock for lock in derived))

    def test_expands_matching_check_ledger_evidence(self) -> None:
        target = _make_target()
        task = self._registered(target)
        exec_rel = _write_exec_record(target, task)
        _append_ledger_to_task(target, task, ledger_type="check", evidence=[exec_rel])
        task = load_task(target, task["id"])
        derived = _derive_task_mirror_locks(target, task)
        self.assertIn(f"path:{exec_rel}", derived)
        for lock in derived:
            self.assertFalse(_shared_namespace_glob(lock), f"{lock} is a shared-namespace glob")

    def test_expands_matching_completion_ledger_evidence(self) -> None:
        target = _make_target()
        task = self._registered(target)
        report_rel = _write_report(target, task)
        _append_ledger_to_task(target, task, ledger_type="completion", evidence=[report_rel])
        task = load_task(target, task["id"])
        derived = _derive_task_mirror_locks(target, task)
        self.assertIn(f"path:{report_rel}", derived)

    def test_ledger_ref_escaping_shiki_is_rejected(self) -> None:
        target = _make_target()
        task = self._registered(target)
        # A matching ledger whose evidence tries to escape the .shiki subtree must
        # not widen coverage outside it.
        _append_ledger_to_task(target, task, ledger_type="check", evidence=[".shiki/../escape.py"])
        task = load_task(target, task["id"])
        derived = _derive_task_mirror_locks(target, task)
        self.assertNotIn("path:.shiki/../escape.py", derived)
        self.assertFalse(any("escape.py" in lock for lock in derived))

    def test_missing_or_malformed_ledger_contributes_nothing(self) -> None:
        target = _make_target()
        task = self._registered(target)
        # ledger_evidence references a ledger id with no file on disk.
        stored = load_task(target, task["id"])
        stored.setdefault("ledger_evidence", []).append("L-DOES-NOT-EXIST")
        write_json(shiki_path(target, "tasks", f"{task['id']}.json"), stored)
        task = load_task(target, task["id"])
        derived = _derive_task_mirror_locks(target, task)
        self.assertNotIn("path:.shiki/ledger/L-DOES-NOT-EXIST.json", derived)

    def _registered(self, target: Path) -> dict:
        result = orchestrate_plan(target, _plan(tasks=[_task("Only task", locks=["path:scripts/one.py"])]))
        return load_task(target, result["task_ids"][0])


class SyncedEvidenceCoverageTests(unittest.TestCase):
    """Acceptance 2: MergeGate accepts a PR carrying the task's own mirror
    evidence with narrow product-path locks only, including a post-execution
    runner/EXEC record reached transitively through a check ledger."""

    def _effective_locks(self, target: Path, task: dict) -> list[str]:
        return [*task["locks"], *_derive_task_mirror_locks(target, task)]

    def test_synced_evidence_inside_effective_locks(self) -> None:
        target = _make_target()
        result = orchestrate_plan(target, _plan(tasks=[_task("Only task", locks=["path:scripts/one.py"])]))
        task = load_task(target, result["task_ids"][0])
        synced = shiki_loop._evidence_relatives_for_task(target, task)
        effective = self._effective_locks(target, task)
        self.assertEqual(files_outside_locks(synced, effective), [])
        # The declared product lock alone does NOT cover the synced .shiki evidence
        # (proving the derivation is doing the work).
        self.assertNotEqual(files_outside_locks(synced, task["locks"]), [])
        self.assertIn(f".shiki/goals/{task['goal_id']}.json", synced)

    def test_covers_transitive_exec_record_after_test_run(self) -> None:
        target = _make_target()
        result = orchestrate_plan(target, _plan(tasks=[_task("Only task", locks=["path:scripts/one.py"])]))
        task = load_task(target, result["task_ids"][0])
        exec_rel = _write_exec_record(target, task)
        _append_ledger_to_task(target, task, ledger_type="check", evidence=[exec_rel])
        task = load_task(target, task["id"])

        synced = shiki_loop._evidence_relatives_for_task(target, task)
        # The loop genuinely syncs the EXEC record (transitive from the check ledger).
        self.assertIn(exec_rel, synced)
        effective = self._effective_locks(target, task)
        self.assertEqual(files_outside_locks(synced, effective), [])
        # And the product locks were never widened.
        self.assertEqual(task["locks"], ["path:scripts/one.py"])


class CrossTaskBlockingTests(unittest.TestCase):
    """Acceptance 3: MergeGate still blocks a mirror file belonging to another
    task or another goal."""

    def _effective_locks(self, target: Path, task: dict) -> list[str]:
        return [*task["locks"], *_derive_task_mirror_locks(target, task)]

    def test_blocks_other_task_and_other_goal_mirror_files(self) -> None:
        target = _make_target()
        result = orchestrate_plan(target, _plan(tasks=[_task("Only task", locks=["path:scripts/one.py"])]))
        task = load_task(target, result["task_ids"][0])
        effective = self._effective_locks(target, task)

        foreign = [
            ".shiki/tasks/T-OTHER.json",
            ".shiki/worktrees/T-OTHER.json",
            ".shiki/locks/T-OTHER.json",
            ".shiki/goals/G-OTHER.json",
            ".shiki/dag/G-OTHER.json",
        ]
        outside = files_outside_locks(foreign, effective)
        self.assertEqual(sorted(outside), sorted(foreign))


class BoundedDerivationSecurityTests(unittest.TestCase):
    """Acceptance 4: a task file listing a ledger that is not its own — foreign
    goal_id or task_id — does not expand the derived set, so a PR-authored task
    file cannot widen its own effective locks."""

    def _victim_ledger(self, target: Path, task: dict, *, goal_id: str, task_id: str, victim_rel: str) -> str:
        """A ledger owned by ANOTHER task/goal that references a victim .shiki path,
        forged onto this task's ledger_evidence by a malicious PR-authored task file."""
        return _append_ledger_to_task(
            target,
            task,
            ledger_type="check",
            evidence=[victim_rel],
            goal_id=goal_id,
            task_id=task_id,
        )

    def test_foreign_goal_ledger_does_not_widen(self) -> None:
        target = _make_target()
        result = orchestrate_plan(target, _plan(tasks=[_task("Only task", locks=["path:scripts/one.py"])]))
        task = load_task(target, result["task_ids"][0])
        victim_rel = ".shiki/runner/EXEC-VICTIM.json"
        self._victim_ledger(target, task, goal_id="G-FOREIGN", task_id=task["id"], victim_rel=victim_rel)
        task = load_task(target, task["id"])
        derived = _derive_task_mirror_locks(target, task)
        self.assertNotIn(f"path:{victim_rel}", derived)
        effective = [*task["locks"], *derived]
        self.assertEqual(files_outside_locks([victim_rel], effective), [victim_rel])

    def test_foreign_task_ledger_does_not_widen(self) -> None:
        target = _make_target()
        result = orchestrate_plan(target, _plan(tasks=[_task("Only task", locks=["path:scripts/one.py"])]))
        task = load_task(target, result["task_ids"][0])
        victim_rel = ".shiki/reports/R-VICTIM.json"
        self._victim_ledger(target, task, goal_id=task["goal_id"], task_id="T-FOREIGN", victim_rel=victim_rel)
        task = load_task(target, task["id"])
        derived = _derive_task_mirror_locks(target, task)
        self.assertNotIn(f"path:{victim_rel}", derived)
        # The foreign ledger's own path is not covered either.
        self.assertFalse(any("FOREIGN" in lock for lock in derived))

    def test_own_ledger_still_expands_alongside_foreign(self) -> None:
        # A legitimate matching ledger IS expanded even when a foreign one is also
        # (maliciously) listed: bounding drops only the foreign reference.
        target = _make_target()
        result = orchestrate_plan(target, _plan(tasks=[_task("Only task", locks=["path:scripts/one.py"])]))
        task = load_task(target, result["task_ids"][0])
        own_exec = _write_exec_record(target, task)
        _append_ledger_to_task(target, task, ledger_type="check", evidence=[own_exec])
        self._victim_ledger(
            target, task, goal_id="G-FOREIGN", task_id="T-FOREIGN", victim_rel=".shiki/runner/EXEC-VICTIM.json"
        )
        task = load_task(target, task["id"])
        derived = _derive_task_mirror_locks(target, task)
        self.assertIn(f"path:{own_exec}", derived)
        self.assertNotIn("path:.shiki/runner/EXEC-VICTIM.json", derived)


class FrozenLockMatchTests(unittest.TestCase):
    """Acceptance 5: frozen-plan matching compares locks exactly and rejects any
    difference, including path:.shiki/**."""

    def _registered(self) -> tuple[Path, dict, dict]:
        target = _make_target()
        goal_id = "G-frozen"
        (target / ".shiki" / "goals" / f"{goal_id}.json").write_text(
            json.dumps({"id": goal_id, "source_plan": "P-frozen", "risk_level": "low"}), encoding="utf-8"
        )
        frozen = {
            "title": "Frozen task",
            "scope": "scope for Frozen task",
            "non_goals": [],
            "required_skills": ["tdd", "code-review"],
            "risk_level": "low",
            "locks": ["path:scripts/one.py"],
            "acceptance_checks": ["works"],
            "runtime": "claude-code",
        }
        task_id, _ = register_task_from_plan(target, goal_id=goal_id, task_plan=dict(frozen), dependencies=[])
        return target, load_task(target, task_id), frozen

    def test_exact_match_passes(self) -> None:
        _, task, frozen = self._registered()
        self.assertEqual(task["locks"], frozen["locks"])
        self.assertEqual(_frozen_task_match_errors(task["id"], task, frozen), [])

    def test_blanket_shiki_glob_rejected(self) -> None:
        _, task, frozen = self._registered()
        task["locks"] = [*task["locks"], "path:.shiki/**"]
        errors = _frozen_task_match_errors(task["id"], task, frozen)
        self.assertTrue(any("lock" in e.lower() for e in errors), errors)

    def test_id_scoped_mirror_lock_rejected(self) -> None:
        # Even a well-formed id-scoped mirror lock is rejected: nothing is injected,
        # so the stored locks must equal the frozen plan's exactly.
        _, task, frozen = self._registered()
        task["locks"] = [*task["locks"], f"path:.shiki/tasks/{task['id']}.json"]
        errors = _frozen_task_match_errors(task["id"], task, frozen)
        self.assertTrue(any("lock" in e.lower() for e in errors), errors)

    def test_dropped_frozen_lock_rejected(self) -> None:
        _, task, frozen = self._registered()
        task["locks"] = [lock for lock in task["locks"] if lock != "path:scripts/one.py"]
        errors = _frozen_task_match_errors(task["id"], task, frozen)
        self.assertTrue(any("lock" in e.lower() for e in errors), errors)


class FrozenPlanCorpusTests(unittest.TestCase):
    """Every task of every frozen plan in the repo is judged clean by MergeGate:
    its declared locks unioned with the read-time derived set cover exactly the
    evidence the loop syncs for it."""

    def test_all_frozen_plan_tasks_cover_their_synced_evidence(self) -> None:
        plan_files = sorted((REPO_ROOT / ".shiki" / "plans").glob("*.json"))
        frozen_plans = []
        for path in plan_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("spec_freeze") or {}).get("status") == "frozen":
                frozen_plans.append(data)
        self.assertTrue(frozen_plans, "expected at least one frozen plan in the repo")

        checked = 0
        for plan in frozen_plans:
            target = _make_target()
            plan_copy = dict(plan)
            plan_copy.pop("id", None)
            result = orchestrate_plan(target, plan_copy)
            for task_id in result["task_ids"]:
                task = load_task(target, task_id)
                synced = shiki_loop._evidence_relatives_for_task(target, task)
                effective = [*task["locks"], *_derive_task_mirror_locks(target, task)]
                outside = files_outside_locks(synced, effective)
                self.assertEqual(outside, [], f"{task_id} in {plan.get('title')!r}: {outside}")
                checked += 1
        self.assertGreaterEqual(checked, 19)


if __name__ == "__main__":
    unittest.main()
