"""Tests for `shiki secret set-claude` token mint/verify/clean-set helpers.

The unavoidable interactive step (`claude setup-token` browser auth) and the live
`gh secret set` are not exercised here; the deterministic, testable pieces are:
token extraction, the isolated-config probe command, the probe-result
interpretation (the CubeShare 401 signature), and the mint/verify wrappers with
injected runners.
"""

from __future__ import annotations

import argparse
import os
import types
import unittest

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
        argv, env = gh.token_probe_invocation("sk-ant-oat01-tok", "/tmp/probe-dir")
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--output-format", argv)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/tmp/probe-dir")
        self.assertEqual(env["HOME"], "/tmp/probe-dir")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-tok")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")


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
    def _runner(self, stdout, stderr=""):
        def fake(argv, *, env=None, input_text=None, check=True):
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


class CmdTests(unittest.TestCase):
    """cmd_secret_set_claude orchestration: verification is mandatory and fails closed."""

    def setUp(self):
        self._orig = {
            k: getattr(gh, k)
            for k in ("require_tool", "set_secret", "verify_claude_oauth_token", "load_default_config")
        }
        gh.require_tool = lambda name: None
        self.set_calls = []
        gh.set_secret = lambda repo, name, value, provider_config=None: self.set_calls.append((repo, name, value))
        gh.load_default_config = lambda: {"repo": "owner/name"}

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


if __name__ == "__main__":
    unittest.main()
