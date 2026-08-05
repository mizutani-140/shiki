"""Three-way ``--force`` install semantics: nothing lost, nothing silently stale.

These tests import the real ``scripts/`` modules and assert their observed
behavior against the upgrade contract for T-...2411811e. Dependency-free
``unittest`` only.

The three-way split under ``--force`` is:

* PROJECT CONTENT (``CONTEXT.md``, ``AGENTS.md``, ``CLAUDE.md``,
  ``.github/CODEOWNERS``) is never overwritten; the incoming template is written
  alongside as ``<file>.new`` and reported.
* GOVERNANCE CONTRACT (``.shiki/config.yaml``, ``.shiki/guardian-policy.json``)
  is never silently kept; ``<file>.new`` is written and a per-key summary names
  what differs, calling out ``mergegate.required_checks`` and ``approval_sources``.
* ``.shiki/migrations/state.json`` is preserved outright (no ``.new``).
* Everything else is overwritten under ``--force``.

A target with pending migrations is refused with NO file written.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_contracts
import shiki_installer
import validate_shiki
from shiki_migrations import MEMORIES_MIGRATION_ID, migration_status
from shiki_process import ShikiError

PROJECT_CONTENT = ("CONTEXT.md", "AGENTS.md", "CLAUDE.md", ".github/CODEOWNERS")
GOVERNANCE = (".shiki/config.yaml", ".shiki/guardian-policy.json")
MIGRATION_STATE = ".shiki/migrations/state.json"

# A config.yaml whose mergegate.required_checks differs from the shipped template
# (the platform ships four checks; this keeps only one), parseable by the same
# subset parser bootstrap uses.
MODIFIED_CONFIG = (
    "mergegate:\n"
    "  required_checks:\n"
    "    - Validate Shiki mirror\n"
)
# A guardian-policy.json whose approval_sources differs from the shipped template.
MODIFIED_GUARDIAN = json.dumps(
    {"version": 1, "approval_sources": {"github_review": {"enabled": False}}},
    indent=2,
) + "\n"


@contextlib.contextmanager
def _captured_output():
    info_lines: list[str] = []
    warn_lines: list[str] = []
    orig_info = shiki_installer.info
    orig_warn = shiki_installer.warn
    shiki_installer.info = lambda message, *a, **k: info_lines.append(str(message))
    shiki_installer.warn = lambda message, *a, **k: warn_lines.append(str(message))
    try:
        yield info_lines, warn_lines
    finally:
        shiki_installer.info = orig_info
        shiki_installer.warn = orig_warn


def _hash_tree(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


class ForceSemanticsTests(unittest.TestCase):
    """A fully installed target, then a ``--force`` refresh over it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-force-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "target"
        self.target.mkdir()
        # Fresh install (no --force): lays down the full template surface.
        with _captured_output():
            shiki_installer.install_template(self.target, force=False, validate=False)

    def _template_text(self, relative: str) -> str:
        return (shiki_installer.ROOT / relative).read_text(encoding="utf-8")

    def test_without_force_nothing_existing_changes(self) -> None:
        before = _hash_tree(self.target)
        with _captured_output():
            shiki_installer.install_template(self.target, force=False, validate=False)
        after = _hash_tree(self.target)
        self.assertEqual(before, after, "a no-force re-run must change nothing")
        self.assertFalse(
            any(p.name.endswith(".new") for p in self.target.rglob("*")),
            "no .new file may be written without --force",
        )

    def test_force_keeps_project_content_and_writes_new_per_file(self) -> None:
        for relative in PROJECT_CONTENT:
            (self.target / relative).write_text(f"LOCAL EDIT {relative}\n", encoding="utf-8")

        with _captured_output() as (info_lines, _):
            shiki_installer.install_template(self.target, force=True, validate=False)

        for relative in PROJECT_CONTENT:
            self.assertEqual(
                (self.target / relative).read_text(encoding="utf-8"),
                f"LOCAL EDIT {relative}\n",
                f"{relative} must not be overwritten by --force",
            )
            new_path = self.target / (relative + ".new")
            self.assertTrue(new_path.exists(), f"{relative}.new must be written")
            self.assertEqual(
                new_path.read_text(encoding="utf-8"),
                self._template_text(relative),
                f"{relative}.new must carry the incoming template",
            )
            self.assertTrue(
                any(f"{relative}.new" in line for line in info_lines),
                f"summary must list {relative}.new",
            )

    def test_force_keeps_governance_writes_new_and_names_keys(self) -> None:
        (self.target / ".shiki" / "config.yaml").write_text(MODIFIED_CONFIG, encoding="utf-8")
        (self.target / ".shiki" / "guardian-policy.json").write_text(
            MODIFIED_GUARDIAN, encoding="utf-8"
        )

        with _captured_output() as (info_lines, _):
            shiki_installer.install_template(self.target, force=True, validate=False)

        # Content kept; .new written with the incoming template.
        self.assertEqual(
            (self.target / ".shiki" / "config.yaml").read_text(encoding="utf-8"),
            MODIFIED_CONFIG,
        )
        self.assertEqual(
            (self.target / ".shiki" / "guardian-policy.json").read_text(encoding="utf-8"),
            MODIFIED_GUARDIAN,
        )
        for relative in GOVERNANCE:
            new_path = self.target / (relative + ".new")
            self.assertTrue(new_path.exists(), f"{relative}.new must be written")
            self.assertEqual(
                new_path.read_text(encoding="utf-8"),
                self._template_text(relative),
            )

        summary = "\n".join(info_lines)
        # The two governance files appear in the summary with the critical
        # differing keys named explicitly.
        config_line = next(
            (line for line in info_lines if ".shiki/config.yaml.new" in line), ""
        )
        guardian_line = next(
            (line for line in info_lines if ".shiki/guardian-policy.json.new" in line), ""
        )
        self.assertIn("mergegate.required_checks", config_line)
        self.assertIn("approval_sources", guardian_line)
        self.assertIn(".shiki/config.yaml.new", summary)
        self.assertIn(".shiki/guardian-policy.json.new", summary)

    def test_force_preserves_migration_state_without_new(self) -> None:
        state_before = (self.target / MIGRATION_STATE).read_text(encoding="utf-8")
        with _captured_output():
            shiki_installer.install_template(self.target, force=True, validate=False)
        self.assertEqual(
            (self.target / MIGRATION_STATE).read_text(encoding="utf-8"),
            state_before,
            "migration state must be preserved outright",
        )
        self.assertFalse(
            (self.target / (MIGRATION_STATE + ".new")).exists(),
            "migration state must not gain a .new",
        )

    def test_force_overwrites_every_other_shipped_file(self) -> None:
        overwritten = (".shiki/README.md", "SYSTEM_PROMPT.md")
        for relative in overwritten:
            (self.target / relative).write_text("STALE LOCAL EDIT\n", encoding="utf-8")

        with _captured_output():
            shiki_installer.install_template(self.target, force=True, validate=False)

        for relative in overwritten:
            self.assertEqual(
                (self.target / relative).read_text(encoding="utf-8"),
                self._template_text(relative),
                f"{relative} must be overwritten under --force",
            )
            self.assertFalse(
                (self.target / (relative + ".new")).exists(),
                f"{relative} must not gain a .new (it is overwritten, not preserved)",
            )

    def test_summary_lists_every_new_written(self) -> None:
        for relative in PROJECT_CONTENT:
            (self.target / relative).write_text("edit\n", encoding="utf-8")
        (self.target / ".shiki" / "config.yaml").write_text(MODIFIED_CONFIG, encoding="utf-8")
        (self.target / ".shiki" / "guardian-policy.json").write_text(
            MODIFIED_GUARDIAN, encoding="utf-8"
        )

        with _captured_output() as (info_lines, _):
            shiki_installer.install_template(self.target, force=True, validate=False)

        summary = "\n".join(info_lines)
        for relative in (*PROJECT_CONTENT, *GOVERNANCE):
            self.assertIn(f"{relative}.new", summary, f"summary omitted {relative}.new")


class PendingMigrationRefusalTests(unittest.TestCase):
    """A target whose valid migration state is behind the registry is refused
    with NO file written."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-pending-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.target = self.tmp / "target"
        (self.target / ".shiki" / "migrations").mkdir(parents=True)

        # Take the real, valid migration state and drop the last applied
        # migration so exactly one is pending while the state stays valid.
        real = json.loads(
            (shiki_installer.ROOT / MIGRATION_STATE).read_text(encoding="utf-8")
        )
        real["applied"] = [
            record for record in real["applied"] if record.get("id") != MEMORIES_MIGRATION_ID
        ]
        (self.target / MIGRATION_STATE).write_text(
            json.dumps(real, indent=2) + "\n", encoding="utf-8"
        )
        # Pre-existing project + governance content that must stay untouched.
        (self.target / "CONTEXT.md").write_text("do not touch\n", encoding="utf-8")

    def test_pending_state_is_actually_pending(self) -> None:
        status = migration_status(self.target)
        self.assertTrue(status["state_exists"])
        self.assertEqual(status["errors"], [], "the behind state must stay valid")
        self.assertIn(MEMORIES_MIGRATION_ID, status["pending"])

    def test_refused_with_nothing_written(self) -> None:
        before = _hash_tree(self.target)
        with _captured_output():
            with self.assertRaises(ShikiError):
                shiki_installer.install_template(self.target, force=True, validate=False)
        after = _hash_tree(self.target)
        self.assertEqual(before, after, "no file may be written when refused")
        self.assertFalse(
            any(p.name.endswith(".new") for p in self.target.rglob("*")),
            "no .new file may be written when refused",
        )


class CodeownersResolverTests(unittest.TestCase):
    """The required CODEOWNERS owner resolves per target, not hardcoded."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shiki-codeowners-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _target_with_repo(self, repo: str | None) -> Path:
        target = self.tmp / (repo.replace("/", "-") if repo else "no-repo")
        (target / ".shiki").mkdir(parents=True)
        if repo is not None:
            (target / ".shiki" / "repo.json").write_text(
                json.dumps({"repo": repo, "source_of_truth": "github"}) + "\n",
                encoding="utf-8",
            )
        return target

    def test_owner_resolves_from_repo_json(self) -> None:
        target = self._target_with_repo("acme-org/widgets")
        self.assertEqual(
            shiki_contracts.codeowners_required_owner(target), "@acme-org"
        )

    def test_owner_defaults_when_repo_json_absent(self) -> None:
        target = self._target_with_repo(None)
        self.assertEqual(
            shiki_contracts.codeowners_required_owner(target),
            shiki_contracts.CODEOWNERS_REQUIRED_OWNER,
        )

    def test_this_repository_owner_is_unchanged(self) -> None:
        # This platform repository has no per-target repo.json, so it keeps the
        # documented default owner.
        target = self._target_with_repo("mizutani-140/shiki")
        self.assertEqual(
            shiki_contracts.codeowners_required_owner(target), "@mizutani-140"
        )
        self.assertEqual(shiki_contracts.CODEOWNERS_REQUIRED_OWNER, "@mizutani-140")

    def test_installed_foreign_owner_target_passes_wired_gate(self) -> None:
        # Asserting the resolver's return value is not enough: a foreign target
        # whose repo.json is present at install time gets a CODEOWNERS naming its
        # own owner, and that installed file passes the wired validate_shiki gate.
        target = self._target_with_repo("acme-org/widgets")
        with _captured_output():
            shiki_installer.install_template(target, force=False, validate=False)

        codeowners = (target / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        self.assertIn("@acme-org", codeowners)
        self.assertNotIn(shiki_contracts.CODEOWNERS_REQUIRED_OWNER, codeowners)

        # No exception == the wired gate accepts the foreign target's own owner.
        validate_shiki.validate_codeowners_governance(target)


if __name__ == "__main__":
    unittest.main()
