"""P2.3.1 — branch-protection payload generation for shiki_github.protect_branch.

protect_branch builds a GitHub branch-protection REST payload and posts it via
`gh api`. We assert the payload shape by capturing the JSON sent to the runner,
without performing any network call (the gh invocation is stubbed).
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_github
from shiki_process import ShikiError


class _RunRecorder:
    """Captures the args/input passed to the stubbed `run` helper."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[dict] = []

    def __call__(self, args, *, input_text=None, env=None, check=False, **kwargs):
        self.calls.append({"args": args, "input_text": input_text})
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="protection error")


class BranchProtectionPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_run = shiki_github.run
        self._orig_info = shiki_github.info
        shiki_github.info = lambda *a, **k: None

    def tearDown(self) -> None:
        shiki_github.run = self._orig_run
        shiki_github.info = self._orig_info

    def _captured_payload(self, review_count: int, checks: list[str]) -> dict:
        recorder = _RunRecorder(returncode=0)
        shiki_github.run = recorder
        shiki_github.protect_branch(
            "owner/name",
            "main",
            checks,
            review_count=review_count,
        )
        self.assertEqual(len(recorder.calls), 1)
        return json.loads(recorder.calls[0]["input_text"])

    def test_required_checks_are_strict_and_listed(self) -> None:
        payload = self._captured_payload(1, ["Validate Shiki mirror", "CCA verdict"])
        self.assertTrue(payload["required_status_checks"]["strict"])
        self.assertEqual(
            payload["required_status_checks"]["contexts"],
            ["Validate Shiki mirror", "CCA verdict"],
        )

    def test_review_count_drives_review_block(self) -> None:
        payload = self._captured_payload(1, [])
        reviews = payload["required_pull_request_reviews"]
        self.assertEqual(reviews["required_approving_review_count"], 1)
        self.assertTrue(reviews["require_code_owner_reviews"])
        self.assertTrue(reviews["dismiss_stale_reviews"])

    def test_zero_review_count_disables_code_owner_review(self) -> None:
        payload = self._captured_payload(0, [])
        reviews = payload["required_pull_request_reviews"]
        self.assertEqual(reviews["required_approving_review_count"], 0)
        self.assertFalse(reviews["require_code_owner_reviews"])

    def test_admin_enforcement_and_force_push_locked(self) -> None:
        payload = self._captured_payload(1, [])
        self.assertTrue(payload["enforce_admins"])
        self.assertFalse(payload["allow_force_pushes"])
        self.assertFalse(payload["allow_deletions"])
        self.assertTrue(payload["required_conversation_resolution"])

    def test_failed_protection_raises(self) -> None:
        shiki_github.run = _RunRecorder(returncode=1)
        with self.assertRaises(ShikiError):
            shiki_github.protect_branch("owner/name", "main", [], review_count=1)


if __name__ == "__main__":
    unittest.main()
