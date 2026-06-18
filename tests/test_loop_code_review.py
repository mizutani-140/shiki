"""Pre-PR independent code-review verifier (PRD 0002 T3, ADR 0011).

Before opening the PR the loop must run an INDEPENDENT read-only reviewer over
the task's diff (a `claude -p` invocation whose --allowedTools are restricted to
read tools and which has NO edit tools — distinct from the bypassPermissions
implementer adapter), parse its structured verdict deterministically, and either:

  * clean verdict  -> record a type:"check" "code-review" ledger, write the
    `## Pre-PR code review` PR-body section, and let create_pr proceed;
  * blocking verdict -> FAIL-CLOSE to stop_blocked (a blocking pre-PR review
    cannot anchor a repair packet — no PR exists yet at create_pr time);
  * dispatch/parse failure -> FAIL-CLOSE to stop_blocked (review not done is
    never silently passed).

These tests stub the reviewer binary (like test_shiki_runner_claude.sh stubs
`claude`) so no real model runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_loop
from shiki_github import github_pr_body
from shiki_process import ensure_control_dirs
from shiki_runtime_adapters import (
    REVIEWER_ADAPTER,
    CLAUDE_ADAPTER,
    parse_code_review_verdict,
)

TASK = "T-20260617T090739480720Z-04c7582b"
GOAL = "G-20260617T031753970001Z-1de3b322"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


class ReviewerAdapterTests(unittest.TestCase):
    def test_reviewer_is_read_only_and_distinct_from_implementer(self):
        # Confinement must be enforced by the ACTUAL restriction mechanisms, not
        # by --allowedTools (which only auto-approves; it does not remove tools).
        # Verify the contract by flag, reading each flag's value.
        argv = list(REVIEWER_ADAPTER.exec_argv)

        def flag_value(name):
            return argv[argv.index(name) + 1] if name in argv else None

        MUTATORS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

        # (1) --tools restricts the AVAILABLE built-in set to read tools only, so
        # the mutating tools do not exist in the reviewer's context.
        available = flag_value("--tools")
        self.assertIsNotNone(available, "reviewer must use --tools to restrict availability")
        available_set = {t.strip() for t in available.split(",")}
        self.assertIn("Read", available_set)
        for mutator in MUTATORS:
            self.assertNotIn(mutator, available_set, f"{mutator} must not be an available tool")

        # (2) --disallowedTools hard-denies the mutating built-ins AND all MCP
        # tools (mcp__*) — --tools restricts built-ins only, so MCP must be denied
        # separately or it escapes the read-only boundary (T3-RO-MCP-002).
        disallowed = flag_value("--disallowedTools")
        self.assertIsNotNone(disallowed, "reviewer must use --disallowedTools to deny mutators")
        disallowed_set = {t.strip() for t in disallowed.split(",")}
        for mutator in MUTATORS:
            self.assertIn(mutator, disallowed_set, f"{mutator} must be explicitly disallowed")
        self.assertIn("mcp__*", disallowed_set, "MCP tools must be denied (--tools does not affect MCP)")

        # (3) Hermetic to ambient MCP / user-project config: no MCP servers load
        # and ambient settings are ignored, so the environment cannot widen the
        # reviewer's read-only surface.
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(flag_value("--setting-sources"), "")

        # (4) Headless deny-by-default so an unmatched tool can never run.
        self.assertEqual(flag_value("--permission-mode"), "dontAsk")

        # (4) Structured-verdict contract; never the bypassPermissions implementer.
        self.assertIn("--json-schema", argv)
        self.assertNotIn("bypassPermissions", " ".join(argv))
        self.assertNotEqual(REVIEWER_ADAPTER.exec_argv, CLAUDE_ADAPTER.exec_argv)
        self.assertIn("bypassPermissions", " ".join(CLAUDE_ADAPTER.exec_argv))


class ParseVerdictTests(unittest.TestCase):
    def test_clean_verdict_parses(self):
        v = parse_code_review_verdict(json.dumps({"verdict": "clean", "findings": []}))
        self.assertEqual(v["verdict"], "clean")

    def test_blocking_verdict_parses(self):
        v = parse_code_review_verdict(
            json.dumps({"verdict": "blocking", "findings": [{"title": "bug"}]})
        )
        self.assertEqual(v["verdict"], "blocking")

    def test_verdict_embedded_in_surrounding_text_parses(self):
        # claude -p may wrap the JSON object in log lines; the parser must find it.
        raw = "starting review...\n" + json.dumps({"verdict": "clean", "findings": []}) + "\ndone\n"
        v = parse_code_review_verdict(raw)
        self.assertEqual(v["verdict"], "clean")

    def test_garbage_is_rejected(self):
        self.assertIsNone(parse_code_review_verdict("not json at all"))

    def test_missing_verdict_field_is_rejected(self):
        self.assertIsNone(parse_code_review_verdict(json.dumps({"findings": []})))

    def test_unknown_verdict_value_is_rejected(self):
        self.assertIsNone(parse_code_review_verdict(json.dumps({"verdict": "maybe"})))

    def test_empty_is_rejected(self):
        self.assertIsNone(parse_code_review_verdict(""))


class _Env:
    """A temp git repo + task-branch worktree with a diff to review."""

    def __init__(self, tmp: Path):
        self.target = tmp / "repo"
        self.target.mkdir()
        _git(self.target, "init", "-b", "main")
        _git(self.target, "config", "user.email", "t@t")
        _git(self.target, "config", "user.name", "t")
        (self.target / "README.md").write_text("x")
        _git(self.target, "add", "-A")
        _git(self.target, "commit", "-m", "init")
        ensure_control_dirs(self.target)
        self.branch = "shiki/t3-review-verifier"
        self.wt = tmp / "wt"
        _git(self.target, "worktree", "add", "-b", self.branch, str(self.wt))
        _git(self.wt, "config", "user.email", "t@t")
        _git(self.wt, "config", "user.name", "t")
        # the implementer left a committed change on the branch -> a real diff
        (self.wt / "feature.py").write_text("def f():\n    return 1\n")
        _git(self.wt, "add", "-A")
        _git(self.wt, "commit", "-m", "impl")
        _write(self.target / ".shiki" / "worktrees" / f"{TASK}.json",
               {"path": str(self.wt), "branch": self.branch})
        _write(self.target / ".shiki" / "tasks" / f"{TASK}.json",
               {"id": TASK, "goal_id": GOAL, "status": "review", "title": "t",
                "scope": "add the pre-PR code-review verifier", "expected_branch": self.branch,
                "ledger_evidence": []})


def _install_stub_reviewer(bindir: Path, verdict_json: str | None, *, returncode: int = 0):
    """Write a fake `claude` on PATH that emits a fixed verdict for the reviewer.

    The reviewer invocation carries --json-schema; the stub consumes the prompt
    on stdin and emits the configured verdict on stdout. (The implementer
    invocation is not exercised by these tests.)
    """
    fake = bindir / "claude"
    body = verdict_json if verdict_json is not None else ""
    script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'case "${1:-}" in\n'
        "  --version) echo '2.0.0 (Claude Code)'; exit 0 ;;\n"
        "  auth) echo '{\"loggedIn\": true}'; exit 0 ;;\n"
        "esac\n"
        "cat >/dev/null\n"
        f"cat <<'VERDICT'\n{body}\nVERDICT\n"
        f"exit {returncode}\n"
    )
    fake.write_text(script)
    fake.chmod(0o755)


def _with_path(bindir: Path):
    old = os.environ["PATH"]
    os.environ["PATH"] = f"{bindir}:{old}"
    return old


class PrePRCodeReviewTests(unittest.TestCase):
    def _run(self, env: _Env):
        return shiki_loop._run_pre_pr_code_review(env.target, TASK)

    def test_clean_verdict_records_ledger_and_pr_section(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            _install_stub_reviewer(bindir, json.dumps({"verdict": "clean", "findings": []}))
            old = _with_path(bindir)
            try:
                result = self._run(env)
            finally:
                os.environ["PATH"] = old
            self.assertEqual(result["status"], "clean")
            self.assertTrue(result.get("ledger_id"))
            # a type:"check" ledger naming "code-review" exists
            ledger = json.loads((env.target / ".shiki" / "ledger" / f"{result['ledger_id']}.json").read_text())
            self.assertEqual(ledger["type"], "check")
            self.assertIn("code-review", ledger["summary"].lower())
            # the verdict is recorded on the task so the PR body can render PR-12
            task = json.loads((env.target / ".shiki" / "tasks" / f"{TASK}.json").read_text())
            self.assertIn("## Pre-PR code review", github_pr_body(task))

    def test_blocking_verdict_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            _install_stub_reviewer(bindir, json.dumps({"verdict": "blocking", "findings": [{"title": "bug"}]}))
            old = _with_path(bindir)
            try:
                result = self._run(env)
            finally:
                os.environ["PATH"] = old
            self.assertEqual(result["status"], "blocking")

    def test_parse_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            _install_stub_reviewer(bindir, "garbage not json")
            old = _with_path(bindir)
            try:
                result = self._run(env)
            finally:
                os.environ["PATH"] = old
            self.assertEqual(result["status"], "fail")

    def test_dispatch_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            _install_stub_reviewer(bindir, json.dumps({"verdict": "clean"}), returncode=3)
            old = _with_path(bindir)
            try:
                result = self._run(env)
            finally:
                os.environ["PATH"] = old
            self.assertEqual(result["status"], "fail")


class ExecuteCreatePrGateTests(unittest.TestCase):
    """The create_pr action gates on the reviewer BEFORE commit/push/PR."""

    def _execute(self, env: _Env):
        decision = {"action": "create_pr", "task_id": TASK, "reason": "in review with no PR"}
        return shiki_loop.execute_action(env.target, GOAL, decision, repair_limit=3)

    def test_blocking_verdict_stops_create_pr_before_any_pr(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            _install_stub_reviewer(bindir, json.dumps({"verdict": "blocking", "findings": [{"title": "x"}]}))
            old = _with_path(bindir)
            try:
                result = self._execute(env)
            finally:
                os.environ["PATH"] = old
            self.assertEqual(result["action"], "stop_blocked")
            # never reached commit/push or PR creation
            self.assertNotIn("impl_commit", result)
            task = json.loads((env.target / ".shiki" / "tasks" / f"{TASK}.json").read_text())
            self.assertIsNone(task.get("expected_pr"))

    def test_parse_failure_stops_create_pr_before_any_pr(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            _install_stub_reviewer(bindir, "not json")
            old = _with_path(bindir)
            try:
                result = self._execute(env)
            finally:
                os.environ["PATH"] = old
            self.assertEqual(result["action"], "stop_blocked")
            self.assertNotIn("impl_commit", result)


if __name__ == "__main__":
    unittest.main()
