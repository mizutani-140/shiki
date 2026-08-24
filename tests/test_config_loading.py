"""P2.3.1 — config loading behavior for shiki_config and mergegate_check.

Both modules ship an independent dependency-free parser for the small
``.shiki/config.yaml`` subset. These tests assert that both parsers agree on
real config content and handle the documented edge cases.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_config
import mergegate_check


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


if __name__ == "__main__":
    unittest.main()
