"""Tests for `shiki secret set-claude` token mint/verify/clean-set helpers.

The unavoidable interactive step (`claude setup-token` browser auth) and the live
`gh secret set` are not exercised here; the deterministic, testable pieces are:
token extraction, the isolated-config probe command, the probe-result
interpretation (the CubeShare 401 signature), and the mint/verify wrappers with
injected runners.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_github as gh
from shiki_process import ShikiError


class ExtractTokenTests(unittest.TestCase):
    def test_extracts_bare_token(self):
        tok = "sk-ant-oat01-" + "A" * 80
        self.assertEqual(gh.extract_setup_token_value(tok), tok)

    def test_extracts_token_from_surrounding_ui(self):
        tok = "sk-ant-oat01-" + "b3Z_x" * 12
        out = f"Open https://claude.ai/oauth to authorize.\nYour token:\n  {tok}\nDone.\n"
        self.assertEqual(gh.extract_setup_token_value(out), tok)

    def test_returns_none_when_absent(self):
        self.assertIsNone(gh.extract_setup_token_value("authorization cancelled"))
        self.assertIsNone(gh.extract_setup_token_value(""))

    def test_picks_longest_match(self):
        short = "sk-ant-oat01-abc"
        full = "sk-ant-oat01-" + "Z" * 90
        self.assertEqual(gh.extract_setup_token_value(f"{short}\n{full}"), full)

    def test_looks_like_token(self):
        self.assertTrue(gh.looks_like_claude_oauth_token("sk-ant-oat01-" + "x" * 40))
        self.assertTrue(gh.looks_like_claude_oauth_token("  sk-ant-oat01-" + "x" * 40 + "  "))
        self.assertFalse(gh.looks_like_claude_oauth_token("sk-ant-api03-" + "x" * 40))
        self.assertFalse(gh.looks_like_claude_oauth_token("not-a-token"))
        self.assertFalse(gh.looks_like_claude_oauth_token(""))


class ProbeInvocationTests(unittest.TestCase):
    def test_isolates_config_and_sets_token(self):
        argv, env, cwd = gh.token_probe_invocation("sk-ant-oat01-tok", "/tmp/probe-dir")
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--output-format", argv)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/tmp/probe-dir")
        self.assertEqual(env["HOME"], "/tmp/probe-dir")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-tok")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")

    def test_runs_from_clean_isolated_working_directory(self):
        # The probe must run from a clean temp dir with no project .claude, not the
        # repo root (shiki_process.run defaults cwd=ROOT, and ROOT has a .claude/).
        # Otherwise a repo-local .claude/settings.json (apiKeyHelper / env creds)
        # would be discovered and could authenticate a bad token. The cwd is the
        # isolated config_dir, which carries no .claude.
        _, _, cwd = gh.token_probe_invocation("sk-ant-oat01-tok", "/tmp/probe-dir")
        self.assertEqual(cwd, "/tmp/probe-dir")

    def test_suppresses_project_local_settings_sources_when_supported(self):
        # With the CLI flag supported, the probe loads only the (isolated) user
        # settings source, dropping project/local .claude/settings*.json so a
        # repo-local apiKeyHelper / env credential cannot authenticate the probe.
        argv, _, _ = gh.token_probe_invocation("sk-ant-oat01-tok", "/tmp/probe-dir")
        self.assertIn("--setting-sources", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")

    def test_omits_setting_sources_flag_when_unsupported(self):
        # An older CLI without the flag falls back to clean-working-directory
        # isolation only (still fail-closed) rather than erroring on an unknown flag.
        argv, _, _ = gh.token_probe_invocation(
            "sk-ant-oat01-tok", "/tmp/probe-dir", setting_sources=False
        )
        self.assertNotIn("--setting-sources", argv)

    def test_blanks_higher_precedence_credential_and_routing_env(self):
        # The probe env is merged OVER the inherited os.environ (shiki_process.run
        # does os.environ.copy().update(env)), so every ambient Anthropic/Claude
        # credential or cloud-route that could authenticate the probe independently
        # of the candidate OAuth token must be explicitly blanked to "". Otherwise
        # an ambient ANTHROPIC_AUTH_TOKEN / API key / Bedrock-Vertex route makes a
        # bad CLAUDE_CODE_OAUTH_TOKEN pass — the false positive that hides a CCA 401.
        _, env, _ = gh.token_probe_invocation("sk-ant-oat01-tok", "/tmp/probe-dir")
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_BEDROCK_BASE_URL",
            "ANTHROPIC_VERTEX_BASE_URL",
            # Microsoft Foundry route: a separate provider whose API key, base URL,
            # resource, and USE/SKIP-auth toggles can authenticate `claude`
            # independently of the candidate OAuth token, so each must be blanked.
            "ANTHROPIC_FOUNDRY_API_KEY",
            "ANTHROPIC_FOUNDRY_BASE_URL",
            "ANTHROPIC_FOUNDRY_RESOURCE",
            "AWS_BEARER_TOKEN_BEDROCK",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        ):
            self.assertEqual(env.get(name), "", f"{name} must be blanked in the probe env")
        # The candidate token is the sole credential left under test.
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-tok")

    def test_every_central_blanked_var_is_applied(self):
        # Drift guard: every name in the central _PROBE_BLANKED_CREDENTIAL_ENV
        # tuple must actually be blanked to "" in the probe env. This catches a
        # future provider route added to the tuple but not to the probe (or vice
        # versa) without re-listing the names here.
        _, env, _ = gh.token_probe_invocation("sk-ant-oat01-tok", "/tmp/probe-dir")
        for name in gh._PROBE_BLANKED_CREDENTIAL_ENV:
            self.assertEqual(env.get(name), "", f"{name} must be blanked in the probe env")
        # The Foundry route must be among the blanked credentials.
        for name in (
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
            "ANTHROPIC_FOUNDRY_API_KEY",
            "ANTHROPIC_FOUNDRY_BASE_URL",
            "ANTHROPIC_FOUNDRY_RESOURCE",
        ):
            self.assertIn(name, gh._PROBE_BLANKED_CREDENTIAL_ENV)


class SettingSourcesSupportTests(unittest.TestCase):
    def _help(self, text):
        def fake(argv, *, check=True):
            return types.SimpleNamespace(args=argv, returncode=0, stdout=text, stderr="")
        return fake

    def test_detects_supported_flag(self):
        help_text = "  --setting-sources <sources>  Comma-separated list of setting sources"
        self.assertTrue(gh.claude_supports_setting_sources(runner=self._help(help_text)))

    def test_detects_unsupported_flag(self):
        self.assertFalse(
            gh.claude_supports_setting_sources(runner=self._help("  --print  Run a single prompt"))
        )

    def test_detects_from_stderr_help(self):
        def fake(argv, *, check=True):
            return types.SimpleNamespace(args=argv, returncode=0, stdout="", stderr="--setting-sources <sources>")
        self.assertTrue(gh.claude_supports_setting_sources(runner=fake))


class InterpretProbeTests(unittest.TestCase):
    def test_valid_token_signature(self):
        ok, _ = gh.interpret_token_probe({"is_error": False, "total_cost_usd": 0.39, "result": "pong"})
        self.assertTrue(ok)

    def test_invalid_token_401_signature(self):
        ok, reason = gh.interpret_token_probe(
            {"is_error": True, "total_cost_usd": 0, "result": "Failed to authenticate. API Error: 401 Invalid bearer token"}
        )
        self.assertFalse(ok)
        self.assertIn("401", reason)

    def test_zero_cost_without_error_is_invalid(self):
        ok, _ = gh.interpret_token_probe({"is_error": False, "total_cost_usd": 0})
        self.assertFalse(ok)

    def test_bool_cost_is_invalid(self):
        ok, _ = gh.interpret_token_probe({"is_error": False, "total_cost_usd": True})
        self.assertFalse(ok)

    def test_missing_cost_is_invalid(self):
        ok, _ = gh.interpret_token_probe({"is_error": False})
        self.assertFalse(ok)

    def test_non_dict_is_invalid(self):
        ok, _ = gh.interpret_token_probe("nope")
        self.assertFalse(ok)

    def test_token_redacted_from_detail(self):
        tok = "sk-ant-oat01-" + "D" * 40
        ok, reason = gh.interpret_token_probe({"is_error": True, "total_cost_usd": 0, "result": f"leaked {tok}"})
        self.assertFalse(ok)
        self.assertNotIn("sk-ant-oat", reason)
        self.assertIn("[REDACTED]", reason)


class MintTests(unittest.TestCase):
    def test_mint_extracts_from_capture(self):
        tok = "sk-ant-oat01-" + "M" * 50
        self.assertEqual(gh.mint_claude_oauth_token(capture=lambda: f"prompts...\n{tok}\n"), tok)

    def test_mint_raises_when_no_token(self):
        with self.assertRaises(ShikiError):
            gh.mint_claude_oauth_token(capture=lambda: "user cancelled authorization")


class VerifyTests(unittest.TestCase):
    def setUp(self):
        # verify_claude_oauth_token guards on require_tool("claude"); the probe
        # itself is injected, so neutralize the guard so the suite runs where the
        # claude CLI is absent (e.g. CI).
        self._orig_require_tool = gh.require_tool
        gh.require_tool = lambda name: None

    def tearDown(self):
        gh.require_tool = self._orig_require_tool

    def _runner(self, stdout, stderr="", *, supports_setting_sources=True):
        # verify_claude_oauth_token first probes `claude --help` to decide whether
        # --setting-sources is supported, then runs the actual token probe (now in
        # an isolated cwd). The fake answers both: help text for the detection call,
        # the canned probe output otherwise. It also accepts cwd= because the probe
        # is run from the isolated working directory.
        help_out = "--setting-sources <sources>" if supports_setting_sources else "(no flag)"

        def fake(argv, *, env=None, cwd=None, input_text=None, check=True):
            if "--help" in argv:
                return types.SimpleNamespace(args=argv, returncode=0, stdout=help_out, stderr="")
            return types.SimpleNamespace(args=argv, returncode=0, stdout=stdout, stderr=stderr)
        return fake

    def test_verify_passes_on_valid_probe(self):
        ok, _ = gh.verify_claude_oauth_token(
            "sk-ant-oat01-tok", runner=self._runner('{"is_error": false, "total_cost_usd": 0.4}')
        )
        self.assertTrue(ok)

    def test_verify_fails_on_401_probe(self):
        ok, reason = gh.verify_claude_oauth_token(
            "sk-ant-oat01-tok",
            runner=self._runner('{"is_error": true, "total_cost_usd": 0, "result": "401 Invalid bearer token"}'),
        )
        self.assertFalse(ok)
        self.assertIn("401", reason)

    def test_verify_fails_on_non_json(self):
        ok, _ = gh.verify_claude_oauth_token(
            "sk-ant-oat01-tok", runner=self._runner("", stderr="Not logged in")
        )
        self.assertFalse(ok)

    def test_verify_redacts_token_in_surfaced_stderr(self):
        tok = "sk-ant-oat01-" + "S" * 40
        ok, reason = gh.verify_claude_oauth_token(
            "sk-ant-oat01-tok", runner=self._runner("not json", stderr=f"boom {tok}")
        )
        self.assertFalse(ok)
        self.assertNotIn("sk-ant-oat", reason)

    def test_ambient_credential_cannot_make_bad_token_pass(self):
        # Regression: a higher-precedence ambient credential must NOT authenticate
        # the probe. The fake runner reproduces shiki_process.run's merge (ambient
        # os.environ overridden by the probe env) and claude's precedence: any
        # non-empty ambient Anthropic credential authenticates regardless of the
        # candidate OAuth token. With the credential-exclusive probe env those are
        # blanked to "", so only the (bad) candidate token remains -> probe 401s.
        ambient = {
            "ANTHROPIC_AUTH_TOKEN": "sk-ant-ambient-valid-bearer",
            "ANTHROPIC_API_KEY": "ambient-api-key",
        }

        def fake(argv, *, env=None, cwd=None, input_text=None, check=True):
            if "--help" in argv:
                return types.SimpleNamespace(args=argv, returncode=0, stdout="--setting-sources", stderr="")
            effective = {**ambient, **(env or {})}
            leaked = [k for k in ambient if effective.get(k)]
            if leaked:
                stdout = '{"is_error": false, "total_cost_usd": 0.4, "result": "pong"}'
            else:
                stdout = '{"is_error": true, "total_cost_usd": 0, "result": "401 Invalid bearer token"}'
            return types.SimpleNamespace(args=argv, returncode=0, stdout=stdout, stderr="")

        ok, reason = gh.verify_claude_oauth_token("sk-ant-oat01-" + "b" * 40, runner=fake)
        self.assertFalse(ok, "ambient credential must not authenticate the probe")
        self.assertIn("401", reason)

    def test_ambient_foundry_credentials_cannot_make_bad_token_pass(self):
        # Regression: an ambient Microsoft Foundry route (CLAUDE_CODE_USE_FOUNDRY
        # plus Foundry credentials/base-URL/resource) must NOT authenticate the
        # probe. The fake runner reproduces shiki_process.run's merge (ambient
        # os.environ overridden by the probe env) and Foundry's precedence: when
        # the route is enabled AND a Foundry credential is present, claude routes
        # through Foundry and authenticates regardless of the candidate OAuth
        # token. The credential-exclusive probe env blanks every Foundry var to
        # "", so the route is off and only the (bad) candidate token remains -> 401.
        ambient = {
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH": "1",
            "ANTHROPIC_FOUNDRY_API_KEY": "foundry-ambient-key",
            "ANTHROPIC_FOUNDRY_BASE_URL": "https://example.foundry.invalid",
            "ANTHROPIC_FOUNDRY_RESOURCE": "ambient-resource",
        }

        def fake(argv, *, env=None, cwd=None, input_text=None, check=True):
            if "--help" in argv:
                return types.SimpleNamespace(args=argv, returncode=0, stdout="--setting-sources", stderr="")
            effective = {**ambient, **(env or {})}
            foundry_active = effective.get("CLAUDE_CODE_USE_FOUNDRY") and effective.get(
                "ANTHROPIC_FOUNDRY_API_KEY"
            )
            if foundry_active:
                stdout = '{"is_error": false, "total_cost_usd": 0.4, "result": "pong"}'
            else:
                stdout = '{"is_error": true, "total_cost_usd": 0, "result": "401 Invalid bearer token"}'
            return types.SimpleNamespace(args=argv, returncode=0, stdout=stdout, stderr="")

        ok, reason = gh.verify_claude_oauth_token("sk-ant-oat01-" + "f" * 40, runner=fake)
        self.assertFalse(ok, "ambient Foundry route must not authenticate the probe")
        self.assertIn("401", reason)


class ProjectSettingsIsolationTests(unittest.TestCase):
    """A repo-local .claude/settings.json/.local.json must not authenticate the probe.

    Regression for the false positive where the verify probe ran in the repo root
    (shiki_process.run defaults cwd=ROOT) and a project-supplied apiKeyHelper / env
    credential authenticated a bad candidate token. The credential-exclusive probe
    now runs from an isolated empty working directory and passes
    --setting-sources user, so project/local .claude settings are neither
    discovered nor loaded and an invalid token still fails closed.
    """

    def setUp(self):
        # verify_claude_oauth_token guards on require_tool("claude"); neutralize so
        # the suite runs where the claude CLI is absent.
        self._orig_require_tool = gh.require_tool
        gh.require_tool = lambda name: None
        # A real "repo" working directory carrying malicious project settings: both
        # an apiKeyHelper (settings.json) and env credentials (settings.local.json).
        self.project = Path(tempfile.mkdtemp(prefix="shiki-probe-projsettings-"))
        claude_dir = self.project / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps({"apiKeyHelper": "printf sk-ant-oat01-projectsupplied"}), encoding="utf-8"
        )
        (claude_dir / "settings.local.json").write_text(
            json.dumps({"env": {"ANTHROPIC_API_KEY": "project-local-key"}}), encoding="utf-8"
        )

    def tearDown(self):
        gh.require_tool = self._orig_require_tool
        shutil.rmtree(self.project, ignore_errors=True)

    @staticmethod
    def _ns(stdout, stderr=""):
        return types.SimpleNamespace(args=None, returncode=0, stdout=stdout, stderr=stderr)

    def _project_settings_runner(self, captured):
        # Model claude's settings discovery: a project .claude/settings(.local).json
        # that supplies env creds / apiKeyHelper authenticates the probe ONLY when it
        # is (a) discoverable from the working directory AND (b) not suppressed via
        # --setting-sources. The credential-exclusive probe satisfies neither, so the
        # bad token 401s.
        def fake(argv, *, env=None, cwd=None, input_text=None, check=True):
            if "--help" in argv:
                return self._ns("--setting-sources <sources>")
            captured["cwd"] = cwd
            captured["argv"] = argv
            run_dir = Path(cwd) if cwd else Path.cwd()
            project_creds = (run_dir / ".claude" / "settings.json").exists() or (
                run_dir / ".claude" / "settings.local.json"
            ).exists()
            suppressed = "--setting-sources" in argv
            if project_creds and not suppressed:
                return self._ns('{"is_error": false, "total_cost_usd": 0.4, "result": "pong"}')
            return self._ns('{"is_error": true, "total_cost_usd": 0, "result": "401 Invalid bearer token"}')
        return fake

    def test_positive_control_project_settings_would_authenticate_unprotected_probe(self):
        # Sanity: the model is not trivially always-401. Run the SAME fake the way an
        # unprotected probe would run — in the project dir, project/local settings
        # NOT suppressed — and a bad token authenticates. This is exactly the false
        # positive the isolation prevents below.
        fake = self._project_settings_runner({})
        result = fake(["claude", "-p", "ping", "--output-format", "json"], cwd=str(self.project))
        self.assertIn('"is_error": false', result.stdout)

    def test_repo_local_settings_do_not_make_bad_token_pass(self):
        captured = {}
        ok, reason = gh.verify_claude_oauth_token(
            "sk-ant-oat01-" + "p" * 40, runner=self._project_settings_runner(captured)
        )
        self.assertFalse(ok, "repo-local .claude settings must not authenticate a bad token")
        self.assertIn("401", reason)
        # The probe must have run in an explicit isolated cwd (not the default repo
        # root) that carries no project .claude, and must suppress project/local
        # settings — the two defenses whose removal would reopen the false positive.
        self.assertIsNotNone(captured.get("cwd"), "probe must run in an explicit isolated cwd")
        run_dir = Path(captured["cwd"])
        self.assertNotEqual(run_dir, self.project)
        self.assertFalse((run_dir / ".claude" / "settings.json").exists())
        self.assertFalse((run_dir / ".claude" / "settings.local.json").exists())
        self.assertIn("--setting-sources", captured["argv"])


class ManagedSettingsTests(unittest.TestCase):
    def _with_existing(self, present):
        orig = os.path.exists
        os.path.exists = lambda path: path in set(present)
        self.addCleanup(lambda: setattr(os.path, "exists", orig))

    def test_detects_present_managed_file(self):
        self._with_existing(["/etc/claude-code/managed-settings.json"])
        self.assertIn("/etc/claude-code/managed-settings.json", gh.managed_claude_settings_paths())

    def test_empty_when_none_present(self):
        self._with_existing([])
        self.assertEqual(gh.managed_claude_settings_paths(), [])

    def test_includes_windows_programdata_when_set(self):
        win = os.path.join("C:\\ProgramData", "ClaudeCode", "managed-settings.json")
        self._with_existing([win])
        orig_env = os.environ.get("PROGRAMDATA")
        os.environ["PROGRAMDATA"] = "C:\\ProgramData"
        self.addCleanup(lambda: os.environ.__setitem__("PROGRAMDATA", orig_env) if orig_env is not None else os.environ.pop("PROGRAMDATA", None))
        self.assertIn(win, gh.managed_claude_settings_paths())


class CmdTests(unittest.TestCase):
    """cmd_secret_set_claude orchestration: verification is mandatory and fails closed."""

    def setUp(self):
        self._orig = {
            k: getattr(gh, k)
            for k in (
                "require_tool",
                "set_secret",
                "verify_claude_oauth_token",
                "load_default_config",
                "managed_claude_settings_paths",
            )
        }
        gh.require_tool = lambda name: None
        self.set_calls = []
        gh.set_secret = lambda repo, name, value, provider_config=None: self.set_calls.append((repo, name, value))
        gh.load_default_config = lambda: {"repo": "owner/name"}
        # Default: no managed settings, so the host running the suite cannot affect
        # the fail-closed assertions. The managed-settings case is tested explicitly.
        gh.managed_claude_settings_paths = lambda: []

    def tearDown(self):
        for key, value in self._orig.items():
            setattr(gh, key, value)
        os.environ.pop("SHIKI_TEST_TOK", None)

    def _args(self, **kw):
        defaults = dict(repo="owner/name", token_stdin=False, from_env=None)
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_missing_repo_errors(self):
        gh.load_default_config = lambda: {}
        with self.assertRaises(ShikiError):
            gh.cmd_secret_set_claude(self._args(repo=None, from_env="SHIKI_TEST_TOK"))
        self.assertEqual(self.set_calls, [])

    def test_verification_failure_blocks_secret_set(self):
        os.environ["SHIKI_TEST_TOK"] = "sk-ant-oat01-" + "x" * 40
        gh.verify_claude_oauth_token = lambda token: (False, "401 Invalid bearer token")
        with self.assertRaises(ShikiError):
            gh.cmd_secret_set_claude(self._args(from_env="SHIKI_TEST_TOK"))
        self.assertEqual(self.set_calls, [])  # fail-closed: secret NOT set

    def test_valid_token_is_set_verbatim(self):
        token = "sk-ant-oat01-" + "y" * 40
        os.environ["SHIKI_TEST_TOK"] = token
        gh.verify_claude_oauth_token = lambda t: (True, "ok")
        rc = gh.cmd_secret_set_claude(self._args(from_env="SHIKI_TEST_TOK"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.set_calls), 1)
        self.assertEqual(self.set_calls[0], ("owner/name", "CLAUDE_CODE_OAUTH_TOKEN", token))

    def test_missing_env_token_errors_before_set(self):
        gh.verify_claude_oauth_token = lambda t: (True, "ok")
        with self.assertRaises(ShikiError):
            gh.cmd_secret_set_claude(self._args(from_env="SHIKI_TEST_TOK"))  # unset env
        self.assertEqual(self.set_calls, [])

    def test_managed_settings_block_secret_set(self):
        # Fail closed when managed/enterprise settings are present: the probe can
        # no longer guarantee token-exclusive verification, so set_secret (and the
        # token mint) must not run, even for a token that would otherwise verify.
        os.environ["SHIKI_TEST_TOK"] = "sk-ant-oat01-" + "m" * 40
        gh.managed_claude_settings_paths = lambda: ["/etc/claude-code/managed-settings.json"]
        verify_called = []
        gh.verify_claude_oauth_token = lambda t: (verify_called.append(t), (True, "ok"))[1]
        with self.assertRaises(ShikiError) as ctx:
            gh.cmd_secret_set_claude(self._args(from_env="SHIKI_TEST_TOK"))
        self.assertIn("managed", str(ctx.exception).lower())
        self.assertEqual(self.set_calls, [])  # fail-closed: secret NOT set
        self.assertEqual(verify_called, [])  # refused before even probing/minting

    def test_set_secret_failure_redacts_token(self):
        # If set_secret fails AFTER successful verification (e.g. gh echoes input
        # in stderr), the surfaced error must never carry the token text.
        token = "sk-ant-oat01-" + "z" * 40
        os.environ["SHIKI_TEST_TOK"] = token
        gh.verify_claude_oauth_token = lambda t: (True, "ok")

        def boom(repo, name, value, provider_config=None):
            raise ShikiError(f"gh secret set failed\nleaked stdin: {value}")

        gh.set_secret = boom
        with self.assertRaises(ShikiError) as ctx:
            gh.cmd_secret_set_claude(self._args(from_env="SHIKI_TEST_TOK"))
        message = str(ctx.exception)
        self.assertNotIn(token, message)
        self.assertNotIn("sk-ant-oat", message)


if __name__ == "__main__":
    unittest.main()
