"""SADR installation and collision-safe legacy upgrade behavior."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_installer
from tests.test_adr_references import find_adr_reference_violations


PRODUCT_NUMBERS = tuple(range(13, 19))


def _quiet_install(target: Path, *, force: bool, validate: bool) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        shiki_installer.install_template(target, force=force, validate=validate)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_sadr_paths() -> tuple[str, ...]:
    return tuple(
        f"docs/adr/{path.name.removeprefix('SADR-')}"
        for path in sorted((shiki_installer.ROOT / "docs" / "adr").glob("SADR-*.md"))
        if path.name != "SADR-0019-separate-platform-sadr-and-target-adr-namespaces.md"
    )


def _create_legacy_upgrade_fixture(
    target: Path,
) -> tuple[dict[str, bytes], tuple[str, ...]]:
    adr_dir = target / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    product_bytes = {
        f"{number:04d}-cubeshare-product-decision.md": (
            f"# CubeShare ADR {number:04d}\n\nDo not rewrite {number}.\n"
        ).encode()
        for number in PRODUCT_NUMBERS
    }
    for name, content in product_bytes.items():
        (adr_dir / name).write_bytes(content)

    legacy_paths = _legacy_sadr_paths()
    for relative in legacy_paths:
        (target / relative).write_bytes(f"legacy Shiki bytes for {relative}\n".encode())

    stamp_path = target / shiki_installer.INSTALL_STAMP_PATH
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = {
        "version": shiki_installer.INSTALL_STAMP_VERSION,
        "platform_commit": None,
        "installed_at": "2000-01-01T00:00:00+00:00",
        "digests": {
            relative: _sha256(target / relative) for relative in legacy_paths
        },
    }
    stamp_path.write_text(
        json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return product_bytes, legacy_paths


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    """Recursive byte/shape snapshot used to prove preflight is write-free."""
    snapshot: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", path.readlink().as_posix())
        elif path.is_dir():
            snapshot[relative] = ("directory", "")
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


class FreshSadrInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-sadr-fresh-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "target"
        self.adr_dir = self.target / "docs" / "adr"
        self.adr_dir.mkdir(parents=True)
        self.product_bytes = {
            f"{number:04d}-product-decision.md": (
                f"# Product ADR {number:04d}\n\nTarget-owned bytes {number}.\n"
            ).encode()
            for number in PRODUCT_NUMBERS
        }
        for name, content in self.product_bytes.items():
            (self.adr_dir / name).write_bytes(content)

    def test_fresh_install_preserves_product_adrs_and_installs_all_sadrs(self) -> None:
        _quiet_install(self.target, force=False, validate=True)

        for name, expected in self.product_bytes.items():
            self.assertEqual((self.adr_dir / name).read_bytes(), expected)

        sadr_numbers = {
            path.name.split("-", 2)[1]
            for path in self.adr_dir.glob("SADR-[0-9][0-9][0-9][0-9]-*.md")
        }
        self.assertEqual(sadr_numbers, {f"{number:04d}" for number in range(1, 20)})

        numeric_records = {
            path.name
            for path in self.adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md")
        }
        self.assertEqual(numeric_records, set(self.product_bytes))
        stamp = json.loads(
            (self.target / shiki_installer.INSTALL_STAMP_PATH).read_text(
                encoding="utf-8"
            )
        )
        for path in self.adr_dir.glob("SADR-[0-9][0-9][0-9][0-9]-*.md"):
            relative = path.relative_to(self.target).as_posix()
            self.assertEqual(stamp["digests"][relative], _sha256(path))
        self.assertEqual(find_adr_reference_violations(self.target), [])


class LegacySadrUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-sadr-upgrade-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "target"
        self.adr_dir = self.target / "docs" / "adr"
        self.product_bytes, self.legacy_paths = _create_legacy_upgrade_fixture(
            self.target
        )

    def test_force_upgrade_deletes_only_stamped_legacy_sadrs(self) -> None:
        _quiet_install(self.target, force=True, validate=True)

        for relative in self.legacy_paths:
            self.assertFalse((self.target / relative).exists(), relative)
        for name, expected in self.product_bytes.items():
            self.assertEqual((self.adr_dir / name).read_bytes(), expected)

        sadrs = list(self.adr_dir.glob("SADR-[0-9][0-9][0-9][0-9]-*.md"))
        self.assertEqual(len(sadrs), 19)
        refreshed = json.loads(
            (self.target / shiki_installer.INSTALL_STAMP_PATH).read_text(encoding="utf-8")
        )
        for relative in self.legacy_paths:
            self.assertNotIn(relative, refreshed["digests"])
        for path in sadrs:
            relative = path.relative_to(self.target).as_posix()
            self.assertEqual(refreshed["digests"][relative], _sha256(path))

    def test_non_force_install_never_deletes_legacy_files(self) -> None:
        before = {
            relative: (self.target / relative).read_bytes()
            for relative in self.legacy_paths
        }
        _quiet_install(self.target, force=False, validate=False)
        for relative, expected in before.items():
            self.assertEqual((self.target / relative).read_bytes(), expected)
        refreshed = json.loads(
            (self.target / shiki_installer.INSTALL_STAMP_PATH).read_text(encoding="utf-8")
        )
        for relative in self.legacy_paths:
            self.assertEqual(refreshed["digests"][relative], _sha256(self.target / relative))


class LegacyCleanupPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-sadr-preflight-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_all_ownership_blockers_abort_before_any_write(self) -> None:
        cases = (
            ("absent", "install stamp is absent"),
            ("unreadable", "install stamp is unreadable"),
            ("malformed", "install stamp is malformed"),
            ("missing-digest", "install stamp has no digest"),
            ("digest-mismatch", "digest mismatch"),
        )
        for name, expected_reason in cases:
            with self.subTest(name=name):
                target = self.tmp / name
                _, legacy_paths = _create_legacy_upgrade_fixture(target)
                stamp_path = target / shiki_installer.INSTALL_STAMP_PATH

                if name == "absent":
                    stamp_path.unlink()
                elif name == "unreadable":
                    stamp_path.unlink()
                    stamp_path.mkdir()
                elif name == "malformed":
                    stamp_path.write_text("{not-json\n", encoding="utf-8")
                else:
                    stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
                    if name == "missing-digest":
                        stamp["digests"] = {}
                    else:
                        stamp["digests"] = {
                            relative: "deadbeef" for relative in legacy_paths
                        }
                    stamp_path.write_text(
                        json.dumps(stamp, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

                before = _tree_snapshot(target)
                with self.assertRaises(shiki_installer.ShikiError) as raised:
                    _quiet_install(target, force=True, validate=False)
                after = _tree_snapshot(target)

                self.assertEqual(before, after, f"{name} preflight changed the tree")
                message = str(raised.exception)
                self.assertIn(expected_reason, message)
                self.assertIn("No files were written", message)
                for relative in legacy_paths:
                    self.assertIn(relative, message)

    def test_absent_stamp_is_allowed_when_no_legacy_path_exists(self) -> None:
        target = self.tmp / "no-legacy"
        target.mkdir()
        _quiet_install(target, force=True, validate=False)
        self.assertTrue(
            (target / "docs" / "adr" / "SADR-0019-separate-platform-sadr-and-target-adr-namespaces.md").is_file()
        )


if __name__ == "__main__":
    unittest.main()
