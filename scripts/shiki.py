#!/usr/bin/env python3
"""Executable Shiki CLI shim.

The implementation lives in dependency-free standard-library modules. These
re-exports are transitional compatibility for existing tests and target repos;
new code should import from the canonical shiki_* modules directly.
"""

from __future__ import annotations

from shiki_cli import build_parser, main
from shiki_config import branch_protection_review_count
from shiki_github import protect_branch
from shiki_installer import TEMPLATE_PATHS, manifest_stage_paths
from shiki_process import CommandResult, ShikiError, ensure_control_dirs, run

__all__ = [
    "CommandResult",
    "ShikiError",
    "TEMPLATE_PATHS",
    "branch_protection_review_count",
    "build_parser",
    "ensure_control_dirs",
    "main",
    "manifest_stage_paths",
    "protect_branch",
    "run",
]


if __name__ == "__main__":
    raise SystemExit(main())
