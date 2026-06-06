#!/usr/bin/env bash
# P2.3.5 — Missing CLAUDE_CODE_OAUTH_TOKEN source for a configured CCA.
#
# The default Shiki runtime split implements GitHub CCA with Claude Code Action
# authenticated by the CLAUDE_CODE_OAUTH_TOKEN secret. When secret setup is
# enabled but the env source is absent, configure_claude_code_secret must fail
# fast with actionable remediation rather than silently configuring nothing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

from shiki_github import configure_claude_code_secret
from shiki_process import ShikiError

secret_env = "SHIKI_TEST_MISSING_CCA_TOKEN"
os.environ.pop(secret_env, None)

# Enabled but missing source -> hard failure with remediation.
try:
    configure_claude_code_secret(
        "owner/name",
        enabled=True,
        secret_env=secret_env,
    )
except ShikiError as error:
    message = str(error)
    assert secret_env in message, message
    assert "CLAUDE_CODE_OAUTH_TOKEN" in message, message
    assert "claude setup-token" in message, message
else:
    raise SystemExit("expected ShikiError when CCA token source is missing")

# Disabled secret setup must NOT raise; it returns an unconfigured status with
# an explanation instead.
status = configure_claude_code_secret(
    "owner/name",
    enabled=False,
    secret_env=secret_env,
)
assert status["enabled"] is False, status
assert status["configured"] is False, status
assert status["remediation"], status

# The CCA workflow must actually wire the secret into the action.
cca_yaml = (Path.cwd() / ".github" / "workflows" / "shiki-cca-completion.yml").read_text(
    encoding="utf-8"
)
assert "secrets.CLAUDE_CODE_OAUTH_TOKEN" in cca_yaml, "CCA workflow must consume the OAuth secret"

print("P2.3.5 missing CCA token handling passed")
PY
