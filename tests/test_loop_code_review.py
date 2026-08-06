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
import shlex
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


def _rev(cwd, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


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
        # NO mutating tool (current or future) exists in the reviewer's context.
        # This allowlist is the primary confinement mechanism.
        available = flag_value("--tools")
        self.assertIsNotNone(available, "reviewer must use --tools to restrict availability")
        available_set = {t.strip() for t in available.split(",")}
        self.assertIn("Read", available_set)
        for mutator in MUTATORS:
            self.assertNotIn(mutator, available_set, f"{mutator} must not be an available tool")

        # (2) --disallowedTools hard-denies the mutating built-ins AND all MCP
        # tools (mcp__*) — --tools restricts built-ins only, so MCP must be denied
        # separately or it escapes the read-only boundary (T3-RO-MCP-002). The
        # deny list names only mutators the runtime recognizes; a deny rule for an
        # unknown name is a no-op that warns "matches no known tool" on stderr, so
        # `MultiEdit` (not a known headless tool) is intentionally NOT denied here
        # — the allowlist in (1) still bars it (T3-RO-DENYNAME-003).
        disallowed = flag_value("--disallowedTools")
        self.assertIsNotNone(disallowed, "reviewer must use --disallowedTools to deny mutators")
        disallowed_set = {t.strip() for t in disallowed.split(",")}
        for mutator in ("Edit", "Write", "NotebookEdit"):
            self.assertIn(mutator, disallowed_set, f"{mutator} must be explicitly disallowed")
        self.assertNotIn(
            "MultiEdit",
            disallowed_set,
            "MultiEdit is not a known headless tool; denying it only emits a spurious warning",
        )
        self.assertIn("mcp__*", disallowed_set, "MCP tools must be denied (--tools does not affect MCP)")

        # (3) Hermetic to ambient MCP / user-project config: no MCP servers load
        # and ambient settings are ignored, so the environment cannot widen the
        # reviewer's read-only surface.
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(flag_value("--setting-sources"), "")

        # (4) Headless deny-by-default so an unmatched tool can never run.
        self.assertEqual(flag_value("--permission-mode"), "dontAsk")

        # (4) Structured-verdict contract; never the bypassPermissions implementer.
        # --json-schema validates the structured output; --output-format json is
        # REQUIRED for that validated object to be emitted (in the result
        # envelope's structured_output field) instead of prose (T3-JSON-OUT-004).
        self.assertIn("--json-schema", argv)
        self.assertEqual(flag_value("--output-format"), "json")
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

    def test_output_format_json_envelope_structured_output_parses(self):
        # The documented --output-format json contract: the validated object is in
        # the result envelope's structured_output field, NOT the prose `result`.
        envelope = {
            "type": "result",
            "subtype": "success",
            "result": "**Verdict: clean** — the test is trivial but passes.",
            "structured_output": {"verdict": "clean", "findings": []},
        }
        v = parse_code_review_verdict(json.dumps(envelope))
        self.assertIsNotNone(v)
        self.assertEqual(v["verdict"], "clean")

    def test_envelope_blocking_structured_output_parses(self):
        envelope = {
            "type": "result",
            "subtype": "success",
            "result": "prose...",
            "structured_output": {"verdict": "blocking", "findings": [{"title": "bug"}]},
        }
        v = parse_code_review_verdict(json.dumps(envelope))
        self.assertEqual(v["verdict"], "blocking")

    def test_non_success_subtype_is_failclosed(self):
        # A non-success result (e.g. the model never produced schema output) must
        # fail closed even if a stale structured_output is present.
        envelope = {
            "type": "result",
            "subtype": "error_max_structured_output_retries",
            "structured_output": {"verdict": "clean"},
        }
        self.assertIsNone(parse_code_review_verdict(json.dumps(envelope)))

    def test_prose_result_without_structured_output_is_failclosed(self):
        # The pre-fix failure mode: prose `result`, no structured_output -> block.
        envelope = {
            "type": "result",
            "subtype": "success",
            "result": "**Verdict: clean** — looks fine to me.",
        }
        self.assertIsNone(parse_code_review_verdict(json.dumps(envelope)))

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


def _install_capturing_reviewer(bindir: Path, capture_path: Path, verdict_json: str):
    """A fake `claude` that saves the review prompt (diff and all) to a file.

    Identical to ``_install_stub_reviewer`` for the verdict it emits, but the
    review invocation tees stdin — the full prompt, whose tail is the git diff the
    loop handed the reviewer — to ``capture_path`` so a test can assert exactly
    what the reviewer was shown, then emits the configured clean/blocking verdict.
    """
    fake = bindir / "claude"
    script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'case "${1:-}" in\n'
        "  --version) echo '2.0.0 (Claude Code)'; exit 0 ;;\n"
        "  auth) echo '{\"loggedIn\": true}'; exit 0 ;;\n"
        "esac\n"
        f"cat >{shlex.quote(str(capture_path))}\n"
        f"cat <<'VERDICT'\n{verdict_json}\nVERDICT\n"
    )
    fake.write_text(script)
    fake.chmod(0o755)


def _diff_file_count(diff_text: str) -> int:
    """Number of files a unified diff touches (one ``diff --git`` header each)."""
    return diff_text.count("diff --git ")


def _repo_with_local_default_behind_remote(tmp: Path, *, bulk_files: int = 60):
    """A repo whose local default branch (and tracking ref) LAG the remote tip.

    Models the drift the loop hit live: after other goals merge to ``origin/main``
    a coordinator's local ``main`` and its ``origin/main`` tracking ref both sit
    behind the remote until a fetch. A large already-merged history (``bulk/``)
    lands on the remote, then the local branch and tracking ref are pinned back to
    the stale tip. Returns ``(target, stale_tip, remote_tip)`` with the coordinator
    parked on a feature branch.
    """
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    target = tmp / "repo"
    target.mkdir()
    _git(target, "init", "-b", "main")
    _git(target, "config", "user.email", "t@t")
    _git(target, "config", "user.name", "t")
    (target / "README.md").write_text("base\n")
    _git(target, "add", "-A")
    _git(target, "commit", "-m", "init on main")
    _git(target, "remote", "add", "origin", str(remote))
    _git(target, "push", "-u", "origin", "main")
    # Pin origin/HEAD -> origin/main (as a real clone would) so the shared default
    # branch resolver names `main` without consulting the operator's config.
    _git(target, "remote", "set-head", "origin", "main")
    stale_tip = _rev(target, "main")

    # A large history merges to the remote. Committing locally then pushing
    # advances both local main and origin; they are pinned back below.
    bulk = target / "bulk"
    bulk.mkdir()
    for i in range(bulk_files):
        (bulk / f"f{i:03d}.txt").write_text("merged history line\n" * 40)
    _git(target, "add", "-A")
    _git(target, "commit", "-m", "bulk merged history")
    _git(target, "push", "origin", "main")
    remote_tip = _rev(target, "main")

    # Coordinator parks on a feature branch; local main and the tracking ref are
    # both pinned back behind the remote tip (pre-fetch drift).
    _git(target, "checkout", "-b", "feature")
    _git(target, "branch", "-f", "main", stale_tip)
    _git(target, "update-ref", "refs/remotes/origin/main", stale_tip)
    return target, stale_tip, remote_tip


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


class ReviewerDiffBaseTests(unittest.TestCase):
    """The reviewer's diff is cut from the origin-resolved default, not a stale
    local ``main`` (T-20260805T023333275527Z-fff69d84).

    A coordinator's local default branch silently lags ``origin`` after other
    goals merge; diffing the task branch against that stale local ref hands the
    reviewer the entire already-merged history instead of the task's own change.
    Measured live 2026-08-03 (local ``main`` 151 commits behind): ``git diff
    --cached main`` -> 2,904,002 bytes / 697 files, versus 47,899 bytes / 4 files
    against ``origin/main``. The reviewer, given megabytes of unrelated files,
    exits 1 and the loop stops with no PR to anchor a repair. This pins the fix:
    the diff is resolved from origin, so it contains only the task's own change.
    """

    def test_reviewer_diff_is_only_the_task_change_when_local_default_lags(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            target, stale_tip, remote_tip = _repo_with_local_default_behind_remote(d, bulk_files=60)
            ensure_control_dirs(target)
            task_branch = "shiki/lag-regression"
            wt = d / "wt"
            # The task branch is cut from the true remote tip and carries only the
            # task's own change — as ensure_physical_worktree cuts it from origin.
            _git(target, "worktree", "add", "-b", task_branch, str(wt), remote_tip)
            _git(wt, "config", "user.email", "t@t")
            _git(wt, "config", "user.name", "t")
            (wt / "feature.py").write_text("def f():\n    return 1\n")
            _git(wt, "add", "-A")
            _git(wt, "commit", "-m", "impl: the task's own change")
            _write(target / ".shiki" / "worktrees" / f"{TASK}.json",
                   {"path": str(wt), "branch": task_branch})
            _write(target / ".shiki" / "tasks" / f"{TASK}.json",
                   {"id": TASK, "goal_id": GOAL, "status": "review", "title": "t",
                    "scope": "the task's own change", "expected_branch": task_branch,
                    "ledger_evidence": []})

            # Discriminating power: the buggy base (stale local `main`) WOULD drag
            # the whole merged `bulk/` history into the diff. If any site is
            # reverted to a bare local name the reviewer is handed THIS diff, so the
            # file-count assertion below fails — the regression is proven, not
            # merely passing today.
            stale_diff = subprocess.run(
                ["git", "diff", "--cached", "main"], cwd=str(wt),
                capture_output=True, text=True,
            ).stdout
            self.assertGreater(_diff_file_count(stale_diff), 1)
            self.assertIn("bulk/", stale_diff)

            capture = d / "captured-prompt.txt"
            bindir = d / "bin"; bindir.mkdir()
            _install_capturing_reviewer(bindir, capture, json.dumps({"verdict": "clean", "findings": []}))
            old = _with_path(bindir)
            try:
                result = shiki_loop._run_pre_pr_code_review(target, TASK)
            finally:
                os.environ["PATH"] = old

            self.assertEqual(result["status"], "clean")
            shown = capture.read_text(encoding="utf-8")
            # The reviewer saw ONLY the task's own change: exactly one file, and
            # none of the already-merged bulk history. Asserted by file count AND
            # byte size (not content sampling) so a regression is loud.
            self.assertEqual(_diff_file_count(shown), 1)
            self.assertIn("feature.py", shown)
            self.assertNotIn("bulk/", shown)
            self.assertLess(len(shown), len(stale_diff) // 10)
            # Only the tracking ref was refreshed by the resolver's fetch; the
            # local default branch itself was left untouched.
            self.assertEqual(_rev(target, "main"), stale_tip)
            self.assertEqual(_rev(target, "origin/main"), remote_tip)


def _load_task(target: Path) -> dict:
    return json.loads((target / ".shiki" / "tasks" / f"{TASK}.json").read_text())


def _write_task_field(target: Path, **fields) -> None:
    task = _load_task(target)
    task.update(fields)
    (target / ".shiki" / "tasks" / f"{TASK}.json").write_text(json.dumps(task), encoding="utf-8")


_DEGENERATE = json.dumps({"verdict": "blocking", "findings": []})
_WITH_FINDINGS = json.dumps({"verdict": "blocking", "findings": [{"title": "real bug"}]})
_CLEAN = json.dumps({"verdict": "clean", "findings": []})


class DegenerateReviewRetryTests(unittest.TestCase):
    """A blocking review naming NOTHING is not a judgment: retry once (bounded,
    durable, reset on re-dispatch), record the raw output, never treat it as clean."""

    def _review(self, env: _Env, verdict_json: str, bindir: Path):
        _install_stub_reviewer(bindir, verdict_json)
        old = _with_path(bindir)
        try:
            return shiki_loop._run_pre_pr_code_review(env.target, TASK)
        finally:
            os.environ["PATH"] = old

    def test_blocking_zero_findings_retries_once_then_stops(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            # First degenerate verdict -> retry (budget consumed), NOT a stop.
            first = self._review(env, _DEGENERATE, bindir)
            self.assertEqual(first["status"], "retry")
            self.assertEqual(_load_task(env.target)["pre_pr_review_empty_retries"], 1)
            # Second degenerate verdict -> budget exhausted -> stop (blocking).
            second = self._review(env, _DEGENERATE, bindir)
            self.assertEqual(second["status"], "blocking")
            self.assertNotEqual(second["status"], "retry")
            # The counter bounds the SAME reviewed work: it did not grow past the bound.
            self.assertEqual(_load_task(env.target)["pre_pr_review_empty_retries"], 1)

    def test_retry_that_returns_findings_stops_with_them_named(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            self.assertEqual(self._review(env, _DEGENERATE, bindir)["status"], "retry")
            # The re-run produced a real finding: stop with it named (not a retry).
            second = self._review(env, _WITH_FINDINGS, bindir)
            self.assertEqual(second["status"], "blocking")
            self.assertEqual(_load_task(env.target)["pre_pr_code_review"]["findings"], [{"title": "real bug"}])

    def test_retry_that_returns_clean_proceeds(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            self.assertEqual(self._review(env, _DEGENERATE, bindir)["status"], "retry")
            # The re-run came back clean: proceed exactly as a first-pass clean would.
            second = self._review(env, _CLEAN, bindir)
            self.assertEqual(second["status"], "clean")
            self.assertEqual(_load_task(env.target)["pre_pr_code_review"]["verdict"], "clean")

    def test_degenerate_verdict_is_never_treated_as_clean(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            first = self._review(env, _DEGENERATE, bindir)
            self.assertNotEqual(first["status"], "clean")
            # Exhaust the budget; still never clean.
            second = self._review(env, _DEGENERATE, bindir)
            self.assertNotEqual(second["status"], "clean")
            recorded = _load_task(env.target)["pre_pr_code_review"]
            self.assertNotEqual(recorded["verdict"], "clean")
            self.assertTrue(recorded["degenerate"])

    def test_reviewer_raw_output_is_recorded_durably(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            result = self._review(env, _DEGENERATE, bindir)
            # A ledger id was returned; an EXEC record it references holds the raw output.
            ledger_id = result["ledger_id"]
            ledger = json.loads((env.target / ".shiki" / "ledger" / f"{ledger_id}.json").read_text())
            self.assertEqual(ledger["type"], "check")
            exec_refs = [ref for ref in ledger["evidence"] if ".shiki/runner/" in ref]
            self.assertTrue(exec_refs, ledger)
            record = json.loads((env.target / exec_refs[0]).read_text())
            self.assertIn("blocking", record["stdout"])

    def test_counter_resets_on_redispatch_and_persists_otherwise(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            # Simulate a consumed budget on the reviewed work.
            _write_task_field(env.target, pre_pr_review_empty_retries=1)

            # (1) Persists when NOT re-dispatched: a fresh review of the SAME work
            # does not reset the counter (it stays bounded).
            bindir = d / "bin"; bindir.mkdir()
            self._review(env, _DEGENERATE, bindir)
            self.assertEqual(_load_task(env.target)["pre_pr_review_empty_retries"], 1)

            # (2) Resets on (re-)dispatch: dispatching fresh work clears the budget so
            # it never wedges newly re-dispatched work.
            orig = shiki_loop._dispatch
            shiki_loop._dispatch = lambda *a, **k: 0
            try:
                shiki_loop.execute_action(
                    env.target, GOAL, {"action": "dispatch", "task_id": TASK}, repair_limit=3
                )
            finally:
                shiki_loop._dispatch = orig
            self.assertEqual(_load_task(env.target)["pre_pr_review_empty_retries"], 0)


class DegenerateReviewCreatePrGateTests(unittest.TestCase):
    """The create_pr effector waits (re-runs the reviewer next pass) on a degenerate
    verdict with budget, and stops — never opens a PR — once the budget is spent."""

    def _execute(self, env: _Env, verdict_json: str, bindir: Path):
        _install_stub_reviewer(bindir, verdict_json)
        old = _with_path(bindir)
        try:
            decision = {"action": "create_pr", "task_id": TASK, "reason": "in review with no PR"}
            return shiki_loop.execute_action(env.target, GOAL, decision, repair_limit=3)
        finally:
            os.environ["PATH"] = old

    def test_degenerate_verdict_waits_then_stops_without_opening_a_pr(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            env = _Env(d)
            bindir = d / "bin"; bindir.mkdir()
            first = self._execute(env, _DEGENERATE, bindir)
            self.assertEqual(first["action"], "wait_review")
            self.assertNotIn("impl_commit", first)
            self.assertIsNone(_load_task(env.target).get("expected_pr"))
            # Budget exhausted on the next pass -> stop_blocked, still no PR.
            second = self._execute(env, _DEGENERATE, bindir)
            self.assertEqual(second["action"], "stop_blocked")
            self.assertNotIn("impl_commit", second)
            self.assertIsNone(_load_task(env.target).get("expected_pr"))


if __name__ == "__main__":
    unittest.main()
