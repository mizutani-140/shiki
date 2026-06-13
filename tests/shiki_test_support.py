"""Shared helpers for the Shiki Python regression test suite.

These tests import the real ``scripts/`` modules and assert their observed
behavior. They never modify the modules under test. The suite is dependency
free: standard-library ``unittest`` only.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def ensure_scripts_on_path() -> None:
    """Put ``scripts/`` on ``sys.path`` so the real modules import cleanly."""
    scripts = str(SCRIPTS_DIR)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


ensure_scripts_on_path()


def synthetic_secrets() -> dict[str, str]:
    """Secret-SHAPED strings assembled at runtime from fragments.

    The memory auto-capture tests must exercise redaction against realistic
    token shapes, but a contiguous token literal in source would trip GitHub
    secret scanning / push protection. Joining fragments keeps the literal out
    of the committed file while the runtime value still matches the redaction
    patterns under test. None of these are real credentials.
    """
    j = "".join
    return {
        "github_pat": j(["ghp", "_", "0123456789abcdefghij", "ABCDEFGHIJ0123"]),
        "github_fine": j(["github", "_pat_", "11ABCDEF0123456789_", "abcdefghijABCDEFGHIJ"]),
        "aws_key_id": j(["AK", "IA", "IOSFODNN7", "EXAMPLE"]),
        "slack": j(["xox", "b", "-1234567890-", "abcdefghijklmnop"]),
        "openai": j(["sk", "-", "abcdefghijklmnopqrstuvwxyz0123456789"]),
        "jwt": j(["eyJ", "hbGciOiJIUzI1", ".eyJ", "zdWIiOiIxMjM0NTY", ".SflKxwRJSMeKKF2QT"]),
        "aws_kv": j(["AWS_SECRET_ACCESS_KEY", "=", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"]),
    }
