"""The declared CCA checklist profile must survive BOTH task-registration paths.

A plan (or a `shiki issue plan` invocation) may declare a task's
``cca_checklist_profile`` — the CCA checklist ids the PR's structured verdict
must judge to a terminal status. ``scripts/mergegate_check.checklist_profile_coverage_failures``
is the field's only consumer: it blocks a verdict that omits or leaves
unresolved any declared id. But that consumer treats an ABSENT profile and an
EMPTY one identically, so a registration payload that silently drops the field
produces no error — the declared governance requirement just disappears
(observed on a real merged Contract PR). Neither registration payload in
``scripts/shiki_tasks.py`` carried the field, and nothing bound the two paths so
one could reveal the other's omission.

These tests pin the carry on each path and BIND the pair:

  * ``register_task_from_plan`` and ``cmd_issue_plan`` each carry a declared
    profile verbatim, turn an omitted profile into a PRESENT empty list, and keep
    a declared ``[]`` empty;
  * the carry actually ARMS MergeGate — a task registered with ``["CCA-01"]``
    makes the coverage gate block a verdict that judges only ``CCA-02``;
  * a set-equality binder proves the two paths write the SAME task-record keys,
    naming both call sites and the fix in its failure message.

Each enforcement and binder assertion is paired with a DIVERGENCE test that
strips the field from one side and proves the assertion then fails, so the suite
is shown to discriminate rather than merely to pass on today's tree.

Target-validity: every fixture is a throwaway temp-directory target; the only
scripts modules imported (``shiki_tasks``, ``shiki_cli``, ``mergegate_check``,
``shiki_installer``) are shipped by ``shiki_installer.TEMPLATE_PATHS``; and the
suite asserts nothing about the platform's own ``.shiki`` corpus. So the file
needs no ``TARGET_INSTALL_EXCLUDES`` entry and passes from a freshly installed
target.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from mergegate_check import checklist_profile_coverage_failures
from shiki_cli import build_parser
from shiki_tasks import cmd_issue_plan, load_task, register_task_from_plan

# Synthetic goal id, distinct from any real mirror id; every test writes only
# into its own temp target, never the platform corpus.
GOAL_ID = "G-20260824T000000000000Z-0000cafe"

# Script modules this file imports. Each must be shipped so the test can run from
# a freshly installed target (TargetValidityTests binds this).
IMPORTED_SCRIPT_MODULES = ("shiki_tasks", "shiki_cli", "mergegate_check", "shiki_installer")

# The .shiki subtrees a registration touches (task file, its ledger, the DAG, the
# goal it reads).
CONTROL_DIRS = ("goals", "tasks", "dag", "ledger")

# Sentinel distinguishing "the caller omitted the field" from "the caller
# declared an empty list" — the two cases must both yield a PRESENT empty list,
# but they reach the payload differently.
_OMIT = object()


def _binder_message(plan_keys: set[str], cli_keys: set[str]) -> str:
    """Failure message for the set-equality binder.

    Names the diverging keys AND both call sites in scripts/shiki_tasks.py plus
    the fix, so a partial carry (one path updated, the other not) reads as an
    actionable instruction rather than a bare set inequality.
    """
    diff = sorted(plan_keys.symmetric_difference(cli_keys))
    return (
        f"task-record key sets diverge ({diff}) between the two registration "
        "paths: register_task_from_plan and cmd_issue_plan in "
        "scripts/shiki_tasks.py must write the SAME task-record keys. Fix: carry "
        "cca_checklist_profile in BOTH registration payloads in "
        "scripts/shiki_tasks.py so neither path silently discards a declared field."
    )


class _CarryTestCase(unittest.TestCase):
    """Base with a disposable GitHub-first target and both registration paths."""

    def make_target(self) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        target = Path(tmp)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        # cmd_issue_plan requires a GitHub-first target (git repo + GitHub origin).
        subprocess.run(
            ["git", "-C", str(target), "remote", "add", "origin", "https://github.com/example/target.git"],
            check=True,
        )
        for name in CONTROL_DIRS:
            (target / ".shiki" / name).mkdir(parents=True, exist_ok=True)
        (target / ".shiki" / "goals" / f"{GOAL_ID}.json").write_text(
            json.dumps({"id": GOAL_ID, "title": "carry test goal", "risk_level": "low", "status": "planned"}),
            encoding="utf-8",
        )
        return target

    def _plan_task(self, *, profile: object = _OMIT, title: str = "Plan path task") -> dict:
        entry = {
            "title": title,
            "scope": "scope for the plan path",
            "acceptance_checks": ["works"],
            "locks": ["path:scripts/plan_path.py"],
            "runtime": "claude-code",
        }
        if profile is not _OMIT:
            entry["cca_checklist_profile"] = profile
        return entry

    def register_via_plan(self, target: Path, *, profile: object = _OMIT, title: str = "Plan path task") -> dict:
        task_id, _ = register_task_from_plan(
            target,
            goal_id=GOAL_ID,
            task_plan=self._plan_task(profile=profile, title=title),
            dependencies=[],
        )
        return load_task(target, task_id)

    def register_via_cli(self, target: Path, *, profile: object = _OMIT, title: str = "Cli path task") -> dict:
        argv = [
            "issue", "plan",
            "--target", str(target),
            "--goal-id", GOAL_ID,
            "--title", title,
            "--scope", "scope for the cli path",
            "--lock", "path:scripts/cli_path.py",
            "--acceptance-check", "works",
        ]
        if profile is not _OMIT:
            for profile_id in profile:  # type: ignore[union-attr]
                argv += ["--cca-checklist-profile", profile_id]
        args = build_parser().parse_args(argv)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cmd_issue_plan(args), 0)
        task_id = json.loads(buffer.getvalue())["task_id"]
        return load_task(target, task_id)


class PlanPathCarryTests(_CarryTestCase):
    def test_declared_profile_carried_verbatim(self) -> None:
        task = self.register_via_plan(self.make_target(), profile=["CCA-01", "CCA-09"])
        self.assertEqual(task["cca_checklist_profile"], ["CCA-01", "CCA-09"])

    def test_omitted_profile_becomes_present_empty_list(self) -> None:
        task = self.register_via_plan(self.make_target(), profile=_OMIT)
        self.assertIn("cca_checklist_profile", task)
        self.assertEqual(task["cca_checklist_profile"], [])

    def test_declared_empty_stays_empty(self) -> None:
        task = self.register_via_plan(self.make_target(), profile=[])
        self.assertIn("cca_checklist_profile", task)
        self.assertEqual(task["cca_checklist_profile"], [])


class CliPathCarryTests(_CarryTestCase):
    def test_declared_profile_carried_verbatim(self) -> None:
        task = self.register_via_cli(self.make_target(), profile=["CCA-01", "CCA-09"])
        self.assertEqual(task["cca_checklist_profile"], ["CCA-01", "CCA-09"])

    def test_omitted_profile_becomes_present_empty_list(self) -> None:
        task = self.register_via_cli(self.make_target(), profile=_OMIT)
        self.assertIn("cca_checklist_profile", task)
        self.assertEqual(task["cca_checklist_profile"], [])


class CliFlagDeclarationTests(unittest.TestCase):
    """--cca-checklist-profile is a repeatable action="append" option, default []."""

    @staticmethod
    def _base_argv(*profiles: str) -> list[str]:
        argv = ["issue", "plan", "--goal-id", GOAL_ID, "--title", "t", "--scope", "s", "--acceptance-check", "a"]
        for profile_id in profiles:
            argv += ["--cca-checklist-profile", profile_id]
        return argv

    def test_defaults_to_empty_list_when_absent(self) -> None:
        args = build_parser().parse_args(self._base_argv())
        self.assertEqual(args.cca_checklist_profile, [])

    def test_is_repeatable_and_appends(self) -> None:
        args = build_parser().parse_args(self._base_argv("CCA-01", "CCA-09"))
        self.assertEqual(args.cca_checklist_profile, ["CCA-01", "CCA-09"])

    def test_help_lists_the_flag(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit):
            build_parser().parse_args(["issue", "plan", "--help"])
        self.assertIn("--cca-checklist-profile", buffer.getvalue())


class EnforcementArmingTests(_CarryTestCase):
    """The carried profile arms mergegate_check's coverage gate — and the
    assertion discriminates: strip the field and the gate falls silent."""

    VERDICT = {"checklist": [{"id": "CCA-02", "status": "pass"}]}

    def test_carry_arms_coverage_gate(self) -> None:
        task = self.register_via_plan(self.make_target(), profile=["CCA-01"])
        reasons = checklist_profile_coverage_failures(task, self.VERDICT)
        self.assertTrue(reasons, "a declared CCA-01 must be reported when the verdict judges only CCA-02")
        self.assertTrue(any("CCA-01" in reason for reason in reasons), reasons)

    def test_enforcement_discriminates_when_field_stripped(self) -> None:
        # Divergence: without the carried field the very same call yields no
        # reasons, proving test_carry_arms_coverage_gate would fail on a task that
        # dropped the profile (i.e. the assertion measures the carry, not luck).
        task = self.register_via_plan(self.make_target(), profile=["CCA-01"])
        stripped = dict(task)
        stripped.pop("cca_checklist_profile")
        self.assertEqual(checklist_profile_coverage_failures(stripped, self.VERDICT), [])


class RegistrationBinderTests(_CarryTestCase):
    """Set-equality binder over the two paths' task-record key sets, plus a
    divergence test proving the binder fails when one path drops the field."""

    def test_key_sets_are_equal_across_paths(self) -> None:
        target = self.make_target()
        plan_keys = set(self.register_via_plan(target, profile=["CCA-01"]))
        cli_keys = set(self.register_via_cli(target, profile=["CCA-01"]))
        self.assertEqual(plan_keys, cli_keys, _binder_message(plan_keys, cli_keys))

    def test_binder_fails_when_one_path_drops_the_field(self) -> None:
        # Divergence: reproduce a partial carry (one path updated, the other not)
        # by removing the field from one record; the set-equality assertion must
        # then fail, and its message must name both call sites and the fix.
        target = self.make_target()
        plan_keys = set(self.register_via_plan(target, profile=["CCA-01"]))
        diverged = dict(self.register_via_cli(target, profile=["CCA-01"]))
        diverged.pop("cca_checklist_profile")
        cli_keys = set(diverged)
        message = _binder_message(plan_keys, cli_keys)
        with self.assertRaises(AssertionError):
            self.assertEqual(plan_keys, cli_keys, message)
        for token in ("register_task_from_plan", "cmd_issue_plan", "scripts/shiki_tasks.py", "cca_checklist_profile"):
            self.assertIn(token, message)


class TargetValidityTests(unittest.TestCase):
    def test_imported_script_modules_are_shipped(self) -> None:
        # The file must run from a freshly installed target, so every scripts
        # module it imports has to be in the shipped surface.
        from shiki_installer import TEMPLATE_PATHS

        shipped = set(TEMPLATE_PATHS)
        for module in IMPORTED_SCRIPT_MODULES:
            self.assertIn(
                f"scripts/{module}.py",
                shipped,
                f"{module} is imported by tests/test_plan_task_carry.py but is not shipped by "
                "shiki_installer.TEMPLATE_PATHS; the test could not run from a freshly installed target",
            )


if __name__ == "__main__":
    unittest.main()
