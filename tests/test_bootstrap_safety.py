"""Bootstrap safety invariants: never destroy an existing target, never
silently overwrite an operator's stateful files under ``--force``.

These tests import the real ``scripts/`` modules and assert their observed
behavior against the safety contract for T-...d36555d8 ("never destroy an
existing target; never silently ..."). Dependency-free ``unittest`` only.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_git
import shiki_installer
from shiki_process import ShikiError


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _git_out(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "shiki@example.test", cwd=path)
    _git("config", "user.name", "Shiki Test", cwd=path)


def _commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content, encoding="utf-8")
    _git("add", filename, cwd=path)
    _git("commit", "-m", message, cwd=path)
    return _git_out("rev-parse", "HEAD", cwd=path)


class CheckoutBranchSafetyTests(unittest.TestCase):
    """checkout_branch must never move (reset) an existing target branch."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-checkout-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo = self.tmp / "repo"
        _init_repo(self.repo)

    def test_no_op_when_already_on_branch(self) -> None:
        _commit(self.repo, "a.txt", "a", "A")
        before = _git_out("rev-parse", "main", cwd=self.repo)
        shiki_git.checkout_branch(self.repo, "main")
        self.assertEqual(_git_out("rev-parse", "main", cwd=self.repo), before)
        self.assertEqual(_git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo), "main")

    def test_existing_branch_is_not_reset(self) -> None:
        main_sha = _commit(self.repo, "a.txt", "a", "A")  # main -> A
        _git("checkout", "-b", "other", cwd=self.repo)
        _commit(self.repo, "b.txt", "b", "B")  # other -> B, HEAD on other
        # Switching to an already-existing main must NOT move it; a `-B main`
        # would reset the ref to B and destroy the commit it pointed at.
        shiki_git.checkout_branch(self.repo, "main")
        self.assertEqual(_git_out("rev-parse", "main", cwd=self.repo), main_sha)
        self.assertEqual(_git_out("rev-parse", "HEAD", cwd=self.repo), main_sha)
        reflog = subprocess.run(
            ["git", "reflog", "show", "main"], cwd=str(self.repo), capture_output=True, text=True
        ).stdout
        self.assertNotIn("reset", reflog.lower())

    def test_missing_branch_is_created_at_head(self) -> None:
        head = _commit(self.repo, "a.txt", "a", "A")
        shiki_git.checkout_branch(self.repo, "feature")
        self.assertEqual(_git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo), "feature")
        self.assertEqual(_git_out("rev-parse", "feature", cwd=self.repo), head)

    def test_failed_checkout_raises_shiki_error(self) -> None:
        _commit(self.repo, "a.txt", "a", "A")
        _git("checkout", "-b", "other", cwd=self.repo)
        original_run = shiki_git.run
        try:
            shiki_git.run = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom")
            with self.assertRaises(ShikiError):
                shiki_git.checkout_branch(self.repo, "main")
        finally:
            shiki_git.run = original_run


class InstallerPreserveUnderForceTests(unittest.TestCase):
    """install --force must never clobber an operator's stateful files."""

    STATEFUL = {
        ".shiki/guardian-policy.json": '{"shiki_test_custom_guardian": true}\n',
        ".shiki/config.yaml": "shiki_test_custom_config: true\n",
        ".shiki/migrations/state.json": '{"shiki_test_custom_state": true}\n',
    }

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-installer-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "target"
        self.target.mkdir()
        self.warnings: list[str] = []
        self._orig_warn = shiki_installer.warn
        self._orig_info = shiki_installer.info
        shiki_installer.warn = lambda message, *a, **k: self.warnings.append(str(message))
        shiki_installer.info = lambda *a, **k: None
        self.addCleanup(self._restore)
        shiki_installer.install_template(self.target, force=False, validate=False)

    def _restore(self) -> None:
        shiki_installer.warn = self._orig_warn
        shiki_installer.info = self._orig_info

    def test_force_preserves_operator_stateful_files(self) -> None:
        for relative, content in self.STATEFUL.items():
            path = self.target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        self.warnings.clear()
        shiki_installer.install_template(self.target, force=True, validate=False)

        for relative, content in self.STATEFUL.items():
            self.assertEqual(
                (self.target / relative).read_text(encoding="utf-8"),
                content,
                f"{relative} was overwritten by --force",
            )
            filename = relative.rsplit("/", 1)[-1]
            self.assertTrue(
                any("preserv" in w.lower() and filename in w for w in self.warnings),
                f"expected a 'preserved' warning naming {filename}; got {self.warnings}",
            )

    def test_force_still_overwrites_non_stateful_template_files(self) -> None:
        # A non-stateful template file (README) must still be refreshed by
        # --force; the preserve carve-out is limited to operator state.
        readme = self.target / ".shiki" / "README.md"
        if not readme.exists():  # pragma: no cover - README ships in the template
            self.skipTest(".shiki/README.md is not part of the template")
        readme.write_text("STALE-LOCAL-EDIT\n", encoding="utf-8")
        shiki_installer.install_template(self.target, force=True, validate=False)
        self.assertNotIn("STALE-LOCAL-EDIT", readme.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
