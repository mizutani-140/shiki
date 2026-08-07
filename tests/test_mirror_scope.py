"""MergeGate scope enforcement must see the WHOLE diff.

Three historical gaps let a PR mutate mirror state its declared locks never
covered, and one gap made a legitimate schema file permanently unmergeable:

  (a) A rename is presented to git's name-only diff as its NEW path only, so
      ``files_outside_locks`` never saw the rename's source leg. A rename could
      move a file OUT of locked scope (its delete leg) unseen. MergeGate now
      derives the lock-scope path set from ``files_status`` — which decomposes a
      rename into D(old)+A(new) — and passes THAT to both the files-outside-locks
      and the active-lock-conflict gates.

  (b) Only ``.shiki`` mirror directories with a dedicated per-record rule (tasks,
      goals, locks, ledger, repairs) were policed by
      ``enforce_untrusted_shiki_mutations``. The eleven other mirror directories
      (dag, plans, reports, memories, runs, worktrees, handoffs, runner, smoke,
      starts, inbox) had none, so a PR with a broad lock (``path:.shiki/**``)
      could delete a foreign goal's DAG or rewrite another goal's report and
      files-outside-locks would wave it through. A generic mirror-class rule now
      blocks deletion and requires goal/task scoping, and the base-snapshot
      delete-protection loop covers every tracked mirror directory.

  (c) ``runtime_evidence_path`` matched runtime evidence by a ``startswith``
      prefix, so ``.shiki/schemas/cca-verdict.schema.json`` — a real repository
      contract — was misclassified as forged runtime evidence and could never
      merge. It now matches the exact runtime filenames.

And Spec Freeze is operator-only (SADR-0009): no PR may author or alter a
``spec_freeze`` block in ANY mirror state, including its own goal's source plan.
Goal/task scoping alone stops a PR from writing a FOREIGN plan, but still lets an
implementation PR grant itself a Spec Freeze, or append amendments, on the plan
that governs it.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from mergegate_check import (
    ChangedFile,
    enforce_untrusted_shiki_mutations,
    lock_scope_paths,
    runtime_evidence_path,
)
from shiki_locks import files_outside_locks
from shiki_manifest import load_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]

GOAL_ID = "G-20260731T000000000000Z-aaaaaaaa"
FOREIGN_GOAL_ID = "G-20260731T000000000001Z-bbbbbbbb"
TASK_ID = "T-20260731T000000000000Z-cccccccc"
PLAN_ID = "P-20260731T000000000000Z-dddddddd"

MIRROR_DIRS = (
    "dag",
    "goals",
    "tasks",
    "plans",
    "reports",
    "memories",
    "runs",
    "worktrees",
    "handoffs",
    "runner",
    "smoke",
    "starts",
    "inbox",
    "ledger",
    "locks",
    "repairs",
)


def _make_target() -> Path:
    tmp = Path(tempfile.mkdtemp())
    for name in MIRROR_DIRS:
        (tmp / ".shiki" / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / ".shiki" / "manifest.json", tmp / ".shiki" / "manifest.json")
    return tmp


def _write_json(path: Path, data: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _goal(target: Path, *, goal_id: str = GOAL_ID, source_plan: str | None = None) -> None:
    payload: dict = {"id": goal_id, "status": "in-progress", "risk_level": "low"}
    if source_plan is not None:
        payload["source_plan"] = source_plan
    _write_json(target / ".shiki" / "goals" / f"{goal_id}.json", payload)


def _task(*, goal_id: str = GOAL_ID, task_id: str = TASK_ID) -> dict:
    return {"id": task_id, "goal_id": goal_id, "locks": [], "ledger_evidence": []}


def _run(
    target: Path,
    changed: list[ChangedFile],
    *,
    goal_id: str = GOAL_ID,
    task_id: str = TASK_ID,
    base_shiki: Path | None = None,
    task: dict | None = None,
) -> list[str]:
    blocking: list[str] = []
    warnings: list[str] = []
    enforce_untrusted_shiki_mutations(
        target=target,
        manifest=load_manifest(target),
        base_shiki=base_shiki,
        changed_files_status=changed,
        task=task or _task(goal_id=goal_id, task_id=task_id),
        goal_id=goal_id,
        task_id=task_id,
        pr={"number": 123},
        blocking=blocking,
        warnings=warnings,
    )
    return blocking


# ---------------------------------------------------------------------------
# (a) A rename must be seen through both legs by the lock-scope gates.
# ---------------------------------------------------------------------------
class RenameLockScopeTests(unittest.TestCase):
    def test_rename_out_of_locked_scope_is_seen(self) -> None:
        # parse_changed_files_status decomposes `R tests/foo.py docs/foo.py`
        # into D(tests/foo.py)+A(docs/foo.py). The source leg is outside docs/**.
        status = [
            ChangedFile("D", "tests/foo.py"),
            ChangedFile("A", "docs/foo.py", old_path="tests/foo.py"),
        ]
        paths = lock_scope_paths(status)
        self.assertEqual(paths, ["tests/foo.py", "docs/foo.py"])
        self.assertEqual(files_outside_locks(paths, ["path:docs/**"]), ["tests/foo.py"])

    def test_rename_inside_locked_scope_is_clean(self) -> None:
        status = [
            ChangedFile("D", "docs/a.py"),
            ChangedFile("A", "docs/b.py", old_path="docs/a.py"),
        ]
        paths = lock_scope_paths(status)
        self.assertEqual(files_outside_locks(paths, ["path:docs/**"]), [])

    def test_lock_scope_paths_are_deduped_and_normalized(self) -> None:
        status = [
            ChangedFile("M", "./docs/a.py"),
            ChangedFile("M", "docs/a.py"),
        ]
        self.assertEqual(lock_scope_paths(status), ["docs/a.py"])


# ---------------------------------------------------------------------------
# (c) runtime_evidence_path must match the EXACT runtime filenames only.
# ---------------------------------------------------------------------------
class RuntimeEvidencePathTests(unittest.TestCase):
    def test_schema_file_is_not_runtime_evidence(self) -> None:
        self.assertFalse(runtime_evidence_path(".shiki/schemas/cca-verdict.schema.json"))
        self.assertFalse(runtime_evidence_path(".shiki/schemas/mergegate-result.schema.json"))

    def test_exact_runtime_filenames_still_flagged_anywhere(self) -> None:
        self.assertTrue(runtime_evidence_path(".shiki/somewhere/cca-verdict.json"))
        self.assertTrue(runtime_evidence_path(".shiki/somewhere/mergegate-result.json"))
        self.assertTrue(runtime_evidence_path(".shiki/gha/cca-verdict.json"))
        self.assertTrue(runtime_evidence_path(".shiki/gha/whatever.json"))
        self.assertTrue(runtime_evidence_path(".shiki/cca/cca-verdict.json"))

    def test_unrelated_mirror_record_is_not_runtime_evidence(self) -> None:
        self.assertFalse(runtime_evidence_path(".shiki/reports/R-1.json"))


# ---------------------------------------------------------------------------
# (b) Generic mirror-class rule: block deletion, require goal/task scoping.
# ---------------------------------------------------------------------------
class GenericMirrorScopeTests(unittest.TestCase):
    def test_deleting_foreign_dag_blocks_without_base(self) -> None:
        target = _make_target()
        _goal(target)
        blocking = _run(target, [ChangedFile("D", f".shiki/dag/{FOREIGN_GOAL_ID}.json")])
        self.assertTrue(any("must not delete" in b and "dag" in b for b in blocking), blocking)

    def test_deleting_foreign_dag_blocks_with_base(self) -> None:
        target = _make_target()
        _goal(target)
        base = target / ".shiki" / "gha" / "base-shiki" / ".shiki"
        _write_json(base / "dag" / f"{FOREIGN_GOAL_ID}.json", {"goal_id": FOREIGN_GOAL_ID})
        blocking = _run(
            target,
            [ChangedFile("D", f".shiki/dag/{FOREIGN_GOAL_ID}.json")],
            base_shiki=base,
        )
        self.assertTrue(any("must not delete" in b and "dag" in b for b in blocking), blocking)

    def test_modifying_foreign_report_blocks(self) -> None:
        target = _make_target()
        _goal(target)
        rel = ".shiki/reports/R-foreign.json"
        _write_json(target / rel, {"id": "R-foreign", "goal_id": FOREIGN_GOAL_ID})
        blocking = _run(target, [ChangedFile("M", rel)])
        self.assertTrue(any("not scoped" in b and "reports" in b for b in blocking), blocking)

    def test_foreign_worktree_runner_run_memory_all_block(self) -> None:
        target = _make_target()
        _goal(target)
        for rel, payload in {
            ".shiki/worktrees/T-OTHER.json": {"id": "T-OTHER", "task_id": "T-OTHER", "goal_id": FOREIGN_GOAL_ID},
            ".shiki/runner/EXEC-OTHER.json": {"id": "EXEC-OTHER", "task_id": "T-OTHER", "goal_id": FOREIGN_GOAL_ID},
            ".shiki/runs/RUN-OTHER.json": {"id": "RUN-OTHER", "goal_id": FOREIGN_GOAL_ID},
            ".shiki/memories/M-OTHER.json": {"id": "M-OTHER", "goal_id": FOREIGN_GOAL_ID},
        }.items():
            _write_json(target / rel, payload)
            blocking = _run(target, [ChangedFile("M", rel)])
            self.assertTrue(any("not scoped" in b for b in blocking), (rel, blocking))

    def test_different_task_same_goal_record_is_not_scoped(self) -> None:
        # Anti-padding: a record naming a DIFFERENT task, even of the PR's own goal,
        # is not own-scoped (mirrors ledger_entry_allowed_for_task).
        target = _make_target()
        _goal(target)
        rel = ".shiki/runner/EXEC-SIB.json"
        _write_json(target / rel, {"id": "EXEC-SIB", "task_id": "T-SIBLING", "goal_id": GOAL_ID})
        blocking = _run(target, [ChangedFile("M", rel)])
        self.assertTrue(any("not scoped" in b for b in blocking), blocking)

    def test_own_task_and_goal_scoped_records_pass(self) -> None:
        target = _make_target()
        _goal(target)
        # id-named own records (dag by goal id, worktree by task id).
        _write_json(target / ".shiki" / "dag" / f"{GOAL_ID}.json", {"goal_id": GOAL_ID})
        _write_json(target / ".shiki" / "worktrees" / f"{TASK_ID}.json", {"id": TASK_ID, "task_id": TASK_ID})
        # content-scoped own records (a goal-level report, a task-level runner rec).
        _write_json(target / ".shiki" / "reports" / "R-own.json", {"id": "R-own", "goal_id": GOAL_ID})
        _write_json(target / ".shiki" / "runner" / "EXEC-own.json", {"id": "EXEC-own", "task_id": TASK_ID, "goal_id": GOAL_ID})
        changed = [
            ChangedFile("M", f".shiki/dag/{GOAL_ID}.json"),
            ChangedFile("M", f".shiki/worktrees/{TASK_ID}.json"),
            ChangedFile("A", ".shiki/reports/R-own.json"),
            ChangedFile("A", ".shiki/runner/EXEC-own.json"),
        ]
        self.assertEqual(_run(target, changed), [])

    def test_base_delete_loop_covers_mirror_dirs(self) -> None:
        # A base report absent at head is a deletion the base-snapshot loop must
        # catch even when no diff entry names it.
        target = _make_target()
        _goal(target)
        base = target / ".shiki" / "gha" / "base-shiki" / ".shiki"
        _write_json(base / "reports" / "R-base.json", {"id": "R-base", "goal_id": GOAL_ID})
        blocking = _run(target, [], base_shiki=base)
        self.assertTrue(any("must not delete base" in b and "reports" in b for b in blocking), blocking)


# ---------------------------------------------------------------------------
# plans scoping + spec_freeze (operator-only) enforcement.
# ---------------------------------------------------------------------------
class PlanScopeAndSpecFreezeTests(unittest.TestCase):
    def _base_with_plan(self, target: Path, plan: dict) -> Path:
        base = target / ".shiki" / "gha" / "base-shiki" / ".shiki"
        _write_json(base / "plans" / f"{PLAN_ID}.json", plan)
        return base

    def test_new_plan_that_is_not_source_plan_blocks(self) -> None:
        target = _make_target()
        _goal(target, source_plan=PLAN_ID)
        rel = ".shiki/plans/P-OTHER.json"
        _write_json(target / rel, {"id": "P-OTHER"})
        blocking = _run(target, [ChangedFile("A", rel)])
        self.assertTrue(any("source_plan" in b for b in blocking), blocking)

    def test_adding_spec_freeze_to_own_source_plan_blocks(self) -> None:
        target = _make_target()
        _goal(target, source_plan=PLAN_ID)
        rel = f".shiki/plans/{PLAN_ID}.json"
        # base plan has NO spec_freeze; head plan grants itself one.
        base = self._base_with_plan(target, {"id": PLAN_ID, "title": "p"})
        _write_json(target / rel, {"id": PLAN_ID, "title": "p", "spec_freeze": {"status": "frozen"}})
        blocking = _run(target, [ChangedFile("M", rel)], base_shiki=base)
        self.assertTrue(any("spec_freeze" in b for b in blocking), blocking)

    def test_modifying_existing_spec_freeze_blocks(self) -> None:
        target = _make_target()
        _goal(target, source_plan=PLAN_ID)
        rel = f".shiki/plans/{PLAN_ID}.json"
        base_plan = {"id": PLAN_ID, "spec_freeze": {"status": "frozen", "amendments": []}}
        base = self._base_with_plan(target, base_plan)
        head_plan = {"id": PLAN_ID, "spec_freeze": {"status": "frozen", "amendments": ["smuggled amendment"]}}
        _write_json(target / rel, head_plan)
        blocking = _run(target, [ChangedFile("M", rel)], base_shiki=base)
        self.assertTrue(any("spec_freeze" in b for b in blocking), blocking)

    def test_touching_own_plan_without_altering_spec_freeze_is_unaffected(self) -> None:
        target = _make_target()
        _goal(target, source_plan=PLAN_ID)
        rel = f".shiki/plans/{PLAN_ID}.json"
        frozen = {"status": "frozen", "approved_by": "operator"}
        base = self._base_with_plan(target, {"id": PLAN_ID, "title": "p", "spec_freeze": frozen, "notes": "old"})
        _write_json(target / rel, {"id": PLAN_ID, "title": "p", "spec_freeze": frozen, "notes": "new"})
        self.assertEqual(_run(target, [ChangedFile("M", rel)], base_shiki=base), [])


if __name__ == "__main__":
    unittest.main()
