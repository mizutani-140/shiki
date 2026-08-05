"""The per-target CODEOWNERS owner is wired into both enforcement sites.

``scripts/shiki_contracts.codeowners_required_owner`` resolves the required
CODEOWNERS owner from a target's ``.shiki/repo.json`` (falling back to the
documented ``CODEOWNERS_REQUIRED_OWNER`` constant when no owner can be
resolved). These tests prove that resolver is actually *consumed* by the two
places that enforce ownership — ``validate_shiki.validate_codeowners_governance``
and ``shiki_doctor._codeowners_findings`` — and that the installer writes a
CODEOWNERS naming the target's own owner.

Asserting the resolver's return value is not sufficient: a foreign target must
pass the real gate and the real doctor finding end to end. Dependency-free
``unittest`` only.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_contracts
import shiki_doctor
import shiki_installer
import validate_shiki

REPO_ROOT = shiki_test_support.REPO_ROOT
CODEOWNERS_REL = shiki_contracts.CODEOWNERS_PATH


@contextlib.contextmanager
def _silence_installer():
    orig_info = shiki_installer.info
    orig_warn = shiki_installer.warn
    shiki_installer.info = lambda *a, **k: None
    shiki_installer.warn = lambda *a, **k: None
    try:
        yield
    finally:
        shiki_installer.info = orig_info
        shiki_installer.warn = orig_warn


def _codeowners_status(target: Path) -> str:
    findings = shiki_doctor._codeowners_findings(target)
    coverage = [f for f in findings if f.id == "doctor.codeowners.coverage"]
    if not coverage:
        raise AssertionError("doctor emitted no doctor.codeowners.coverage finding")
    return coverage[0].status


class CodeownersWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-codeowners-wiring-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _write_repo_json(self, target: Path, repo: str) -> None:
        (target / ".shiki").mkdir(parents=True, exist_ok=True)
        (target / ".shiki" / "repo.json").write_text(
            json.dumps(
                {
                    "source_of_truth": "github",
                    "default_branch": "main",
                    "mirror": ".shiki",
                    "provider": "github",
                    "repo": repo,
                    "host": "github.com",
                    "remote_protocol": "https",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _install(self, name: str, repo: str | None) -> Path:
        """Install a full target; when ``repo`` is set, ``repo.json`` is present
        BEFORE install so the installer substitutes the owner into CODEOWNERS."""
        target = self.tmp / name
        target.mkdir()
        if repo is not None:
            self._write_repo_json(target, repo)
        with _silence_installer():
            shiki_installer.install_template(target, force=False, validate=False)
        return target

    # -- the load-bearing case ------------------------------------------------

    def test_foreign_owner_target_passes_both_enforcement_sites(self) -> None:
        target = self._install("acme", "acme-org/widgets")

        codeowners = (target / CODEOWNERS_REL).read_text(encoding="utf-8")
        self.assertIn("@acme-org", codeowners)
        self.assertNotIn("@mizutani-140", codeowners)

        # The gate accepts it (no exception) ...
        validate_shiki.validate_codeowners_governance(target)
        # ... and so does the doctor finding.
        self.assertEqual(_codeowners_status(target), "pass")

    # -- a mismatched owner fails both ---------------------------------------

    def test_wrong_owner_fails_both_enforcement_sites(self) -> None:
        target = self._install("mismatch", "acme-org/widgets")
        # Overwrite the installed CODEOWNERS so it names the wrong owner while
        # repo.json still resolves to @acme-org.
        original = (target / CODEOWNERS_REL).read_text(encoding="utf-8")
        (target / CODEOWNERS_REL).write_text(
            original.replace("@acme-org", "@someone-else"), encoding="utf-8"
        )

        with self.assertRaises(validate_shiki.ValidationError):
            validate_shiki.validate_codeowners_governance(target)
        self.assertEqual(_codeowners_status(target), "fail")

    # -- no resolvable owner falls back to the constant ----------------------

    def test_no_repo_json_falls_back_to_constant(self) -> None:
        # No repo.json at install time: the shipped owner is written verbatim,
        # and both sites resolve to the documented fallback constant.
        target = self._install("plain", None)
        codeowners = (target / CODEOWNERS_REL).read_text(encoding="utf-8")
        self.assertIn(shiki_contracts.CODEOWNERS_REQUIRED_OWNER, codeowners)

        # Fallback is never "anything passes": the shipped file names the
        # constant, so it passes; a foreign CODEOWNERS with no repo.json fails.
        validate_shiki.validate_codeowners_governance(target)
        self.assertEqual(_codeowners_status(target), "pass")

        (target / CODEOWNERS_REL).write_text(
            codeowners.replace(
                shiki_contracts.CODEOWNERS_REQUIRED_OWNER, "@not-the-maintainer"
            ),
            encoding="utf-8",
        )
        with self.assertRaises(validate_shiki.ValidationError):
            validate_shiki.validate_codeowners_governance(target)
        self.assertEqual(_codeowners_status(target), "fail")

    # -- this repository's own result is unchanged ---------------------------

    def test_this_repository_gate_and_doctor_unchanged(self) -> None:
        # The platform repo has no per-target repo.json, so both sites keep the
        # documented default owner and continue to pass.
        validate_shiki.validate_codeowners_governance()  # default root == ROOT
        validate_shiki.validate_codeowners_governance(REPO_ROOT)
        self.assertEqual(_codeowners_status(REPO_ROOT), "pass")

    # -- the resolver is actually consumed by production code -----------------

    def test_installer_writes_target_owner(self) -> None:
        target = self._install("installer", "octo-inc/service")
        codeowners = (target / CODEOWNERS_REL).read_text(encoding="utf-8")
        # Every governance rule names the target owner, not the maintainer.
        for line in codeowners.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertIn("@octo-inc", stripped)
        self.assertNotIn("@mizutani-140", codeowners)
        # The platform's own copy is untouched by a target install.
        self.assertIn(
            shiki_contracts.CODEOWNERS_REQUIRED_OWNER,
            (REPO_ROOT / CODEOWNERS_REL).read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
