"""P2.3.1 — config loading behavior for shiki_config and mergegate_check.

Both modules ship an independent dependency-free parser for the small
``.shiki/config.yaml`` subset. These tests assert that both parsers agree on
real config content and handle the documented edge cases.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_config
import shiki_doctor
import mergegate_check
from shiki_contracts import DEFAULT_REQUIRED_CHECKS


CONFIG_SAMPLE = """version: 1
platform: shiki
defaults:
  branch_prefix: shiki
  automatic_repair_limit: 3
  required_review: true
guardian:
  users:
    - mizutani-140
mergegate:
  required_checks:
    - Validate Shiki mirror
    - CCA verdict
"""


class ConfigLoadingTests(unittest.TestCase):
    def _write_config(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        shiki_dir = tmp / ".shiki"
        shiki_dir.mkdir(parents=True)
        (shiki_dir / "config.yaml").write_text(body, encoding="utf-8")
        return tmp

    def test_scalar_parsing_coerces_booleans(self) -> None:
        self.assertIs(shiki_config.parse_config_scalar("true"), True)
        self.assertIs(shiki_config.parse_config_scalar("false"), False)
        self.assertEqual(shiki_config.parse_config_scalar("'shiki'"), "shiki")
        self.assertEqual(shiki_config.parse_config_scalar("  3 "), "3")

    def test_section_scalar_and_list_parsing(self) -> None:
        target = self._write_config(CONFIG_SAMPLE)
        config = shiki_config.load_shiki_config(target)
        # The parser models the small subset Shiki owns: it keeps section-scoped
        # scalars and lists. Top-level bare scalars (e.g. ``platform: shiki``)
        # are intentionally not captured.
        self.assertNotIn("platform", config)
        self.assertIs(config["defaults"]["required_review"], True)
        self.assertEqual(config["defaults"]["branch_prefix"], "shiki")
        self.assertEqual(config["guardian"]["users"], ["mizutani-140"])
        self.assertEqual(
            config["mergegate"]["required_checks"],
            ["Validate Shiki mirror", "CCA verdict"],
        )

    def test_missing_config_returns_empty(self) -> None:
        target = Path(tempfile.mkdtemp())
        self.assertEqual(shiki_config.load_shiki_config(target), {})

    def test_comments_and_blank_lines_ignored(self) -> None:
        body = "# a comment\n\ndefaults:\n  # inner comment\n  required_review: false\n"
        target = self._write_config(body)
        config = shiki_config.load_shiki_config(target)
        self.assertIs(config["defaults"]["required_review"], False)

    def test_both_parsers_agree(self) -> None:
        target = self._write_config(CONFIG_SAMPLE)
        self.assertEqual(
            shiki_config.load_shiki_config(target),
            mergegate_check.load_shiki_config(target),
        )

    def test_configured_required_review_defaults_true(self) -> None:
        target = Path(tempfile.mkdtemp())
        self.assertTrue(shiki_config.configured_required_review(target))
        self.assertEqual(shiki_config.branch_protection_review_count(target), 1)

    def test_configured_required_review_honors_false(self) -> None:
        target = self._write_config("defaults:\n  required_review: false\n")
        self.assertFalse(shiki_config.configured_required_review(target))
        self.assertEqual(shiki_config.branch_protection_review_count(target), 0)
        self.assertFalse(mergegate_check.configured_required_review(target))

    def test_code_owner_review_defaults_false(self) -> None:
        """Absent key means GitHub-level code-owner enforcement is OFF.

        The default is false because the CCA Review Bridge is the intended
        approver (SADR-0013) and a bot can never be a CODEOWNER, so enforcing
        it makes every CODEOWNERS-touching PR unmergeable in a repository whose
        only code owner is the PR author (SADR-0021).
        """
        target = Path(tempfile.mkdtemp())
        self.assertFalse(shiki_config.configured_required_code_owner_review(target))
        target = self._write_config("defaults:\n  required_review: true\n")
        self.assertFalse(shiki_config.configured_required_code_owner_review(target))

    def test_code_owner_review_honors_true(self) -> None:
        target = self._write_config(
            "defaults:\n  required_review: true\n  required_code_owner_review: true\n"
        )
        self.assertTrue(shiki_config.configured_required_code_owner_review(target))

    def test_code_owner_review_honors_explicit_false(self) -> None:
        target = self._write_config(
            "defaults:\n  required_review: true\n  required_code_owner_review: false\n"
        )
        self.assertFalse(shiki_config.configured_required_code_owner_review(target))

    def test_code_owner_review_requires_review_to_be_required(self) -> None:
        """required_review: false wins: GitHub still demands code-owner approval
        at required_approving_review_count 0, which would be a deadlock with no
        approving-review requirement to justify it."""
        target = self._write_config(
            "defaults:\n  required_review: false\n  required_code_owner_review: true\n"
        )
        self.assertFalse(shiki_config.configured_required_code_owner_review(target))

    def test_mergegate_required_checks_fall_back_to_defaults(self) -> None:
        target = self._write_config("platform: shiki\n")
        checks = mergegate_check.configured_required_checks(target)
        self.assertEqual(list(checks), list(mergegate_check.DEFAULT_REQUIRED_CHECKS))


class DoctorReadsPolicyThroughConfigTests(unittest.TestCase):
    """Doctor must judge branch protection against the SAME values that configure it.

    Doctor used to restate shiki_config's rules over its own parser
    (``_required_review``, ``_required_checks``, ``_required_code_owner_review``).
    Every divergence failed OPEN: a config relying on the documented defaults made
    doctor compare protection against ``[]`` required checks and ``required_review
    = False``, so it reported "matches Shiki required checks/review policy" for a
    branch with no protection at all -- while bootstrap had protected it with four
    contexts and one approving review, and MergeGate blocked merges without a review.

    These tests pin the behaviour, not the helpers, so the mirrors cannot come back
    by a different name.
    """

    UNPROTECTED = {
        "required_status_checks": {"contexts": []},
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
            "require_code_owner_reviews": False,
        },
    }

    def _target(self, config_body: str) -> Path:
        target = Path(tempfile.mkdtemp())
        (target / ".shiki").mkdir(parents=True)
        (target / ".shiki" / "config.yaml").write_text(config_body, encoding="utf-8")
        return target

    def _branch_protection_finding(self, target: Path, protection: dict):
        """Run doctor's online checks against a stubbed `gh`, return the BP finding."""
        original_gh = shiki_doctor._gh
        original_which = shiki_doctor.shutil.which

        def fake_gh(args, config):
            stdout = ""
            if args[:1] == ["api"] and args[1].endswith("/protection"):
                stdout = json.dumps(protection)
            elif args[:1] == ["api"] and "permissions/workflow" in args[1]:
                stdout = json.dumps(
                    {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True}
                )
            elif args[:1] == ["api"]:
                stdout = "[]"
            elif args[:2] == ["repo", "view"]:
                stdout = json.dumps({"defaultBranchRef": {"name": "main"}})
            elif args[:2] == ["secret", "list"]:
                stdout = "CLAUDE_CODE_OAUTH_TOKEN\t2026"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        shiki_doctor._gh = fake_gh
        shiki_doctor.shutil.which = lambda name: "/usr/bin/gh"
        try:
            provider = shiki_doctor.provider_from_repo_json(
                {"provider": "github", "repo": "owner/name", "host": "github.com"}
            )
            findings = shiki_doctor._online_findings(provider, target)
        finally:
            shiki_doctor._gh = original_gh
            shiki_doctor.shutil.which = original_which
        return next(f for f in findings if f.id == "doctor.github.branch_protection")

    def test_absent_config_keys_still_judge_protection(self) -> None:
        """The regression: a config relying on BOTH documented defaults.

        Absent ``defaults.required_review`` means review IS required, and an absent
        ``mergegate.required_checks`` means the four DEFAULT_REQUIRED_CHECKS. Doctor
        must fail an unprotected branch on both counts, not pass it.
        """
        target = self._target("version: 1\nplatform: shiki\n")
        finding = self._branch_protection_finding(target, self.UNPROTECTED)
        self.assertEqual(finding.status, "fail")
        self.assertEqual(
            finding.details["required_checks"],
            list(shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)),
        )
        failures = " | ".join(finding.details["failures"])
        self.assertIn("missing required checks", failures)
        self.assertIn("required review count is less than 1", failures)

    def test_empty_required_checks_section_falls_back_to_defaults(self) -> None:
        """A present-but-empty list is the same as absent for every other reader."""
        target = self._target("mergegate:\n  required_checks:\n")
        finding = self._branch_protection_finding(target, self.UNPROTECTED)
        self.assertEqual(
            finding.details["required_checks"],
            list(DEFAULT_REQUIRED_CHECKS),
        )

    def test_explicit_required_review_false_is_still_honoured(self) -> None:
        """Fixing the default must not break the opt-out: false still means false."""
        target = self._target("defaults:\n  required_review: false\n")
        finding = self._branch_protection_finding(target, self.UNPROTECTED)
        failures = " | ".join(finding.details["failures"])
        self.assertNotIn("required review count", failures)

    def test_code_owner_opt_in_readable_without_explicit_required_review(self) -> None:
        """``required_code_owner_review: true`` was unreadable when required_review
        was absent, because doctor's mirror read the absent key as false and the
        precedence rule then forced code-owner review off."""
        target = self._target("defaults:\n  required_code_owner_review: true\n")
        self.assertTrue(shiki_config.configured_required_code_owner_review(target))
        finding = self._branch_protection_finding(target, self.UNPROTECTED)
        self.assertTrue(finding.details["required_code_owner_review"])

    def test_doctor_keeps_no_private_copy_of_the_rules(self) -> None:
        """Structural guard: the mirrors must not return under any name."""
        for name in ("_required_review", "_required_checks", "_required_code_owner_review"):
            self.assertFalse(
                hasattr(shiki_doctor, name),
                f"shiki_doctor.{name} is a private restatement of a shiki_config rule; "
                "read the configured_* helper instead so the two cannot diverge.",
            )


if __name__ == "__main__":
    unittest.main()
