"""Install version stamp: written on install, drift-reported by ``doctor``.

A uniformly-old target is internally consistent, so ``validate_shiki`` passes
and ``doctor`` reports zero failures — indistinguishable from a current target.
The install stamp is the instrument that removes that blind spot: every install
and upgrade writes a small ``.shiki/install-stamp.json`` naming the platform
commit, the install time, and a content digest per platform-owned shipped path,
and ``doctor`` reports three kinds of drift from it —

* a shipped path whose current content differs from the stamped digest
  (hand-edited or half-upgraded);
* a stamped platform commit that is not an ancestor of the running platform HEAD
  (the target is ahead, or from a different lineage);
* an absent stamp (a target installed before stamps existed) — a distinct
  finding, never a pass.

Each is a real ``doctor`` finding with its own id, so ``--strict`` fails on it,
and ``doctor`` never writes to the target.

This file also guards the migration ``--target`` fix: the global ``shiki`` execs
the PLATFORM's ``scripts/``, so ``migrate --target <other repo>`` would apply the
platform registry to the target's state and could write migration ids the
target's own validator rejects. When the target ships its own, differing
registry, ``migrate`` refuses and names the correct command.

Dependency-free ``unittest`` only; drives ``scripts/shiki.py`` via subprocess so
the assertions exercise the same exit contract an operator sees.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_installer import INSTALL_STAMP_PATH, _incoming_is_older

SHIKI_CLI = shiki_test_support.SCRIPTS_DIR / "shiki.py"
REPO_ROOT = shiki_test_support.REPO_ROOT

# A platform-owned shipped path that the stamp always digests (overwrite
# category — not project content, governance, or preserve-outright state).
SHIPPED_PATH = "scripts/shiki_git.py"
# Files the stamp must never digest: they are customized per target, so a
# difference there is expected, not drift.
CUSTOMIZABLE_PATHS = ("AGENTS.md", ".shiki/config.yaml", ".github/CODEOWNERS")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SHIKI_CLI), *args],
        capture_output=True,
        text=True,
    )


def _findings(doctor_json: str) -> dict[str, str]:
    report = json.loads(doctor_json)
    return {finding["id"]: finding["status"] for finding in report["findings"]}


def _finding(report_json: str, finding_id: str) -> dict[str, object]:
    report = json.loads(report_json)
    for finding in report["findings"]:
        if finding["id"] == finding_id:
            return finding
    raise AssertionError(f"missing doctor finding {finding_id}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_snapshot(root: Path) -> dict[str, str]:
    """Digest every file under ``root`` except Python bytecode cache."""
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            snapshot[str(path.relative_to(root))] = _sha256(path)
    return snapshot


class InstallStampTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-install-stamp-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "target"
        self.target.mkdir()
        install = _run("install-target", str(self.target), "--local-only", "--no-validate")
        self.assertEqual(install.returncode, 0, install.stderr)
        self.stamp_path = self.target / INSTALL_STAMP_PATH

    def _read_stamp(self) -> dict:
        return json.loads(self.stamp_path.read_text(encoding="utf-8"))

    def _doctor(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return _run("doctor", "--json", "--target", str(self.target), *extra)

    # -- stamp shape -------------------------------------------------------

    def test_fresh_install_writes_a_stamp(self) -> None:
        self.assertTrue(self.stamp_path.is_file(), "install must write an install stamp")
        stamp = self._read_stamp()
        self.assertEqual(stamp["version"], 1)

        # Names the platform commit the install came from.
        head = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        self.assertEqual(head.returncode, 0, head.stderr)
        self.assertEqual(stamp["platform_commit"], head.stdout.strip())

        # Names the install timestamp.
        self.assertIsInstance(stamp["installed_at"], str)
        self.assertTrue(stamp["installed_at"])

        # Names a content digest per shipped path — platform-owned paths only.
        digests = stamp["digests"]
        self.assertIn(SHIPPED_PATH, digests)
        self.assertEqual(digests[SHIPPED_PATH], _sha256(self.target / SHIPPED_PATH))
        for customizable in CUSTOMIZABLE_PATHS:
            self.assertNotIn(
                customizable, digests, f"{customizable} is customized per target; it must not be stamped"
            )

    def test_stamp_is_not_committable(self) -> None:
        # The untracked stamp must never reach a commit: validate rejects an
        # unknown tracked .shiki path, and the loop stages with `git add -A`.
        subprocess.run(["git", "-C", str(self.target), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.target), "add", "."], check=True)
        tracked = subprocess.run(
            ["git", "-C", str(self.target), "ls-files"], capture_output=True, text=True,
        ).stdout
        self.assertNotIn(INSTALL_STAMP_PATH, tracked.splitlines())
        # The target's own validator still passes with the stamp on disk.
        validate = subprocess.run(
            ["python3", "scripts/validate_shiki.py"], cwd=str(self.target),
            capture_output=True, text=True,
        )
        self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)

    def test_upgrade_refreshes_the_stamp(self) -> None:
        # A stale stamp (older platform content: a wrong recorded digest and an
        # old timestamp) is refreshed by a --force upgrade.
        stamp = self._read_stamp()
        stamp["installed_at"] = "2000-01-01T00:00:00+00:00"
        stamp["digests"][SHIPPED_PATH] = "deadbeef"
        self.stamp_path.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        upgrade = _run("install-target", str(self.target), "--local-only", "--no-validate", "--force")
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)

        refreshed = self._read_stamp()
        self.assertNotEqual(refreshed["installed_at"], "2000-01-01T00:00:00+00:00")
        self.assertEqual(refreshed["digests"][SHIPPED_PATH], _sha256(self.target / SHIPPED_PATH))

    # -- drift reporting ---------------------------------------------------

    def test_no_drift_on_untouched_fresh_install(self) -> None:
        statuses = _findings(self._doctor().stdout)
        self.assertEqual(statuses["doctor.install_stamp.present"], "pass")
        self.assertEqual(statuses["doctor.install_stamp.content"], "pass")
        self.assertEqual(statuses["doctor.install_stamp.platform_commit"], "pass")

    def test_editing_a_shipped_file_is_reported_naming_that_path(self) -> None:
        edited = self.target / SHIPPED_PATH
        edited.write_text(edited.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")

        finding = _finding(self._doctor().stdout, "doctor.install_stamp.content")
        self.assertEqual(finding["status"], "warn")
        self.assertIn(SHIPPED_PATH, finding["summary"])
        self.assertIn(SHIPPED_PATH, finding["details"]["drifted"])

    def test_absent_stamp_is_a_distinct_finding_never_a_pass(self) -> None:
        self.stamp_path.unlink()
        report = self._doctor().stdout
        statuses = _findings(report)
        self.assertEqual(statuses["doctor.install_stamp.present"], "warn")
        self.assertNotEqual(statuses["doctor.install_stamp.present"], "pass")
        # Content/commit cannot be judged without a stamp, but are never a pass.
        self.assertEqual(statuses["doctor.install_stamp.content"], "skip")
        self.assertEqual(statuses["doctor.install_stamp.platform_commit"], "skip")

    def test_newer_platform_commit_is_reported_not_silently_accepted(self) -> None:
        # A stamp naming a commit that is not an ancestor of the running platform
        # HEAD (target ahead, or a different lineage) is reported.
        stamp = self._read_stamp()
        stamp["platform_commit"] = "0" * 40  # not an ancestor of any real HEAD
        self.stamp_path.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        finding = _finding(self._doctor().stdout, "doctor.install_stamp.platform_commit")
        self.assertEqual(finding["status"], "warn")
        self.assertIn("not an ancestor", finding["summary"])

    def test_strict_fails_on_drift(self) -> None:
        edited = self.target / SHIPPED_PATH
        edited.write_text(edited.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")
        self.assertEqual(_findings(self._doctor().stdout)["doctor.install_stamp.content"], "warn")
        self.assertNotEqual(self._doctor("--strict").returncode, 0)

    def test_strict_fails_on_absent_stamp(self) -> None:
        self.stamp_path.unlink()
        self.assertEqual(_findings(self._doctor().stdout)["doctor.install_stamp.present"], "warn")
        self.assertNotEqual(self._doctor("--strict").returncode, 0)

    def test_doctor_never_writes_to_the_target(self) -> None:
        # Prime bytecode caches with one run, then assert a second run mutates
        # no target content (the stamp included).
        self._doctor()
        before = _content_snapshot(self.target)
        self._doctor()
        after = _content_snapshot(self.target)
        self.assertEqual(before, after, "doctor must not write to the target")


class OlderContentGuardTests(unittest.TestCase):
    """A later install must never overwrite a stamp with older content."""

    def _rev(self, ref: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", ref],
            capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def test_incoming_older_lineage_is_preserved_not_overwritten(self) -> None:
        head = self._rev("HEAD")
        parent = self._rev("HEAD~1")
        if not head or not parent:
            self.skipTest("repository has no HEAD~1 to compare lineage against")
        # Incoming (an ancestor) is older than the recorded stamp -> preserve.
        self.assertTrue(_incoming_is_older(parent, head))
        # Incoming (a descendant) is newer -> refresh.
        self.assertFalse(_incoming_is_older(head, parent))
        # Same commit or an unknown commit is never treated as older.
        self.assertFalse(_incoming_is_older(head, head))
        self.assertFalse(_incoming_is_older("0" * 40, head))
        self.assertFalse(_incoming_is_older(None, head))


class MigrateForeignTargetTests(unittest.TestCase):
    """`migrate --target <other repo>` must resolve the target's own registry or
    refuse; a fresh (identical-registry) target is never refused."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-migrate-foreign-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _install(self, name: str) -> Path:
        target = self.tmp / name
        target.mkdir()
        install = _run("install-target", str(target), "--local-only", "--no-validate")
        self.assertEqual(install.returncode, 0, install.stderr)
        return target

    def test_fresh_identical_registry_target_is_not_refused(self) -> None:
        target = self._install("fresh")
        result = _run("migrate", "status", "--json", "--target", str(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("refusing to run the platform migration registry", result.stderr)

    def test_foreign_registry_target_is_refused_with_the_correct_command(self) -> None:
        target = self._install("foreign")
        foreign_module = target / "scripts" / "shiki_migrations.py"
        foreign_module.write_text(
            foreign_module.read_text(encoding="utf-8") + "\n# foreign platform divergence\n",
            encoding="utf-8",
        )

        for command in ("status", "apply"):
            result = _run("migrate", command, "--json", "--target", str(target))
            self.assertNotEqual(result.returncode, 0, f"migrate {command} must be refused")
            self.assertIn("[shiki] error:", result.stderr)
            self.assertIn("refusing to run the platform migration registry", result.stderr)
            # Names the correct command: run the target's own CLI.
            self.assertRegex(
                result.stderr,
                re.compile(rf"cd \S*foreign && python3 scripts/shiki\.py migrate {command}"),
            )
            self.assertNotIn("Traceback (most recent call last)", result.stderr)


if __name__ == "__main__":
    unittest.main()
