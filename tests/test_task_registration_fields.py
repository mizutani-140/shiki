"""What a registered task record carries, for the ISS checklist family to judge.

``docs/agents/checklists.md`` marks every ISS row **blocking**, and the CCA judges
those rows against the ``.shiki`` task record. Ten of the eleven rows map onto a
field ``task.schema.json`` defines; two did not hold up in practice:

  * **ISS-05** ("AFK/HITL classification is explicit") had NO backing field at all.
    The schema defined no afk/hitl/readiness property, so no task record could
    satisfy the row. Measured 2026-08-24 on Contract PR #341 (run 32677302308):
    the CCA scored it ``insufficient_evidence`` -- "task.json has no explicit
    AFK/HITL field; assigned_runtime=claude-code implies AFK by SADR-0008 default
    but this is not stated explicitly on the task record" -- and
    ``enforce_cca_verdict`` correctly refused the ``complete`` verdict. On the
    immediately preceding run at a different head SHA the same PR passed, because
    that judge scored only CCA-01..CCA-12 and never reached ISS-*. Identical
    content, opposite gate results.
  * **ISS-11** ("CCA checklist profile is listed") had a field, but neither
    registration path wrote it, so a planner's declared profile reached no task
    file. That carry landed separately on main as PR #352, and
    ``tests/test_plan_task_carry.py`` owns its behaviour — this module does not
    duplicate it. Pair 16 still binds the field structurally, because ISS-11 is
    judged from it whoever implements the carry.

This suite pins the behaviour that closes both. ``dispatch_mode`` is the explicit
AFK/HITL surface; it is deliberately OPTIONAL in the schema (promoting it would
invalidate every task file registered before it existed), so ISS-05's row also
carries a total fallback -- derive from ``assigned_runtime`` -- and
``dispatch_mode_for_runtime`` is that rule as executable code. The name is
``dispatch_mode`` and not ``execution_mode`` because
``shiki_runtime_registry.RuntimeDescriptor.execution_mode`` already means
something else (``local_cli``/``github_action``/``workflow_job``/...).

The structural binder -- blocking ISS rows must map onto fields the schema
actually defines and both registration paths actually emit -- lives in
``tests/test_paired_invariants.py`` Pair 16. This module covers the behaviour.

The suite is dependency free: standard-library ``unittest`` only.
"""

from __future__ import annotations

import argparse
import io
import json
import contextlib
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_tasks import (
    cmd_issue_plan,
    dispatch_mode_for_runtime,
    load_task,
    register_task_from_plan,
)

CONTROL_DIRS = ("goals", "tasks", "dag", "ledger", "locks", "plans")


def _make_target() -> Path:
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(tmp)], check=True)
    # cmd_issue_plan enforces a GitHub-first target before it writes anything.
    subprocess.run(
        ["git", "-C", str(tmp), "remote", "add", "origin", "https://github.com/example/target.git"],
        check=True,
    )
    for name in CONTROL_DIRS:
        (tmp / ".shiki" / name).mkdir(parents=True, exist_ok=True)
    return tmp


def _goal(target: Path, goal_id: str = "G-0001") -> str:
    (target / ".shiki" / "goals" / f"{goal_id}.json").write_text(
        json.dumps({"id": goal_id, "risk_level": "low"}), encoding="utf-8"
    )
    return goal_id


def _task_plan(**overrides) -> dict:
    plan = {
        "title": "A slice",
        "scope": "scope for a slice",
        "acceptance_checks": ["works"],
        "locks": ["path:scripts/one.py"],
    }
    plan.update(overrides)
    return plan


def _register_via_cli(target: Path, goal_id: str, **overrides) -> str:
    """Drive ``cmd_issue_plan`` through its argparse namespace, as the CLI does."""
    namespace = argparse.Namespace(
        target=str(target),
        goal_id=goal_id,
        title="A slice",
        scope="scope for a slice",
        non_goal=[],
        dependency=[],
        lock=["path:scripts/one.py"],
        runtime="claude-code",
        risk_level="low",
        required_skill=["tdd"],
        acceptance_check=["works"],
        expected_branch=None,
        expected_pr=None,
        github_issue=None,
        dispatch_mode=None,
        cca_checklist_profile=[],
    )
    for key, value in overrides.items():
        setattr(namespace, key, value)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = cmd_issue_plan(namespace)
    assert exit_code == 0, f"cmd_issue_plan failed: {buffer.getvalue()}"
    return json.loads(buffer.getvalue())["task_id"]


class DispatchModeDerivation(unittest.TestCase):
    """``dispatch_mode_for_runtime`` is the ISS-05 fallback, as executable code.

    ISS-05's row states the rule in prose for the CCA; this function states it for
    the registration paths. Both must say the same thing or a record's stored
    value and the row's fallback can disagree.
    """

    def test_human_runtime_is_hitl(self) -> None:
        # `human` is the manual HITL approval/review surface in the runtime
        # registry -- the one runtime whose execution IS a human decision.
        self.assertEqual(dispatch_mode_for_runtime("human"), "hitl")

    def test_every_other_registry_runtime_is_afk(self) -> None:
        # SADR-0008: Claude Code is the default AFK implementer, and every
        # non-human runtime executes without a human in the loop.
        for runtime in ("codex", "codex-front", "claude-code", "github-actions", "hermes-runner", "other"):
            with self.subTest(runtime=runtime):
                self.assertEqual(dispatch_mode_for_runtime(runtime), "afk")

    def test_unknown_or_absent_runtime_is_afk(self) -> None:
        # The fallback must be TOTAL: ISS-05 has to be judgeable for a record
        # whose runtime is missing or unrecognised, not merely for known ones.
        for runtime in (None, "", "not-a-runtime"):
            with self.subTest(runtime=runtime):
                self.assertEqual(dispatch_mode_for_runtime(runtime), "afk")


class PlanRegistrationCarriesFields(unittest.TestCase):
    """``register_task_from_plan`` writes both ISS-backing fields."""

    def test_dispatch_mode_defaults_from_runtime(self) -> None:
        target = _make_target()
        goal_id = _goal(target)
        task_id, _ = register_task_from_plan(
            target, goal_id=goal_id, task_plan=_task_plan(runtime="human"), dependencies=[]
        )
        self.assertEqual(load_task(target, task_id)["dispatch_mode"], "hitl")

        task_id, _ = register_task_from_plan(
            target, goal_id=goal_id, task_plan=_task_plan(runtime="claude-code"), dependencies=[]
        )
        self.assertEqual(load_task(target, task_id)["dispatch_mode"], "afk")

    def test_explicit_plan_dispatch_mode_wins_over_the_derivation(self) -> None:
        # The point of the field: a slice run by an automated runtime can still
        # be marked HITL because a human decision is load-bearing (CLAUDE.md
        # "Prefer AFK slices, but mark HITL when a human decision is still
        # load-bearing"). The derivation alone cannot express that.
        target = _make_target()
        goal_id = _goal(target)
        task_id, _ = register_task_from_plan(
            target,
            goal_id=goal_id,
            task_plan=_task_plan(runtime="claude-code", dispatch_mode="hitl"),
            dependencies=[],
        )
        self.assertEqual(load_task(target, task_id)["dispatch_mode"], "hitl")

class CliRegistrationCarriesFields(unittest.TestCase):
    """``cmd_issue_plan`` writes the same two fields as the plan path."""

    def test_dispatch_mode_defaults_from_runtime(self) -> None:
        target = _make_target()
        goal_id = _goal(target)
        task_id = _register_via_cli(target, goal_id, runtime="human")
        self.assertEqual(load_task(target, task_id)["dispatch_mode"], "hitl")

        task_id = _register_via_cli(target, goal_id, runtime="codex")
        self.assertEqual(load_task(target, task_id)["dispatch_mode"], "afk")

    def test_explicit_flag_wins_over_the_derivation(self) -> None:
        target = _make_target()
        goal_id = _goal(target)
        task_id = _register_via_cli(target, goal_id, runtime="claude-code", dispatch_mode="hitl")
        self.assertEqual(load_task(target, task_id)["dispatch_mode"], "hitl")

class RegisteredRecordsStayValid(unittest.TestCase):
    """The new fields must not break the schema or the validator.

    ``dispatch_mode`` stays out of ``required`` on purpose: promoting it would
    invalidate all 153 task files registered before it existed, and
    ``task.schema.json.required`` and ``validate_shiki.TASK_REQUIRED`` must be
    promoted together (``tests/test_cca_verdict_consistency``).
    """

    def _schema(self) -> dict:
        root = Path(__file__).resolve().parents[1]
        return json.loads((root / ".shiki" / "schemas" / "task.schema.json").read_text(encoding="utf-8"))

    def test_dispatch_mode_is_defined_and_optional(self) -> None:
        schema = self._schema()
        self.assertEqual(set(schema["properties"]["dispatch_mode"]["enum"]), {"afk", "hitl"})
        self.assertNotIn("dispatch_mode", schema["required"])

    def test_registered_task_validates_against_the_schema(self) -> None:
        from shiki_schema import validate_instance

        target = _make_target()
        goal_id = _goal(target, "G-20260824T010736685173Z-c1131b6d")
        task_id, _ = register_task_from_plan(
            target, goal_id=goal_id, task_plan=_task_plan(), dependencies=[]
        )
        validate_instance(load_task(target, task_id), self._schema())

    def _registered_record(self) -> tuple[Path, dict]:
        target = _make_target()
        goal_id = _goal(target, "G-20260824T010736685173Z-c1131b6d")
        task_id, _ = register_task_from_plan(
            target, goal_id=goal_id, task_plan=_task_plan(), dependencies=[]
        )
        path = target / ".shiki" / "tasks" / f"{task_id}.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    def test_validator_rejects_an_out_of_enum_dispatch_mode(self) -> None:
        import validate_shiki

        path, record = self._registered_record()
        record["dispatch_mode"] = "sometimes"
        with self.assertRaises(validate_shiki.ValidationError) as caught:
            validate_shiki.validate_task(path, record)
        self.assertIn("dispatch_mode", str(caught.exception))

    def test_validator_accepts_a_record_without_dispatch_mode(self) -> None:
        # The 153 pre-existing records have no such field and must stay valid.
        import validate_shiki

        path, record = self._registered_record()
        record.pop("dispatch_mode")
        validate_shiki.validate_task(path, record)  # must not raise


if __name__ == "__main__":
    unittest.main()
