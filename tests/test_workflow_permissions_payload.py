"""Workflow-permissions payload generation for shiki_github.configure_workflow_permissions.

configure_workflow_permissions builds the GitHub Actions workflow-permissions
REST payload and posts it via `gh api ... -X PUT --input -` (mirroring
protect_branch). We assert the PUT path and payload shape by capturing the JSON
sent to the runner, without performing any network call (the gh invocation is
stubbed). The CCA Review Bridge depends on can_approve_pull_request_reviews, so
the default payload is asserted as default=read + can-approve=true, and a gh
failure must warn (not raise) because branch protection is the hard gate.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_github


class _RunRecorder:
    """Captures the args/input passed to the stubbed `run` helper."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[dict] = []

    def __call__(self, args, *, input_text=None, env=None, check=False, **kwargs):
        self.calls.append({"args": args, "input_text": input_text})
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="workflow permission error")


class WorkflowPermissionsPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_run = shiki_github.run
        self._orig_info = shiki_github.info
        self._orig_warn = shiki_github.warn
        shiki_github.info = lambda *a, **k: None
        shiki_github.warn = lambda *a, **k: None

    def tearDown(self) -> None:
        shiki_github.run = self._orig_run
        shiki_github.info = self._orig_info
        shiki_github.warn = self._orig_warn

    def _capture(self, *, returncode: int = 0, **kwargs) -> _RunRecorder:
        recorder = _RunRecorder(returncode=returncode)
        shiki_github.run = recorder
        shiki_github.configure_workflow_permissions("owner/name", **kwargs)
        return recorder

    def test_puts_workflow_permissions_path(self) -> None:
        recorder = self._capture()
        self.assertEqual(len(recorder.calls), 1)
        args = recorder.calls[0]["args"]
        self.assertEqual(args[0], "gh")
        self.assertEqual(args[1], "api")
        self.assertEqual(args[2], "repos/owner/name/actions/permissions/workflow")
        self.assertIn("-X", args)
        self.assertEqual(args[args.index("-X") + 1], "PUT")
        self.assertIn("--input", args)
        self.assertEqual(args[args.index("--input") + 1], "-")

    def test_default_payload_is_read_and_can_approve(self) -> None:
        recorder = self._capture()
        payload = json.loads(recorder.calls[0]["input_text"])
        self.assertEqual(payload["default_workflow_permissions"], "read")
        self.assertIs(payload["can_approve_pull_request_reviews"], True)

    def test_overrides_are_honored(self) -> None:
        recorder = self._capture(can_approve_pull_requests=False, default_permissions="write")
        payload = json.loads(recorder.calls[0]["input_text"])
        self.assertEqual(payload["default_workflow_permissions"], "write")
        self.assertIs(payload["can_approve_pull_request_reviews"], False)

    def test_failure_warns_not_raises(self) -> None:
        # A non-zero gh exit must warn-not-fail: branch protection is the hard
        # gate; workflow permissions are a convenience the operator can also set.
        warnings: list[str] = []
        shiki_github.warn = lambda message, *a, **k: warnings.append(message)
        recorder = self._capture(returncode=1)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("workflow permission", warnings[0].lower())


if __name__ == "__main__":
    unittest.main()
