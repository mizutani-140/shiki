#!/usr/bin/env bash
# P2.3.8 — Failed branch-protection enforcement surfaces a hard failure.
#
# shiki_github.protect_branch must raise ShikiError (non-zero outcome) when the
# GitHub API call to configure branch protection fails, so a repository can
# never be left silently unprotected. The success path must not raise.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path.cwd() / "scripts"))

import shiki_github
from shiki_process import ShikiError

shiki_github.info = lambda *a, **k: None


class StubRun:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def __call__(self, *args, **kwargs):
        return SimpleNamespace(returncode=self.returncode, stdout="", stderr="api rejected")


# Failure path: API returns non-zero -> ShikiError with remediation.
shiki_github.run = StubRun(1)
try:
    shiki_github.protect_branch("owner/name", "main", ["CCA verdict"], review_count=1)
except ShikiError as error:
    assert "branch protection" in str(error).lower(), str(error)
else:
    raise SystemExit("expected ShikiError when branch protection enforcement fails")

# Success path: API returns zero -> no exception.
shiki_github.run = StubRun(0)
shiki_github.protect_branch("owner/name", "main", ["CCA verdict"], review_count=1)

print("P2.3.8 branch protection failure handling passed")
PY
