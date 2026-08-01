"""The shipped schemas must be TRUE of the mirror, and the mirror validated
against them.

Two contracts drifted apart. ``scripts/validate_shiki.py`` accepts control ids
in either form — the legacy sequential ``T-0001`` and the collision-resistant
``T-<YYYYMMDDTHHMMSSffffffZ>-<8 hex>`` minted by
``shiki_state.new_control_id`` — via a shared dual ``ID_SUFFIX`` alternation. But
the SHIPPED JSON Schemas under ``.shiki/schemas/`` still pinned the legacy-only
``^T-[0-9]{4,}$`` pattern, and nothing ran the real mirror records through those
schemas, so the drift was invisible: the majority of goals, tasks, ledger, dag,
plan, run, report, worktree and runner records did NOT satisfy the schema shipped
beside them. The plan dependency parser in ``scripts/shiki_tasks.py`` carried the
same legacy-only literal, so a plan could not declare a dependency on an
already-registered task by its collision-resistant id.

These tests pin the repaired contract:

* every real mirror record validates against the schema its directory declares;
* no shipped schema may reintroduce a legacy-only id pattern (and the conformance
  loop fails with a pattern-mismatch message if one does);
* a plan dependency declared as a collision-resistant id resolves, and the legacy
  form still works;
* a freshly installed target's goal-creation records validate against the schemas
  installed beside them;
* a ledger entry whose ``task_id`` belongs to a different goal than its
  ``goal_id`` fails validation, while every existing entry still passes.

They exercise the real modules and never modify them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import validate_shiki
from shiki_jsonschema import validate_json_schema, JsonSchemaError
from shiki_process import shiki_path, write_json
from shiki_state import new_control_id
from shiki_tasks import load_task, orchestrate_plan, register_contract_from_plan

REPO_ROOT = shiki_test_support.REPO_ROOT
SCHEMAS_DIR = REPO_ROOT / ".shiki" / "schemas"

# A legacy-only control-id pattern: a prefix, a hyphen, then a bare 4+ digit run
# anchored to end. This is exactly the shape the dual pattern must replace; the
# timestamp alternation ``[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8}`` must always be
# present alongside it.
_LEGACY_ONLY_ID_PATTERN = re.compile(r"\^[A-Z]+-\[0-9\]\{4,\}\$")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _control_dirs(target: Path) -> None:
    for name in (
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
        "schemas",
    ):
        (target / ".shiki" / name).mkdir(parents=True, exist_ok=True)


def _install_schemas(target: Path) -> None:
    (target / ".shiki" / "schemas").mkdir(parents=True, exist_ok=True)
    for schema in SCHEMAS_DIR.glob("*.json"):
        shutil.copy2(schema, target / ".shiki" / "schemas" / schema.name)


def _make_target() -> Path:
    target = Path(tempfile.mkdtemp(prefix="shiki-mirror-schema-"))
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    _control_dirs(target)
    return target


def _frozen_plan(tasks: list[dict], *, plan_id: str | None = None) -> dict:
    plan = {
        "title": "mirror schema conformance plan",
        "outcome": "records validate against the schemas installed beside them",
        "risk_level": "low",
        "grill_with_docs": {"status": "complete"},
        "spec_freeze": {"status": "frozen"},
        "tasks": tasks,
    }
    if plan_id is not None:
        plan["id"] = plan_id
    return plan


def _plan_task(title: str, *, dependencies: list[str] | None = None) -> dict:
    task = {
        "title": title,
        "scope": f"scope for {title}",
        "acceptance_checks": ["python3 scripts/validate_shiki.py"],
        "locks": [],
        "runtime": "claude-code",
    }
    if dependencies is not None:
        task["dependencies"] = dependencies
    return task


class EveryMirrorRecordValidates(unittest.TestCase):
    """Acceptance #1: every record under the mirror validates against its schema."""

    def test_real_mirror_conforms_to_declared_schemas(self) -> None:
        # The real repository mirror must satisfy the shipped schemas. Before the
        # dual-pattern fix 809 of 1007 records failed this.
        validate_shiki.validate_mirror_schema_conformance(REPO_ROOT)

    def test_loop_covers_the_nine_documented_directories(self) -> None:
        self.assertEqual(
            set(validate_shiki.MIRROR_RECORD_SCHEMAS),
            {"goals", "tasks", "ledger", "dag", "plans", "runs", "reports", "worktrees", "runner"},
        )


class NoSchemaUsesLegacyOnlyPattern(unittest.TestCase):
    """Acceptance #2: reintroducing a legacy-only pattern is rejected."""

    def test_no_shipped_schema_pins_a_legacy_only_id_pattern(self) -> None:
        offenders: list[str] = []
        for schema_path in sorted(SCHEMAS_DIR.glob("*.json")):
            for pattern in _iter_patterns(_load(schema_path)):
                if _LEGACY_ONLY_ID_PATTERN.fullmatch(pattern):
                    offenders.append(f"{schema_path.name}: {pattern}")
        self.assertEqual(offenders, [], f"legacy-only id patterns must be gone: {offenders}")

    def test_conformance_loop_fails_when_a_schema_reverts_to_legacy_only(self) -> None:
        target = _make_target()
        try:
            _install_schemas(target)
            # A record whose id is the collision-resistant form the mirror
            # actually uses.
            goal_id = new_control_id("G")
            task_id = new_control_id("T")
            write_json(
                shiki_path(target, "dag", f"{goal_id}.json"),
                {"goal_id": goal_id, "nodes": [task_id], "edges": []},
            )
            # It validates against the shipped (dual) schema.
            validate_shiki.validate_mirror_schema_conformance(target)

            # Revert the dag schema's goal id to the legacy-only pattern.
            schema_path = target / ".shiki" / "schemas" / "dag.schema.json"
            schema = _load(schema_path)
            schema["properties"]["goal_id"]["pattern"] = "^G-[0-9]{4,}$"
            schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")

            with self.assertRaises(validate_shiki.ValidationError) as caught:
                validate_shiki.validate_mirror_schema_conformance(target)
            self.assertIn("does not match pattern", str(caught.exception))
        finally:
            shutil.rmtree(target, ignore_errors=True)


class PlanDependencyIdAlternation(unittest.TestCase):
    """Acceptance #3: a collision-resistant dependency id resolves; legacy works."""

    def test_collision_resistant_and_legacy_dependency_ids_resolve(self) -> None:
        target = _make_target()
        try:
            collision_resistant = "T-20260101T000000000000Z-0000dead"
            legacy = "T-0001"
            plan = _frozen_plan(
                [
                    _plan_task("Alpha"),
                    _plan_task("Beta", dependencies=["Alpha"]),
                    _plan_task("Gamma", dependencies=[collision_resistant]),
                    _plan_task("Delta", dependencies=[legacy]),
                ],
                plan_id=new_control_id("P"),
            )
            result = register_contract_from_plan(target, plan)
            by_title = {load_task(target, tid)["title"]: load_task(target, tid) for tid in result["task_ids"]}

            # Beta depends on Alpha by title -> resolves to Alpha's generated id.
            self.assertEqual(by_title["Beta"]["dependencies"], [by_title["Alpha"]["id"]])
            # Gamma keeps the raw collision-resistant id (the new behavior).
            self.assertEqual(by_title["Gamma"]["dependencies"], [collision_resistant])
            # Delta keeps the raw legacy id (the pre-existing behavior).
            self.assertEqual(by_title["Delta"]["dependencies"], [legacy])
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_a_non_id_non_title_dependency_still_raises(self) -> None:
        target = _make_target()
        try:
            plan = _frozen_plan(
                [_plan_task("Alpha", dependencies=["not-a-task"])],
                plan_id=new_control_id("P"),
            )
            with self.assertRaises(Exception) as caught:
                register_contract_from_plan(target, plan)
            self.assertIn("unknown dependency", str(caught.exception))
        finally:
            shutil.rmtree(target, ignore_errors=True)


class FreshInstallGoalCreationValidates(unittest.TestCase):
    """Acceptance #4: fresh install + goal creation records validate."""

    def test_orchestrated_records_validate_against_installed_schemas(self) -> None:
        target = _make_target()
        try:
            _install_schemas(target)
            plan = _frozen_plan([_plan_task("Alpha"), _plan_task("Beta", dependencies=["Alpha"])])
            orchestrate_plan(target, plan)

            # Records were produced with collision-resistant ids in every id-bearing
            # directory (goals, tasks, dag, ledger, runs, worktrees, plans).
            produced = 0
            for subdir in validate_shiki.MIRROR_RECORD_SCHEMAS:
                produced += len(list((target / ".shiki" / subdir).glob("*.json")))
            self.assertGreater(produced, 0, "goal creation produced no records")

            # Each validates against the schema installed beside it.
            validate_shiki.validate_mirror_schema_conformance(target)
        finally:
            shutil.rmtree(target, ignore_errors=True)


class LedgerTaskGoalPairing(unittest.TestCase):
    """Acceptance #5: a ledger entry's task_id must belong to its goal_id."""

    def _entry(self, *, goal_id: str, task_id: str) -> dict:
        return {
            "id": new_control_id("L"),
            "timestamp": "2026-08-01T00:00:00Z",
            "goal_id": goal_id,
            "task_id": task_id,
            "type": "check",
            "actor": "fixture",
            "summary": "fixture entry",
            "evidence": ["fixture"],
        }

    def test_mismatched_pairing_fails_matched_passes(self) -> None:
        goal_a = "G-20260101T000000000000Z-0000a001"
        goal_b = "G-20260101T000000000000Z-0000b002"
        task = "T-20260101T000000000000Z-0000c003"
        known_tasks = {task}
        known_goals = {goal_a, goal_b}
        task_goal_by_id = {task: goal_a}
        path = Path(".shiki/ledger/fixture.json")

        # Matched: the task belongs to the same goal the entry names.
        validate_shiki.validate_ledger(
            path, self._entry(goal_id=goal_a, task_id=task), known_tasks, known_goals, task_goal_by_id
        )

        # Mismatched: the task belongs to goal_a but the entry claims goal_b.
        with self.assertRaises(validate_shiki.ValidationError) as caught:
            validate_shiki.validate_ledger(
                path, self._entry(goal_id=goal_b, task_id=task), known_tasks, known_goals, task_goal_by_id
            )
        message = str(caught.exception)
        self.assertIn(task, message)
        self.assertIn(goal_a, message)

    def test_every_existing_ledger_entry_still_pairs(self) -> None:
        shiki = REPO_ROOT / ".shiki"
        known_goals = {_load(p)["id"] for p in (shiki / "goals").glob("*.json")}
        task_goal_by_id = {
            _load(p)["id"]: str(_load(p).get("goal_id") or "") for p in (shiki / "tasks").glob("*.json")
        }
        known_tasks = set(task_goal_by_id)
        for ledger_path in (shiki / "ledger").glob("*.json"):
            validate_shiki.validate_ledger(
                ledger_path, _load(ledger_path), known_tasks, known_goals, task_goal_by_id
            )


def _iter_patterns(node: object) -> list[str]:
    """Every ``pattern`` string anywhere in a JSON Schema tree."""
    found: list[str] = []
    if isinstance(node, dict):
        pattern = node.get("pattern")
        if isinstance(pattern, str):
            found.append(pattern)
        for value in node.values():
            found.extend(_iter_patterns(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_iter_patterns(value))
    return found


if __name__ == "__main__":
    unittest.main()
