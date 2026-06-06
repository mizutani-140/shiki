"""P2.3.1 — manifest staging selection (installer + manifest include set).

Asserts the real shiki_installer.manifest_stage_paths and exclusion logic, and
that the manifest helpers report the canonical include/exclude sets.
"""

from __future__ import annotations

import unittest

import shiki_test_support  # noqa: F401  (path bootstrap)
from shiki_test_support import REPO_ROOT

import shiki_installer
import shiki_manifest


# Every shiki_*.py module that scripts/shiki.py depends on must be staged so a
# target install is runnable. This mirrors the module-boundary contract.
REQUIRED_STAGE_PATHS = [
    "scripts/shiki.py",
    "scripts/shiki_cli.py",
    "scripts/shiki_config.py",
    "scripts/shiki_github.py",
    "scripts/shiki_installer.py",
    "scripts/shiki_locks.py",
    "scripts/shiki_manifest.py",
    "scripts/shiki_state.py",
    "scripts/shiki_tasks.py",
    "scripts/mergegate_check.py",
    "scripts/validate_shiki.py",
]


class ManifestStagingTests(unittest.TestCase):
    def test_stage_paths_include_required_modules(self) -> None:
        stage = set(shiki_installer.manifest_stage_paths(REPO_ROOT))
        for required in REQUIRED_STAGE_PATHS:
            self.assertIn(required, stage)

    def test_stage_paths_include_manifest_and_repo_state(self) -> None:
        stage = set(shiki_installer.manifest_stage_paths(REPO_ROOT))
        self.assertIn(".shiki/manifest.json", stage)

    def test_stage_paths_only_include_existing_files(self) -> None:
        for relative in shiki_installer.manifest_stage_paths(REPO_ROOT):
            self.assertTrue((REPO_ROOT / relative).exists(), f"staged missing path {relative}")

    def test_excluded_from_commit_glob(self) -> None:
        patterns = [".shiki/gha/**"]
        self.assertTrue(shiki_installer.excluded_from_commit(".shiki/gha/pr.json", patterns))
        self.assertTrue(shiki_installer.excluded_from_commit(".shiki/gha", patterns))
        self.assertFalse(shiki_installer.excluded_from_commit(".shiki/tasks/T-1.json", patterns))

    def test_excluded_from_commit_exact(self) -> None:
        patterns = [".shiki/repo.json"]
        self.assertTrue(shiki_installer.excluded_from_commit(".shiki/repo.json", patterns))
        self.assertFalse(shiki_installer.excluded_from_commit(".shiki/repo2.json", patterns))

    def test_runtime_directories_are_excluded_from_stage(self) -> None:
        manifest = shiki_manifest.load_manifest(REPO_ROOT)
        excluded = shiki_manifest.manifest_exclude_from_commit(manifest)
        stage = shiki_installer.manifest_stage_paths(REPO_ROOT)
        for relative in stage:
            self.assertFalse(
                shiki_installer.excluded_from_commit(relative, excluded),
                f"staged path {relative} is in exclude_from_commit",
            )


if __name__ == "__main__":
    unittest.main()
