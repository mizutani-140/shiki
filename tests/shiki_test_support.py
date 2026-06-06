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
