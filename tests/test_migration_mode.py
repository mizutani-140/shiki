"""Migration PR mode: deliver a repository-wide mirror change as a re-run proof.

A change that must touch every record in the mirror (e.g. backfilling a field into
all 100+ task files) cannot be delivered through any goal-shaped MergeGate mode.
contract / goal_reconcile / amendment are each scoped to a SINGLE goal's records,
and ``enforce_untrusted_shiki_mutations`` rejects a foreign task file independently
of the task's declared locks — so a repository-wide change, which has no goal, has
no route. This was measured: 104 of 112 task files lacked ``cca_checklist_profile``
and the promotion task that wanted to require it could not backfill them.

Migration mode is that route: a two-factor (marker + maintainer label) PR whose
entire proof obligation is that its ``.shiki`` diff EQUALS the output of re-running
a migration REGISTERED ON THE BASE BRANCH against a copy of the base snapshot,
file for file and byte for byte.

The load-bearing property — the reason a PR cannot rubber-stamp its own diff — is
that the migration function is resolved and executed from the BASE branch's
``scripts/shiki_migrations.py``, never the PR head's. A PR that carries its own new
migration definition and declares it does NOT pass, because the id is absent from
base. These tests drive the real gate (``enforce_migration`` /
``migration_decision`` / ``resolve_base_migration_registry``) directly, including
one end-to-end case that resolves the base registry from a REAL git repository.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from mergegate_check import (
    ChangedFile,
    declared_migration_ids,
    enforce_migration,
    migration_decision,
    resolve_base_migration_registry,
)
from shiki_migrations import MIGRATION_SOURCE_OF_TRUTH, Migration

MIG_ID = "M-20260801-0001-cca-profile-backfill"
BASELINE = "M-20260604-0001-baseline"
GOAL = "G-20260801T000000000000Z-0000aaaa"
T1 = "T-20260801T000000000000Z-0000a001"
T2 = "T-20260801T000000000000Z-0000a002"
T3 = "T-20260801T000000000000Z-0000a003"
TASK_IDS = (T1, T2, T3)


def _canonical(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _backfill_apply(root, dry_run: bool) -> dict:
    """A deterministic, file-transforming migration: add an empty
    ``cca_checklist_profile`` to every task file that lacks one. The exact same
    callable seeds the head fixture and is executed by the gate against a base
    copy, so a conforming head is byte-identical to the re-run by construction."""
    tasks_dir = Path(root) / ".shiki" / "tasks"
    changed: list[str] = []
    for path in sorted(tasks_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "cca_checklist_profile" not in data:
            data["cca_checklist_profile"] = []
            if not dry_run:
                path.write_text(_canonical(data), encoding="utf-8")
            changed.append(path.name)
    return {"summary": f"backfilled {len(changed)} task file(s)", "evidence": []}


def _migration(*, migration_id: str = MIG_ID, destructive: bool = False, apply=_backfill_apply) -> Migration:
    return Migration(
        id=migration_id,
        title="Backfill cca_checklist_profile",
        description="Add an empty cca_checklist_profile to every task file that lacks one.",
        introduced_in="T-20260801T000000000000Z-0000a001",
        destructive=destructive,
        apply=apply,
    )


def _registry(**kwargs) -> tuple[Migration, ...]:
    return (_migration(**kwargs),)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(data), encoding="utf-8")


def _base_task(task_id: str) -> dict:
    return {"id": task_id, "goal_id": GOAL, "title": task_id, "status": "planned"}


def _seed_base(base_root: Path, *, applied: list[str] | None = None) -> None:
    """A base ``.shiki`` snapshot with task files that lack cca_checklist_profile,
    plus a migration state file recording ``applied`` (baseline only by default)."""
    shiki = base_root / ".shiki"
    for task_id in TASK_IDS:
        _write(shiki / "tasks" / f"{task_id}.json", _base_task(task_id))
    applied_ids = applied if applied is not None else [BASELINE]
    _write(
        shiki / "migrations" / "state.json",
        {
            "version": 1,
            "source_of_truth": MIGRATION_SOURCE_OF_TRUTH,
            "applied": [
                {
                    "id": migration_id,
                    "status": "applied",
                    "applied_at": "2026-06-04T00:00:00Z",
                    "actor": "shiki-cli",
                    "summary": "applied",
                }
                for migration_id in applied_ids
            ],
        },
    )


def _seed_head_from_rerun(base_root: Path, head_root: Path) -> None:
    """The conforming head: a byte-exact copy of the base with the migration
    applied — i.e. exactly what the gate re-runs."""
    shutil.copytree(base_root / ".shiki", head_root / ".shiki")
    _backfill_apply(head_root, False)


def _conforming_changes() -> list[ChangedFile]:
    # Every task file lacked the field, so the migration modifies all three.
    return [ChangedFile("M", f".shiki/tasks/{task_id}.json") for task_id in TASK_IDS]


def _run(
    head_root: Path,
    base_root: Path,
    changes: list[ChangedFile],
    *,
    body: str | None = None,
    registry: tuple[Migration, ...] | None = None,
) -> list[str]:
    pr = {"body": body if body is not None else f"Migration: {MIG_ID}", "baseRefName": "main"}
    blocking: list[str] = []
    enforce_migration(
        target=head_root,
        pr=pr,
        base_shiki=base_root / ".shiki",
        changed_files_status=changes,
        blocking=blocking,
        warnings=[],
        base_registry=registry if registry is not None else _registry(),
    )
    return blocking


class MigrationDecisionTests(unittest.TestCase):
    MARKER = "<!-- shiki:migration -->"
    LABEL = {"name": "mergegate:migration"}

    def _body(self, *lines: str) -> str:
        # A directive marker is a bare HTML comment on its own line, as an operator
        # (and the adversarial fixtures) place it.
        return "\n".join([self.MARKER, *lines])

    def test_marker_and_label_enables_mode(self) -> None:
        mode, err = migration_decision({"body": self._body("repo-wide backfill"), "labels": [self.LABEL]})
        self.assertTrue(mode)
        self.assertIsNone(err)

    def test_marker_without_label_blocks_with_the_label_message(self) -> None:
        # The maintainer label is the independent second factor; a standalone marker
        # without it must not grant the mode. Same message shape as contract_decision.
        mode, err = migration_decision({"body": self._body("repo-wide backfill"), "labels": []})
        self.assertFalse(mode)
        self.assertIsNotNone(err)
        self.assertIn("label", err)
        self.assertIn("mergegate:migration", err)

    def test_label_without_marker_does_not_enter_mode(self) -> None:
        mode, err = migration_decision({"body": "ordinary PR", "labels": [self.LABEL]})
        self.assertFalse(mode)
        self.assertIsNone(err)

    def test_marker_quoted_in_code_span_is_not_a_directive(self) -> None:
        # A quoted marker is documentation, not intent: this feature's own task
        # scope documents `<!-- shiki:migration -->` and the goal loop inlines that
        # scope verbatim into the implementation PR body it opens for the task.
        inline = f"Entered when the `{self.MARKER}` marker is in the body."
        mode, err = migration_decision({"body": inline, "labels": []})
        self.assertFalse(mode)
        self.assertIsNone(err)
        fenced = f"```\n{self.MARKER}\n```\nordinary work"
        mode, err = migration_decision({"body": fenced, "labels": []})
        self.assertFalse(mode)
        self.assertIsNone(err)
        # Even WITH the label a quoted marker stays documentation.
        mode, err = migration_decision({"body": inline, "labels": [self.LABEL]})
        self.assertFalse(mode)
        self.assertIsNone(err)

    def test_mid_sentence_marker_in_prose_is_not_a_directive(self) -> None:
        # THE self-block regression. The goal loop inlines this task's own
        # acceptance_checks into the implementation PR body it opens, and one of them
        # names the bare marker mid-sentence ("entered ... with BOTH the
        # <!-- shiki:migration --> marker and the ... label"). A directive marker is
        # a bare comment on its OWN line, so a mid-sentence mention must NOT enter the
        # mode and — critically — must NOT fail closed on the missing label, which
        # would block this very task's implementation PR.
        prose = (
            "Migration mode is entered only with BOTH the "
            f"{self.MARKER} marker and the mergegate:migration label; the marker alone blocks."
        )
        mode, err = migration_decision({"body": prose, "labels": []})
        self.assertFalse(mode)
        self.assertIsNone(err)

    def test_this_tasks_own_impl_pr_body_does_not_enter_or_error(self) -> None:
        # End-to-end guard: build the real implementation PR body from this task's
        # stored scope + acceptance_checks (exactly as shiki_github.github_pr_body
        # does) and confirm the loop's own PR neither enters migration mode nor fails
        # closed on the missing label.
        task_path = shiki_test_support.REPO_ROOT / ".shiki" / "tasks" / "T-20260801T041653422672Z-c4815d7a.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        body = "\n".join(
            ["## Scope", task["scope"], "## Acceptance", *[f"- {c}" for c in task.get("acceptance_checks", [])]]
        )
        mode, err = migration_decision({"body": body, "labels": []})
        self.assertFalse(mode)
        self.assertIsNone(err)


class DeclaredMigrationIdTests(unittest.TestCase):
    def test_single_line_is_parsed(self) -> None:
        self.assertEqual(declared_migration_ids(f"header\nMigration: {MIG_ID}\nfooter"), [MIG_ID])

    def test_absent_line_yields_nothing(self) -> None:
        self.assertEqual(declared_migration_ids("no declaration here"), [])

    def test_mid_sentence_mention_is_not_a_declaration(self) -> None:
        # Only a line that BEGINS with `Migration:` is a directive; a backtick
        # example ("declare it as `Migration: <id>`") never starts a line.
        self.assertEqual(declared_migration_ids("declare it as `Migration: <id>` here"), [])

    def test_two_lines_are_both_returned(self) -> None:
        body = f"Migration: {MIG_ID}\nMigration: M-20260801-0002-other"
        self.assertEqual(len(declared_migration_ids(body)), 2)


class MigrationGateTests(unittest.TestCase):
    def _ctx(self):
        return tempfile.TemporaryDirectory()

    def test_conforming_pr_passes(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            blocking = _run(head, base, _conforming_changes())
        self.assertEqual(blocking, [])

    def test_extra_changed_file_blocks_naming_the_path(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            # A .shiki file the migration never touches, added to the diff and
            # written differing from base in the head.
            extra = f".shiki/goals/{GOAL}.json"
            _write(head / extra, {"id": GOAL, "status": "complete"})
            changes = _conforming_changes() + [ChangedFile("M", extra)]
            blocking = _run(head, base, changes)
        self.assertTrue(any("does not produce" in b and extra in b for b in blocking), blocking)

    def test_single_changed_byte_blocks_naming_the_path(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            # Flip one byte in one produced file so it no longer matches the re-run.
            deviant = head / ".shiki" / "tasks" / f"{T2}.json"
            data = deviant.read_bytes()
            deviant.write_bytes(data[:-2] + b"X" + data[-1:])
            blocking = _run(head, base, _conforming_changes())
        self.assertTrue(
            any("byte-for-byte" in b and f".shiki/tasks/{T2}.json" in b for b in blocking),
            blocking,
        )

    def test_missing_produced_file_blocks_naming_the_path(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            # Revert one produced file to base and drop it from the diff: the re-run
            # produces it, the PR does not include it.
            _write(head / ".shiki" / "tasks" / f"{T3}.json", _base_task(T3))
            changes = [ChangedFile("M", f".shiki/tasks/{tid}.json") for tid in (T1, T2)]
            blocking = _run(head, base, changes)
        self.assertTrue(
            any("does not include it" in b and f".shiki/tasks/{T3}.json" in b for b in blocking),
            blocking,
        )

    def test_declared_id_absent_from_base_registry_blocks(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            # The base registry knows a DIFFERENT migration; the declared id is not
            # on base. This is the effect of the security property (see the
            # real-git end-to-end case below for the self-defining attack itself).
            other = _registry(migration_id="M-20260801-0099-unrelated")
            blocking = _run(head, base, _conforming_changes(), registry=other)
        self.assertTrue(
            any("is not registered in the base branch" in b and MIG_ID in b for b in blocking),
            blocking,
        )

    def test_missing_migration_line_blocks(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            blocking = _run(head, base, _conforming_changes(), body="no declaration in this body")
        self.assertTrue(any("on its own line" in b for b in blocking), blocking)

    def test_duplicated_migration_line_blocks(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            body = f"Migration: {MIG_ID}\nMigration: M-20260801-0002-second"
            blocking = _run(head, base, _conforming_changes(), body=body)
        self.assertTrue(any("exactly one 'Migration:' line" in b for b in blocking), blocking)

    def test_already_applied_migration_blocks(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            # The migration is already recorded applied in the base state file.
            _seed_base(base, applied=[BASELINE, MIG_ID])
            _seed_head_from_rerun(base, head)
            blocking = _run(head, base, _conforming_changes())
        self.assertTrue(any("already recorded as applied" in b and MIG_ID in b for b in blocking), blocking)

    def test_destructive_migration_blocks(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            blocking = _run(head, base, _conforming_changes(), registry=_registry(destructive=True))
        self.assertTrue(any("destructive" in b and MIG_ID in b for b in blocking), blocking)

    def test_non_shiki_file_blocks(self) -> None:
        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            changes = _conforming_changes() + [ChangedFile("M", "scripts/mergegate_check.py")]
            blocking = _run(head, base, changes)
        self.assertTrue(
            any("outside .shiki/" in b and "scripts/mergegate_check.py" in b for b in blocking),
            blocking,
        )

    def test_migration_whose_apply_raises_fails_closed(self) -> None:
        # A base migration that raises while re-running must yield a named block, not
        # crash the gate: a governance check fails closed.
        def _boom(root, dry_run):
            raise RuntimeError("prerequisite missing")

        with self._ctx() as tmp:
            head, base = Path(tmp) / "head", Path(tmp) / "base"
            _seed_base(base)
            _seed_head_from_rerun(base, head)
            blocking = _run(head, base, _conforming_changes(), registry=_registry(apply=_boom))
        self.assertTrue(any("failed" in b and MIG_ID in b for b in blocking), blocking)

    def test_missing_base_snapshot_blocks(self) -> None:
        with self._ctx() as tmp:
            head = Path(tmp) / "head"
            (head / ".shiki").mkdir(parents=True)
            blocking: list[str] = []
            enforce_migration(
                target=head,
                pr={"body": f"Migration: {MIG_ID}", "baseRefName": "main"},
                base_shiki=Path(tmp) / "does-not-exist" / ".shiki",
                changed_files_status=_conforming_changes(),
                blocking=blocking,
                warnings=[],
                base_registry=_registry(),
            )
        self.assertTrue(any("requires a base .shiki snapshot" in b for b in blocking), blocking)


# The base migrations module a real git repo commits on its base branch. Minimal
# and self-contained (no ``shiki_process`` import) so ``resolve_base_migration_registry``
# can exec it without any dependency on the running checkout.
_BASE_MIGRATIONS_SRC = '''
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Migration:
    id: str
    destructive: bool = False
    apply: "Callable[..., Any] | None" = None


def _noop(root, dry_run):
    return {"summary": "noop", "evidence": []}


def migration_registry():
    return (Migration(id="M-20260604-0001-baseline", apply=_noop),)


if __name__ == "__main__":
    raise SystemExit(0)
'''

# The head module a self-defining-migration PR would carry: it ADDS a migration the
# base branch never merged. The gate must never see this — it resolves from base.
_HEAD_MIGRATIONS_SRC = _BASE_MIGRATIONS_SRC.replace(
    "return (Migration(id=\"M-20260604-0001-baseline\", apply=_noop),)",
    "return (\n"
    "        Migration(id=\"M-20260604-0001-baseline\", apply=_noop),\n"
    "        Migration(id=\"" + MIG_ID + "\", apply=_noop),\n"
    "    )",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


class BaseRegistryResolutionTests(unittest.TestCase):
    """The security property, exercised end-to-end against a REAL git repository:
    the migration function is resolved from the BASE branch, never the PR head, so a
    PR that carries its own new migration definition blocks because the id is absent
    from base — the case that matters most."""

    def _repo(self, tmp: str) -> Path:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        return repo

    def test_registry_is_read_from_base_not_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            scripts = repo / "scripts"
            scripts.mkdir()
            # Base branch: the registry WITHOUT the attacker migration.
            (scripts / "shiki_migrations.py").write_text(_BASE_MIGRATIONS_SRC, encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "base migrations")
            # Head branch: adds the attacker migration to its own registry.
            _git(repo, "checkout", "-q", "-b", "head")
            (scripts / "shiki_migrations.py").write_text(_HEAD_MIGRATIONS_SRC, encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "head adds a self-defining migration")

            registry = resolve_base_migration_registry(repo, "main")
        ids = {str(migration.id) for migration in registry}
        self.assertIn(BASELINE, ids)
        self.assertNotIn(MIG_ID, ids, "the head-defined migration must be absent from the base registry")

    def test_self_defining_migration_pr_blocks_because_id_absent_from_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            scripts = repo / "scripts"
            scripts.mkdir()
            (scripts / "shiki_migrations.py").write_text(_BASE_MIGRATIONS_SRC, encoding="utf-8")
            # A base .shiki snapshot for the gate to apply against.
            _seed_base(repo)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "base")
            _git(repo, "checkout", "-q", "-b", "head")
            # The PR head both DEFINES the migration and delivers a .shiki change,
            # declaring the id it just invented.
            (scripts / "shiki_migrations.py").write_text(_HEAD_MIGRATIONS_SRC, encoding="utf-8")
            _backfill_apply(repo, False)

            blocking: list[str] = []
            # base_registry=None => the gate resolves it from base via git show.
            enforce_migration(
                target=repo,
                pr={"body": f"<!-- shiki:migration -->\nMigration: {MIG_ID}", "baseRefName": "main"},
                base_shiki=repo / ".shiki",
                changed_files_status=_conforming_changes(),
                blocking=blocking,
                warnings=[],
            )
        self.assertTrue(
            any("is not registered in the base branch" in b and MIG_ID in b for b in blocking),
            blocking,
        )


if __name__ == "__main__":
    unittest.main()
