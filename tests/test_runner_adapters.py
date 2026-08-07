"""Runtime adapter boundary contract (SADR-0008).

Asserts that every runner adapter binds to a registered runtime with the
runner role, that the codex and claude-code adapters exist with coherent
tool/argv contracts, and that unknown adapters fail closed.
"""

from __future__ import annotations

import unittest

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_process import ShikiError
from shiki_runtime_adapters import (
    CLAUDE_ADAPTER,
    CODEX_ADAPTER,
    RUNNER_ADAPTERS,
    get_runner_adapter,
    runner_adapter_names,
)
from shiki_runtime_registry import get_runtime


class RunnerAdapterContractTests(unittest.TestCase):
    def test_adapter_names_are_registered_runtimes_with_runner_role(self) -> None:
        for name, adapter in RUNNER_ADAPTERS.items():
            descriptor = get_runtime(name)
            self.assertEqual(adapter.name, name)
            self.assertIn("runner", descriptor.roles)
            self.assertIn("implementer", descriptor.roles)
            self.assertIn(adapter.required_tool, descriptor.required_tools)

    def test_codex_and_claude_adapters_exist(self) -> None:
        self.assertEqual(runner_adapter_names(), ("claude-code", "codex"))
        self.assertIs(get_runner_adapter("codex"), CODEX_ADAPTER)
        self.assertIs(get_runner_adapter("claude-code"), CLAUDE_ADAPTER)

    def test_exec_argv_starts_with_required_tool(self) -> None:
        for adapter in RUNNER_ADAPTERS.values():
            self.assertTrue(adapter.exec_argv)
            self.assertEqual(adapter.exec_argv[0], adapter.required_tool)

    def test_claude_adapter_runs_headless(self) -> None:
        self.assertIn("-p", CLAUDE_ADAPTER.exec_argv)

    def test_command_label_records_handoff_reference(self) -> None:
        label = CLAUDE_ADAPTER.command_label(".shiki/handoffs/T-0001-task.md")
        self.assertIn("claude -p", label)
        self.assertIn(".shiki/handoffs/T-0001-task.md", label)
        self.assertIn("codex exec", CODEX_ADAPTER.command_label("h.md"))

    def test_unknown_adapter_fails_closed(self) -> None:
        with self.assertRaises(ShikiError):
            get_runner_adapter("hermes-runner")

    def test_auth_status_callables_are_bound(self) -> None:
        for adapter in RUNNER_ADAPTERS.values():
            self.assertTrue(callable(adapter.auth_status))
            self.assertTrue(adapter.auth_remediation)


if __name__ == "__main__":
    unittest.main()
