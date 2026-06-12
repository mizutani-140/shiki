#!/usr/bin/env python3
"""Runtime adapter boundary for Shiki runner dispatch.

A RunnerAdapter binds a registry runtime name to the local tool, auth probe,
and headless execution command used by `shiki runner <adapter>`. The shared
runner machinery (worktree materialization, evidence recording, task status
transitions) lives in shiki_runtime and is runtime-agnostic; adding a runtime
means adding one adapter here plus a registry role grant.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable

from shiki_process import ROOT, ShikiError, first_line, run


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def combined_output(probe: dict[str, Any]) -> str:
    return "\n".join(
        part
        for part in [str(probe.get("stdout", "")).strip(), str(probe.get("stderr", "")).strip()]
        if part
    )


def command_probe(name: str, args: list[str]) -> dict[str, Any]:
    if not command_exists(name):
        return {
            "installed": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    result = run([name, *args], cwd=ROOT, check=False)
    return {
        "installed": True,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def claude_auth_status() -> dict[str, Any]:
    version = command_probe("claude", ["--version"])
    auth = command_probe("claude", ["auth", "status"])
    logged_in = False
    auth_method = "unknown"
    api_provider = "unknown"

    if auth["stdout"]:
        try:
            data = json.loads(auth["stdout"])
            logged_in = bool(data.get("loggedIn"))
            auth_method = str(data.get("authMethod", "unknown"))
            api_provider = str(data.get("apiProvider", "unknown"))
        except json.JSONDecodeError:
            logged_in = auth["returncode"] == 0
    elif auth["returncode"] == 0:
        logged_in = True

    ready = bool(version["installed"] and logged_in)
    blocking = []
    if not version["installed"]:
        blocking.append("Claude Code CLI is not installed.")
    elif not logged_in:
        blocking.append("Claude Code is not authenticated; /shiki cannot run inside Claude Code until Claude Code login succeeds.")

    return {
        "installed": version["installed"],
        "version": first_line(version["stdout"]),
        "logged_in": logged_in,
        "auth_method": auth_method,
        "api_provider": api_provider,
        "ready": ready,
        "blocking_reasons": blocking,
        "remediation": "Run `claude auth login` in a terminal or `/login` inside Claude Code, then rerun `/shiki`." if blocking else "",
    }


def codex_auth_status() -> dict[str, Any]:
    version = command_probe("codex", ["--version"])
    auth = command_probe("codex", ["login", "status"])
    logged_in = auth["returncode"] == 0 and "logged in" in combined_output(auth).lower()
    ready = bool(version["installed"] and logged_in)
    blocking = []
    if not version["installed"]:
        blocking.append("Codex CLI is not installed.")
    elif not logged_in:
        blocking.append("Codex CLI is not authenticated.")

    return {
        "installed": version["installed"],
        "version": first_line(combined_output(version)),
        "logged_in": logged_in,
        "ready": ready,
        "blocking_reasons": blocking,
        "remediation": "Run `codex login` or sign in to Codex App before using the Codex entrypoint." if blocking else "",
    }


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class RunnerAdapter:
    name: str
    display_name: str
    required_tool: str
    exec_argv: tuple[str, ...]
    auth_status: Callable[[], dict[str, Any]]
    auth_remediation: str

    def command_label(self, handoff_ref: str) -> str:
        return f"{' '.join(self.exec_argv)} <{handoff_ref}>"

    def execute(self, cwd: Path, prompt: str) -> ExecResult:
        process = subprocess.run(
            list(self.exec_argv),
            cwd=str(cwd),
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
        )
        return ExecResult(process.returncode, process.stdout, process.stderr)


CODEX_ADAPTER = RunnerAdapter(
    name="codex",
    display_name="Codex CLI",
    required_tool="codex",
    exec_argv=("codex", "exec", "-"),
    auth_status=codex_auth_status,
    auth_remediation="Run `codex login` or sign in to Codex App before dispatch.",
)

CLAUDE_ADAPTER = RunnerAdapter(
    name="claude-code",
    display_name="Claude Code",
    required_tool="claude",
    exec_argv=("claude", "-p", "--permission-mode", "bypassPermissions"),
    auth_status=claude_auth_status,
    auth_remediation="Run `claude auth login` in a terminal or `/login` inside Claude Code before dispatch.",
)


RUNNER_ADAPTERS: dict[str, RunnerAdapter] = {
    adapter.name: adapter for adapter in (CODEX_ADAPTER, CLAUDE_ADAPTER)
}


def runner_adapter_names() -> tuple[str, ...]:
    return tuple(sorted(RUNNER_ADAPTERS))


def get_runner_adapter(name: str) -> RunnerAdapter:
    try:
        return RUNNER_ADAPTERS[name]
    except KeyError as error:
        known = ", ".join(runner_adapter_names())
        raise ShikiError(f"no runner adapter for runtime {name!r}; known adapters: {known}") from error
