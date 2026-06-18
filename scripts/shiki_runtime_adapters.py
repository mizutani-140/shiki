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

# The independent pre-PR code-review verifier (ADR 0011). It is the SAME model as
# the implementer but in a SEPARATE context with HARD read-only confinement — the
# independence IS the context boundary, exactly as for CCA. It is NEVER the
# bypassPermissions implementer. A reviewer that cannot mutate the tree cannot
# forge a "0 findings" by editing the implementation or the evidence.
#
# Confinement is enforced by the documented restriction mechanisms, NOT by
# --allowedTools (which only auto-APPROVES; it does not remove tools):
#   --tools           restricts the AVAILABLE built-in set to read tools only, so
#                     Edit/Write/MultiEdit/NotebookEdit do not exist in context.
#                     NOTE: --tools restricts BUILT-IN tools only; MCP tools are
#                     unaffected and must be denied separately (below).
#   --disallowedTools belt-and-suspenders: hard-removes the mutating built-ins
#                     AND every MCP tool (mcp__*), so no ambient MCP/user/managed
#                     server surface escapes the read-only boundary.
#   --strict-mcp-config  load MCP servers ONLY from --mcp-config; with none
#                     passed, NO MCP servers load — the reviewer is hermetic to
#                     ambient MCP configuration.
#   --setting-sources ""  ignore user/project/local settings so the reviewer is
#                     independent of ambient customizations (allowed tools, hooks).
#   --permission-mode dontAsk: in headless -p mode (no interactive approver) any
#                     unmatched tool is denied, never prompted/hung.
#   --allowedTools    auto-approves the read ops so the review does not stall.
CODE_REVIEW_AVAILABLE_TOOLS = "Read,Grep,Glob,Bash"
# Hard-deny the mutating built-ins AND all MCP tools (mcp__* — T3-RO-MCP-002).
# --tools above is the PRIMARY mechanism: it is an allowlist, so any built-in not
# named there (every current OR future mutator) is already unavailable. This deny
# list is belt-and-suspenders and must therefore name only mutators the runtime
# actually recognizes — a deny rule for an unknown name is a no-op that emits a
# spurious "matches no known tool" warning on stderr. `MultiEdit` is not a known
# tool name in headless `claude -p`, so it is intentionally omitted here; the
# allowlist still bars it. (T3-RO-DENYNAME-003.)
CODE_REVIEW_DISALLOWED_TOOLS = "Edit,Write,NotebookEdit,mcp__*"
CODE_REVIEW_ALLOWED_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git status:*)"

# Minimal structured-verdict contract. `verdict` is the only field the loop gates
# on; `findings`/`summary` carry the (non-load-bearing) detail rendered into the
# PR-12 body section.
CODE_REVIEW_VERDICT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["clean", "blocking"]},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "severity": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                    "required": ["title"],
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["verdict"],
    },
    separators=(",", ":"),
)

CODE_REVIEW_VALID_VERDICTS = ("clean", "blocking")

REVIEWER_ADAPTER = RunnerAdapter(
    name="claude-code-reviewer",
    display_name="Claude Code reviewer (read-only)",
    required_tool="claude",
    exec_argv=(
        "claude",
        "-p",
        # Hard read-only confinement (see CODE_REVIEW_* notes above): --tools
        # restricts built-in availability; --disallowedTools removes the mutating
        # built-ins AND all MCP tools (mcp__*); --strict-mcp-config with no
        # --mcp-config loads zero MCP servers; --setting-sources "" ignores
        # ambient user/project settings; --permission-mode dontAsk denies anything
        # unmatched in headless mode; --allowedTools only auto-approves read ops.
        "--tools",
        CODE_REVIEW_AVAILABLE_TOOLS,
        "--disallowedTools",
        CODE_REVIEW_DISALLOWED_TOOLS,
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        CODE_REVIEW_ALLOWED_TOOLS,
        # Structured-verdict contract. --json-schema validates the model's
        # structured output against the schema; --output-format json is REQUIRED
        # for that validated object to be emitted — it surfaces in the result
        # envelope's `structured_output` field. Without --output-format json the
        # run returns prose (the `result` text), which carries no parseable
        # verdict and forces the loop to fail closed. (T3-JSON-OUT-004.)
        "--json-schema",
        CODE_REVIEW_VERDICT_SCHEMA,
        "--output-format",
        "json",
    ),
    auth_status=claude_auth_status,
    auth_remediation="Run `claude auth login` in a terminal or `/login` inside Claude Code before dispatch.",
)


def _valid_verdict(obj: Any) -> dict[str, Any] | None:
    """Return ``obj`` iff it is a dict carrying a recognized ``verdict``."""
    if isinstance(obj, dict):
        verdict = obj.get("verdict")
        if isinstance(verdict, str) and verdict in CODE_REVIEW_VALID_VERDICTS:
            return obj
    return None


def parse_code_review_verdict(stdout: str) -> dict[str, Any] | None:
    """Deterministically extract the reviewer's structured verdict.

    The reviewer runs ``claude -p --json-schema ... --output-format json``, whose
    single result envelope carries the schema-validated object in its
    ``structured_output`` field (documented contract — not prose). We read that
    field directly. A result envelope only carries a trustworthy verdict when
    ``subtype == "success"``; any other subtype (e.g.
    ``error_max_structured_output_retries``) is fail-closed.

    Returns the verdict dict, or ``None`` on ANY deviation — the caller treats
    ``None`` as fail-closed (review-not-proven -> block), never a silent pass.
    """
    text = (stdout or "").strip()
    if not text:
        return None

    # Primary contract: the --output-format json result envelope.
    try:
        envelope = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        envelope = None
    if isinstance(envelope, dict):
        if envelope.get("type") == "result" and envelope.get("subtype") != "success":
            return None
        structured = _valid_verdict(envelope.get("structured_output"))
        if structured is not None:
            return structured
        # Bare schema object emitted without the envelope is also accepted.
        bare = _valid_verdict(envelope)
        if bare is not None:
            return bare

    # Fallback: a bare verdict object prefixed by non-JSON log lines. (Kept for
    # robustness; the primary path covers the documented --output-format json
    # contract.)
    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = _valid_verdict(json.loads(text[start : index + 1]))
                    except (json.JSONDecodeError, ValueError):
                        candidate = None
                    if candidate is not None:
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


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
