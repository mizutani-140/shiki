"""P0.3.2 — required checks derive from .shiki/config.yaml, not hard-coded CLI defaults.

`shiki_config.configured_required_checks` is the canonical config-first source used
by bootstrap/init to configure GitHub branch protection. These tests prove:
- a config change alters the derived checks (and the branch-protection payload contexts);
- the hard-coded DEFAULT_REQUIRED_CHECKS is used ONLY as a documented fallback when
  the target has no `mergegate.required_checks` (missing or empty);
- the `shiki init`/`bootstrap-platform` CLI no longer hard-codes the defaults.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import shiki_test_support  # noqa: F401  (path bootstrap)

import mergegate_check
import shiki_config
import shiki_github
from shiki_contracts import DEFAULT_REQUIRED_CHECKS


def _write_config(body: str) -> Path:
    tmp = Path(tempfile.mkdtemp())
    (tmp / ".shiki").mkdir(parents=True)
    (tmp / ".shiki" / "config.yaml").write_text(body, encoding="utf-8")
    return tmp


CUSTOM_CONFIG = """version: 1
mergegate:
  required_checks:
    - Validate Shiki mirror
    - Custom Project Check
    - CCA verdict
"""

EMPTY_CHECKS_CONFIG = """version: 1
mergegate:
  required_checks:
"""

# `required_checks` written as an inline scalar instead of a block list. The
# subset parser yields the bare string/bool, not a list.
SCALAR_CHECKS_CONFIG = """version: 1
mergegate:
  required_checks: CCA verdict
"""

BOOL_CHECKS_CONFIG = """version: 1
mergegate:
  required_checks: true
"""

# One rule, two independent copies. Each row pins the VALUE both must return --
# parity alone would not have caught the scalar defect, because both copies
# were wrong in exactly the same way.
REQUIRED_CHECKS_SHAPES = (
    ("block list", CUSTOM_CONFIG, ["Validate Shiki mirror", "Custom Project Check", "CCA verdict"]),
    ("no mergegate section", "version: 1\n", list(DEFAULT_REQUIRED_CHECKS)),
    ("empty required_checks", EMPTY_CHECKS_CONFIG, list(DEFAULT_REQUIRED_CHECKS)),
    ("inline scalar", SCALAR_CHECKS_CONFIG, list(DEFAULT_REQUIRED_CHECKS)),
    ("bare bool", BOOL_CHECKS_CONFIG, list(DEFAULT_REQUIRED_CHECKS)),
)


class ConfiguredRequiredChecksTests(unittest.TestCase):
    def test_config_overrides_hard_coded_default(self) -> None:
        target = _write_config(CUSTOM_CONFIG)
        result = shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
        self.assertEqual(result, ["Validate Shiki mirror", "Custom Project Check", "CCA verdict"])
        # The stale hard-coded default is NOT used when config defines checks.
        self.assertNotEqual(result, list(DEFAULT_REQUIRED_CHECKS))
        self.assertIn("Custom Project Check", result)

    def test_missing_config_uses_documented_fallback(self) -> None:
        target = Path(tempfile.mkdtemp())  # no .shiki/config.yaml
        result = shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
        self.assertEqual(result, list(DEFAULT_REQUIRED_CHECKS))

    def test_empty_required_checks_uses_documented_fallback(self) -> None:
        target = _write_config(EMPTY_CHECKS_CONFIG)
        result = shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
        self.assertEqual(result, list(DEFAULT_REQUIRED_CHECKS))

    def test_scalar_required_checks_uses_documented_fallback(self) -> None:
        """A non-list `required_checks` is absent, not a sequence of characters.

        `load_shiki_config` parses `required_checks: CCA verdict` as the string
        'CCA verdict'. Iterating it produced ten single-character check names,
        which bootstrap then wrote into branch protection and MergeGate then
        demanded of the PR. `validate_shiki` rejects the shape loudly; the
        consumers fall back to the documented default.
        """
        target = _write_config(SCALAR_CHECKS_CONFIG)
        result = shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
        self.assertEqual(result, list(DEFAULT_REQUIRED_CHECKS))
        self.assertNotIn("C", result)

    def test_returns_new_list_not_default_alias(self) -> None:
        target = Path(tempfile.mkdtemp())
        result = shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
        result.append("mutation")
        # Mutating the result must not corrupt the shared default tuple/list.
        self.assertNotIn("mutation", DEFAULT_REQUIRED_CHECKS)


class RequiredChecksCopiesAgreeTests(unittest.TestCase):
    """`shiki_config` and `mergegate_check` each ship a copy of one rule.

    They are read by different consumers -- bootstrap writes branch protection
    from the first, MergeGate blocks the merge on the second, and doctor reports
    on it -- so a divergence is silent and wrong in both directions. Every row
    asserts the expected VALUE, not just that the two agree.
    """

    def test_shiki_config_copy_matches_expected_value(self) -> None:
        for label, body, expected in REQUIRED_CHECKS_SHAPES:
            with self.subTest(shape=label):
                target = _write_config(body)
                self.assertEqual(
                    shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS),
                    expected,
                )

    def test_mergegate_copy_matches_expected_value(self) -> None:
        for label, body, expected in REQUIRED_CHECKS_SHAPES:
            with self.subTest(shape=label):
                target = _write_config(body)
                self.assertEqual(list(mergegate_check.configured_required_checks(target)), expected)

    def test_both_copies_agree(self) -> None:
        for label, body, _expected in REQUIRED_CHECKS_SHAPES:
            with self.subTest(shape=label):
                target = _write_config(body)
                self.assertEqual(
                    shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS),
                    list(mergegate_check.configured_required_checks(target)),
                )


class _RunRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, args, *, input_text=None, env=None, check=False, **kwargs):
        self.calls.append({"args": args, "input_text": input_text})
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class BranchProtectionContextsFollowConfigTests(unittest.TestCase):
    """The branch-protection payload contexts must reflect config, not the default."""

    def setUp(self) -> None:
        self._orig_run = shiki_github.run
        self._orig_info = shiki_github.info
        shiki_github.info = lambda *a, **k: None

    def tearDown(self) -> None:
        shiki_github.run = self._orig_run
        shiki_github.info = self._orig_info

    def _payload_contexts(self, checks: list[str]) -> list[str]:
        import json
        rec = _RunRecorder()
        shiki_github.run = rec
        shiki_github.protect_branch("owner/name", "main", checks, review_count=1)
        # The payload JSON is passed as --input - via input_text.
        call = rec.calls[-1]
        payload = json.loads(call["input_text"])
        return payload["required_status_checks"]["contexts"]

    def test_config_checks_flow_into_payload(self) -> None:
        target = _write_config(CUSTOM_CONFIG)
        derived = shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
        contexts = self._payload_contexts(derived)
        self.assertEqual(contexts, ["Validate Shiki mirror", "Custom Project Check", "CCA verdict"])
        self.assertIn("Custom Project Check", contexts)

    def test_fallback_checks_flow_into_payload_when_config_absent(self) -> None:
        target = Path(tempfile.mkdtemp())
        derived = shiki_config.configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
        contexts = self._payload_contexts(derived)
        self.assertEqual(contexts, list(DEFAULT_REQUIRED_CHECKS))


class CliNoLongerHardCodesDefaultsTests(unittest.TestCase):
    """`shiki init`/`bootstrap-platform` must not bake DEFAULT_REQUIRED_CHECKS into argparse."""

    def test_required_check_default_is_none(self) -> None:
        import shiki_cli
        parser = shiki_cli.build_parser()
        ns = parser.parse_args(["init", "/tmp/x", "--repo", "owner/name"])
        # Omitting --required-check must yield None so the handler derives from config.
        self.assertIsNone(ns.required_check)

    def test_explicit_required_check_is_collected(self) -> None:
        import shiki_cli
        parser = shiki_cli.build_parser()
        ns = parser.parse_args(["init", "/tmp/x", "--repo", "owner/name", "--required-check", "Only One"])
        self.assertEqual(ns.required_check, ["Only One"])


if __name__ == "__main__":
    unittest.main()
