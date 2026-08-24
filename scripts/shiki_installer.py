#!/usr/bin/env python3
"""Template copy, manifest staging, and local/global install helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from shiki_config import load_shiki_config
from shiki_contracts import CODEOWNERS_PATH, CODEOWNERS_REQUIRED_OWNER, codeowners_required_owner
from shiki_manifest import load_manifest, manifest_create_directories, manifest_directories, manifest_exclude_from_commit, manifest_install_include
from shiki_migrations import migration_status
from shiki_process import ROOT, ShikiError, info, warn, validate_target_shiki, load_default_config, utc_now

TEMPLATE_PATHS = [
    "bin/shiki",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTEXT.md",
    "SYSTEM_PROMPT.md",
    ".gitignore",
    ".claude/commands/shiki.md",
    ".codex/skills/shiki/SKILL.md",
    ".shiki",
    ".github/ISSUE_TEMPLATE",
    ".github/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE",
    ".github/prompts",
    ".github/workflows/shiki-validate.yml",
    ".github/workflows/shiki-claude-review.yml",
    ".github/workflows/shiki-cca-completion.yml",
    ".github/workflows/shiki-mergegate.yml",
    ".github/workflows/shiki-orchestrator.yml",
    "docs/agents",
    "docs/adr",
    "skills/engineering",
    "tests",
    "scripts/shiki_schema.py",
    "scripts/validate_shiki.py",
    "scripts/shiki_contracts.py",
    "scripts/shiki_jsonschema.py",
    "scripts/shiki_evidence.py",
    "scripts/shiki_locks.py",
    "scripts/shiki_loop.py",
    "scripts/shiki_memory.py",
    "scripts/shiki_manifest.py",
    "scripts/shiki_migrations.py",
    "scripts/shiki_provider.py",
    "scripts/shiki_workflows.py",
    "scripts/enforce_cca_verdict.py",
    "scripts/cca_verdict_usable.py",
    "scripts/build_cca_evidence_manifest.py",
    "scripts/guardian_approval_signal.py",
    "scripts/mergegate_check.py",
    "scripts/shiki_sync_proof.py",
    "scripts/shiki.py",
    "scripts/shiki_bootstrap.py",
    "scripts/shiki_cli.py",
    "scripts/shiki_config.py",
    "scripts/shiki_contract_approval.py",
    "scripts/shiki_doctor.py",
    "scripts/shiki_git.py",
    "scripts/shiki_github.py",
    "scripts/shiki_guardian.py",
    "scripts/shiki_guardian_review.py",
    "scripts/shiki_guardian_status.py",
    "scripts/shiki_installer.py",
    "scripts/shiki_process.py",
    "scripts/shiki_runtime.py",
    "scripts/shiki_runtime_adapters.py",
    "scripts/shiki_runtime_registry.py",
    "scripts/shiki_session_lease.py",
    "scripts/shiki_state_classes.py",
    "scripts/shiki_tasks.py",
    "scripts/shiki_state.py",
    "scripts/test_shiki_init.sh",
    "scripts/test_shiki_control_plane.sh",
    "scripts/test_shiki_run_orchestrator.sh",
    "scripts/test_shiki_daemon_runner.sh",
    "scripts/test_shiki_runner_codex.sh",
    "scripts/test_shiki_runner_claude.sh",
    "scripts/test_shiki_goal_loop.sh",
    "scripts/test_shiki_memory_loop.sh",
    "scripts/test_shiki_code_review_gate.sh",
    "scripts/test_shiki_start.sh",
    "scripts/test_shiki_runtime_auth.sh",
    "scripts/test_shiki_runtime_registry.sh",
    "scripts/test_shiki_state_classes.sh",
    "scripts/test_shiki_provider_config.sh",
    "scripts/test_shiki_guardian_policy.sh",
    "scripts/test_shiki_evidence_integrity.sh",
    "scripts/test_shiki_governance_evidence.sh",
    "scripts/test_shiki_doctor.sh",
    "scripts/test_shiki_migrations.sh",
    "scripts/test_shiki_module_boundaries.sh",
    "scripts/test_shiki_shellcheck.sh",
    "scripts/test_shiki_validator_hardening.sh",
    "scripts/test_shiki_workflow_lint.sh",
]

DEFAULT_GLOBAL_COMMAND_PATH = "~/.local/bin/shiki"
DEFAULT_CLAUDE_COMMAND_PATH = "~/.claude/commands/shiki.md"
DEFAULT_CODEX_SKILL_PATH = "~/.codex/skills/shiki/SKILL.md"

# The install stamp records, per target, which platform commit an install came
# from, when it was installed, and a content digest for every platform-owned
# shipped path. It is target state (like migration state and repo.json), not a
# contract, so it lives beside the other target-owned records under `.shiki/`.
#
# It is deliberately NOT tracked: it is never added to `manifest_stage_paths`,
# and validate_shiki only classifies *tracked* `.shiki/` paths, so an untracked
# stamp is invisible to the mirror validator. Keeping it out of the manifest is
# what lets a target carry it without a governance-schema change.
INSTALL_STAMP_PATH = ".shiki/install-stamp.json"
INSTALL_STAMP_VERSION = 1
LEGACY_SHIKI_SADR_MAX = 18

# Under --force the shipped surface splits three ways, so nothing is lost and
# nothing is silently stale.
#
# PROJECT CONTENT is authored per target and must never be overwritten. The
# incoming template is written alongside as ``<file>.new`` and reported, so the
# operator can merge deliberately (CONTEXT.md is the glossary AGENTS.md names as
# the source of truth for domain language).
PROJECT_CONTENT_FILES = (
    "CONTEXT.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".github/CODEOWNERS",
)

# GOVERNANCE CONTRACT is target-specific but decides what branch protection
# requires (config.yaml -> mergegate.required_checks) and what may approve
# (guardian-policy.json -> approval_sources). It must never be silently kept
# either: write ``<file>.new`` alongside and report which keys differ, so a stale
# contract is visible rather than inferred.
GOVERNANCE_CONTRACT_FILES = (
    ".shiki/config.yaml",
    ".shiki/guardian-policy.json",
)

# Governance keys named explicitly in the end-of-run summary when they differ,
# because they are the ones that change what protection requires / what approves.
GOVERNANCE_CRITICAL_KEYS: dict[str, tuple[str, ...]] = {
    ".shiki/config.yaml": (
        "mergegate.required_checks",
        # An existing target predating SADR-0021 has no code-owner key at all.
        # _governance_diff compares template leaves against target leaves, so the
        # key must ship in the template or its ABSENCE in a target produces no
        # diff and the summary never names it — a silent governance downgrade at
        # the next `--protect`, which is exactly what this entry prevents.
        "defaults.required_code_owner_review",
    ),
    ".shiki/guardian-policy.json": ("approval_sources",),
}

# Applied-migration state is target history, not a contract: it is preserved
# outright under --force and never written as a ``.new``.
PRESERVE_OUTRIGHT_FILES = (".shiki/migrations/state.json",)

# Shipped `tests/` files that must NOT be copied into a target install. Each is a
# platform-only test: it asserts something that exists only in the Shiki platform
# repo itself, so it can never pass from a freshly installed target and would make
# the target's own `unittest discover -s tests` (a required check) fail forever.
# These files are still committed and run in the platform (they are absent only
# from the target's shipped surface, not from `exclude_from_commit`). Reason per
# entry: an omission here is a deliberate, recorded decision, not an oversight.
TARGET_INSTALL_EXCLUDES = {
    "tests/test_loop_e2e_contract.py": (
        "Contract meta-test over scripts/test_shiki_loop_e2e.sh and "
        "docs/agents/autonomous-loop-e2e-acceptance.md — platform-only e2e "
        "artifacts that are not part of the shipped target surface."
    ),
    "tests/test_mirror_lock_injection.py": (
        "FrozenPlanCorpusTests asserts the platform's own frozen-plan corpus "
        "(>=1 frozen plan, >=19 tasks); a freshly installed target starts with "
        "an empty .shiki mirror, so the assertion cannot hold there."
    ),
    "tests/test_install_version_drift.py": (
        "Install-stamp drift and platform-commit lineage are judged against the "
        "running platform's git HEAD; a freshly installed target has different "
        "or absent git lineage, so these assertions are platform-context only."
    ),
}

def manifest_stage_paths(path: Path) -> list[str]:
    candidates = list(TEMPLATE_PATHS)
    candidates.append(".shiki/manifest.json")
    candidates.append(".shiki/guardian-policy.json")
    candidates.append(".shiki/migrations/state.json")
    candidates.append(".shiki/repo.json")
    manifest = load_manifest(path) if (path / ".shiki" / "manifest.json").exists() else load_manifest(ROOT)
    excluded = manifest_exclude_from_commit(manifest)
    return [
        relative
        for relative in candidates
        if (path / relative).exists() and not excluded_from_commit(relative, excluded)
    ]


def excluded_from_commit(relative: str, patterns: list[str]) -> bool:
    normalized = relative.strip().replace("\\", "/")
    for pattern in patterns:
        clean = pattern.strip().replace("\\", "/")
        if clean.endswith("/**"):
            prefix = clean[:-3]
            if normalized == prefix or normalized.startswith(f"{prefix}/"):
                return True
            continue
        if normalized == clean:
            return True
    return False


@dataclass
class NewFileNote:
    """A ``<file>.new`` written alongside a preserved original under --force."""

    relative: str
    new_relative: str
    category: str  # "project-content" | "governance-contract"
    differing_keys: tuple[str, ...] = field(default_factory=tuple)


def refuse_pending_migrations(target: Path) -> None:
    """Precondition run BEFORE any file write.

    Refuse to upgrade a target whose valid ``.shiki`` migration state is behind
    the current registry, so a target with pending migrations keeps an untouched
    tree instead of being half-rewritten and then aborting. Only the "valid but
    behind" upgrade case is gated: a fresh target has no state file yet (it is
    seeded by this install), and a target whose state file is absent or malformed
    is left to post-write validation, which surfaces malformed state.
    """
    status = migration_status(target)
    if status["state_exists"] and not status["errors"] and status["pending"]:
        pending = ", ".join(status["pending"])
        raise ShikiError(
            f"target has pending .shiki migrations ({pending}); run "
            "`shiki migrate apply --execute` before upgrading. No files were written."
        )


def legacy_shiki_adr_paths() -> tuple[str, ...]:
    """Former numeric paths for Shiki SADR-0001 through SADR-0018.

    The authoritative slugs come from the current SADR filenames. SADR-0019 is
    the namespace decision itself and never shipped under a numeric legacy path.
    """
    paths: list[str] = []
    for sadr in sorted((ROOT / "docs" / "adr").glob("SADR-[0-9][0-9][0-9][0-9]-*.md")):
        number = sadr.name.split("-", 2)[1]
        if int(number) <= LEGACY_SHIKI_SADR_MAX:
            paths.append(f"docs/adr/{sadr.name.removeprefix('SADR-')}")
    return tuple(paths)


def preflight_legacy_sadr_cleanup(target: Path, *, force: bool) -> tuple[str, ...]:
    """Authorize exact legacy Shiki ADR removals before the first write.

    A non-force install never removes existing files. A forced install needs no
    stamp when no legacy Shiki path is present. When legacy paths do exist, each
    must be named in a readable install stamp and its current bytes must match
    the recorded digest.
    """
    if not force:
        return ()
    present = tuple(
        relative
        for relative in legacy_shiki_adr_paths()
        if (target / relative).exists() or (target / relative).is_symlink()
    )
    if not present:
        return ()

    stamp_path = target / INSTALL_STAMP_PATH
    try:
        raw = stamp_path.read_text(encoding="utf-8")
        stamp = json.loads(raw)
    except FileNotFoundError:
        detail = "install stamp is absent"
        blockers = [f"{relative}: {detail}" for relative in present]
    except (OSError, UnicodeDecodeError) as error:
        detail = f"install stamp is unreadable ({error})"
        blockers = [f"{relative}: {detail}" for relative in present]
    except json.JSONDecodeError as error:
        detail = f"install stamp is malformed ({error})"
        blockers = [f"{relative}: {detail}" for relative in present]
    else:
        if not isinstance(stamp, dict):
            blockers = [
                f"{relative}: install stamp is malformed (expected an object)"
                for relative in present
            ]
        else:
            digests = stamp.get("digests")
            blockers = []
            if not isinstance(digests, dict):
                blockers.extend(
                    f"{relative}: install stamp has no digest for this path"
                    for relative in present
                )
            else:
                for relative in present:
                    expected = digests.get(relative)
                    if not isinstance(expected, str) or not expected:
                        blockers.append(
                            f"{relative}: install stamp has no digest for this path"
                        )
                        continue
                    try:
                        actual = _sha256_file(target / relative)
                    except OSError as error:
                        blockers.append(
                            f"{relative}: legacy path is unreadable ({error})"
                        )
                        continue
                    if actual != expected:
                        blockers.append(
                            f"{relative}: digest mismatch (stamp {expected}, current {actual})"
                        )

    if blockers:
        joined = "\n".join(f"- {blocker}" for blocker in blockers)
        raise ShikiError(
            "legacy Shiki ADR cleanup is not authorized:\n"
            f"{joined}\nNo files were written."
        )
    return present


def install_template(target: Path, *, force: bool, validate: bool) -> list[NewFileNote]:
    # Preconditions first: never rewrite the tree of a target that cannot be
    # upgraded cleanly (see refuse_pending_migrations).
    legacy_cleanup = preflight_legacy_sadr_cleanup(target, force=force)
    refuse_pending_migrations(target)

    # Resolve the target's required CODEOWNERS owner once from its
    # ``.shiki/repo.json`` (present when a target already knows its GitHub
    # identity). The shipped ``.github/CODEOWNERS`` names this maintainer on every
    # line, so a foreign target's copy is rewritten to name its own owner; when no
    # owner can be resolved this is the documented fallback and the file is copied
    # verbatim. See ``codeowners_required_owner``.
    codeowners_owner = codeowners_required_owner(target)

    for relative in legacy_cleanup:
        (target / relative).unlink()
        info(f"removed stamped legacy Shiki ADR: {target / relative}")

    notes: list[NewFileNote] = []
    for relative in TEMPLATE_PATHS:
        source = ROOT / relative
        if not source.exists():
            warn(f"template path missing, skipped: {relative}")
            continue
        copy_path(
            source,
            target / relative,
            force=force,
            target_install=True,
            notes=notes,
            codeowners_owner=codeowners_owner,
        )

    manifest = load_manifest(ROOT)
    directories = manifest_directories(manifest)
    for relative in manifest_create_directories(manifest):
        state_dir = target / relative
        state_dir.mkdir(parents=True, exist_ok=True)
        metadata = directories.get(relative, {})
        if metadata.get("tracked") is True and metadata.get("required") is True and not any(state_dir.iterdir()):
            (state_dir / ".gitkeep").write_text("", encoding="utf-8")
        info(f"ensured empty state directory: {state_dir}")

    # Stamp the install after the shipped surface is on disk, so the digests
    # describe exactly what was written. Refreshed on every install and upgrade.
    # The stamp stays untracked local state, so keep it out of git first.
    _ensure_stamp_gitignored(target)
    write_install_stamp(target)

    # A single explicit summary at the end, so a half-upgrade is visible rather
    # than inferred. Printed before validation so the .new files are always
    # reported even if a later gate fails.
    print_new_file_summary(notes)

    if validate:
        validate_target_shiki(target)

    return notes


def print_new_file_summary(notes: list[NewFileNote]) -> None:
    if not notes:
        info(
            "template summary: no .new files written (no existing project-content "
            "or governance file was kept alongside a refreshed copy)"
        )
        return
    info(f"template summary: wrote {len(notes)} .new file(s) alongside preserved originals:")
    for note in sorted(notes, key=lambda item: item.relative):
        if note.category == "governance-contract":
            keys = ", ".join(note.differing_keys) if note.differing_keys else "no parseable key differences"
            info(f"  governance-contract: kept {note.relative}, wrote {note.new_relative} (differing keys: {keys})")
        else:
            info(f"  project-content: kept {note.relative}, wrote {note.new_relative}")


def should_skip(path: Path, *, target_install: bool = False) -> bool:
    parts = set(path.parts)
    if "__pycache__" in parts or path.name == ".DS_Store" or path.suffix == ".pyc":
        return True
    if target_install:
        relative = path.relative_to(ROOT)
        relative_text = relative.as_posix()
        manifest = load_manifest(ROOT)
        if relative_text in manifest_install_include(manifest):
            return False
        # An installed target also contains its own product ADRs under
        # ``docs/adr/NNNN-*.md``. They are target content, never Shiki template
        # files, so a target-local CLI must not re-export them when installing
        # another fixture or repository. Platform decisions use the explicit
        # ``SADR-NNNN-*.md`` namespace and continue through the normal copy path.
        if relative_text.startswith("docs/adr/"):
            name = path.name
            if (
                len(name) > 5
                and name[:4].isdigit()
                and name[4] == "-"
                and name.endswith(".md")
            ):
                return True
        # Platform-only tests never ship into a target (see TARGET_INSTALL_EXCLUDES).
        if relative_text in TARGET_INSTALL_EXCLUDES:
            return True
        # Provider metadata is created per-target by shiki init/start; copying it
        # into a new target would point that target at this repository's origin.
        if relative_text == ".shiki/repo.json":
            return True
        state_prefixes = tuple(f"{directory}/" for directory in manifest_create_directories(manifest))
        if relative_text.startswith(state_prefixes):
            return True
        return excluded_from_commit(relative_text, manifest_exclude_from_commit(manifest))
    return False


def template_relative(source: Path) -> str | None:
    """The shipped-surface relative path for ``source`` (POSIX), or None."""
    try:
        return source.relative_to(ROOT).as_posix()
    except ValueError:
        return None


def force_category(source: Path) -> str:
    """Classify a shipped file for --force handling.

    Returns one of ``project-content``, ``governance-contract``,
    ``preserve-outright``, or ``overwrite``.
    """
    relative = template_relative(source)
    if relative in PROJECT_CONTENT_FILES:
        return "project-content"
    if relative in GOVERNANCE_CONTRACT_FILES:
        return "governance-contract"
    if relative in PRESERVE_OUTRIGHT_FILES:
        return "preserve-outright"
    return "overwrite"


def copy_path(
    source: Path,
    target: Path,
    *,
    force: bool,
    target_install: bool = False,
    notes: list[NewFileNote] | None = None,
    codeowners_owner: str | None = None,
) -> None:
    if should_skip(source, target_install=target_install):
        return
    if source.is_dir():
        for child in source.iterdir():
            copy_path(
                child,
                target / child.name,
                force=force,
                target_install=target_install,
                notes=notes,
                codeowners_owner=codeowners_owner,
            )
        return

    if target.exists():
        if not force:
            warn(f"kept existing file: {target}")
            return
        category = force_category(source)
        if category == "preserve-outright":
            warn(f"preserved existing stateful file (not overwritten by --force): {target}")
            return
        if category in ("project-content", "governance-contract"):
            differing = (
                governance_differing_keys(source, target)
                if category == "governance-contract"
                else ()
            )
            note = write_new_alongside(source, target, category=category, differing_keys=differing)
            if notes is not None:
                notes.append(note)
            return

    target.parent.mkdir(parents=True, exist_ok=True)
    if (
        target_install
        and codeowners_owner
        and codeowners_owner != CODEOWNERS_REQUIRED_OWNER
        and template_relative(source) == CODEOWNERS_PATH
    ):
        # Substitute the target's own owner for the shipped maintainer so the
        # target's CODEOWNERS check resolves against its own owner. Only the
        # owner token changes; when it equals the fallback the file is copied
        # verbatim above, preserving byte-for-byte behaviour for this repo.
        text = source.read_text(encoding="utf-8").replace(CODEOWNERS_REQUIRED_OWNER, codeowners_owner)
        target.write_text(text, encoding="utf-8")
    else:
        shutil.copy2(source, target)
    info(f"installed {target}")


def write_new_alongside(
    source: Path,
    target: Path,
    *,
    category: str,
    differing_keys: tuple[str, ...] = (),
) -> NewFileNote:
    """Keep ``target`` untouched and write the incoming template as ``<target>.new``."""
    new_path = target.parent / (target.name + ".new")
    new_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, new_path)
    warn(
        f"preserved existing {category} file (not overwritten by --force); "
        f"wrote {new_path.name} for review: {target}"
    )
    relative = template_relative(source) or target.name
    return NewFileNote(
        relative=relative,
        new_relative=f"{relative}.new",
        category=category,
        differing_keys=tuple(differing_keys),
    )


_MISSING = object()


def _flatten_leaf_keys(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested mapping to dotted leaf keys; lists are leaves."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key in value:
            child = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten_leaf_keys(value[key], child))
        return out
    out[prefix] = value
    return out


def _load_governance_data(relative: str | None, path: Path) -> dict[str, Any]:
    if relative == ".shiki/config.yaml":
        # Reuse the subset YAML parser bootstrap already owns; it reads
        # ``<dir>/.shiki/config.yaml`` and captures mergegate.required_checks.
        return load_shiki_config(path.parent.parent)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def governance_differing_keys(source: Path, target: Path) -> tuple[str, ...]:
    """Dotted keys that differ between the incoming template and the kept target.

    Differences under a critical key (``mergegate.required_checks`` for config,
    ``approval_sources`` for guardian policy) collapse to that key so it is named
    explicitly, while other differences keep their leaf path.
    """
    relative = template_relative(source)
    template_data = _load_governance_data(relative, source)
    target_data = _load_governance_data(relative, target)
    template_leaves = _flatten_leaf_keys(template_data)
    target_leaves = _flatten_leaf_keys(target_data)
    keys = set(template_leaves) | set(target_leaves)
    differing = [
        key
        for key in keys
        if template_leaves.get(key, _MISSING) != target_leaves.get(key, _MISSING)
    ]
    critical = GOVERNANCE_CRITICAL_KEYS.get(relative or "", ())
    collapsed: set[str] = set()
    for leaf in differing:
        mapped = leaf
        for crit in critical:
            if leaf == crit or leaf.startswith(f"{crit}."):
                mapped = crit
                break
        collapsed.add(mapped)
    return tuple(sorted(collapsed))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _is_stamped_shipped(relative: str) -> bool:
    """Whether a shipped path belongs in the install stamp's digest set.

    Only *platform-owned* content is stamped: files that an upgrade overwrites
    verbatim, so a difference proves a hand-edit or a half-upgrade. Project
    content, governance contracts, and preserve-outright state are customized
    per target, so a difference there is expected and is never drift.
    """
    if relative == INSTALL_STAMP_PATH:
        return False
    return (
        relative not in PROJECT_CONTENT_FILES
        and relative not in GOVERNANCE_CONTRACT_FILES
        and relative not in PRESERVE_OUTRIGHT_FILES
    )


def shipped_stamp_paths() -> list[str]:
    """Relative POSIX paths of every platform-owned file an install writes.

    Enumerated from the platform (``ROOT``) with the same skip rules install
    uses, so the set names exactly the shipped surface that a target must match.
    """
    shipped: list[str] = []

    def collect(source: Path) -> None:
        if should_skip(source, target_install=True):
            return
        if source.is_dir():
            for child in sorted(source.iterdir()):
                collect(child)
            return
        relative = template_relative(source)
        if relative is None or not _is_stamped_shipped(relative):
            return
        shipped.append(relative)

    for relative in TEMPLATE_PATHS:
        source = ROOT / relative
        if source.exists():
            collect(source)
    return sorted(set(shipped))


def target_stamp_digests(target: Path) -> dict[str, str]:
    """Content digest for every stamped shipped path present in ``target``."""
    return {
        relative: _sha256_file(target / relative)
        for relative in shipped_stamp_paths()
        if (target / relative).is_file()
    }


def platform_commit(root: Path = ROOT) -> str | None:
    """The git HEAD of the running platform checkout, or None when unavailable."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def commit_is_ancestor(root: Path, ancestor: str, descendant: str) -> bool | None:
    """True if ``ancestor`` is an ancestor of ``descendant`` in ``root``'s git.

    None when the answer cannot be determined (git missing, or a commit unknown
    to this checkout — which is itself the "different lineage" signal).
    """
    if not ancestor or not descendant:
        return None
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _incoming_is_older(incoming_commit: str | None, existing_commit: str | None) -> bool:
    """Whether a fresh install is provably older than the recorded stamp.

    Only a *proven* older lineage — the incoming platform commit is a strict
    ancestor of the recorded one — blocks the overwrite. Same commit, unrelated
    lineages, or an undeterminable relationship all refresh, so the guard never
    silently keeps stale content it cannot prove is newer.
    """
    if not incoming_commit or not existing_commit or incoming_commit == existing_commit:
        return False
    return commit_is_ancestor(ROOT, incoming_commit, existing_commit) is True


def _ensure_stamp_gitignored(target: Path) -> None:
    """Keep the untracked install stamp out of ``git add`` / ``git add -A``.

    The stamp is target-local state that is never committed: validate_shiki
    rejects an unknown *tracked* ``.shiki/`` path, and the goal loop stages
    worktrees with ``git add -A``, so an un-ignored stamp would be committed and
    make the target's own Validate check permanently red. The shipped
    ``.gitignore`` is platform-owned and cannot name it, so the installer
    ensures the target's own ``.gitignore`` ignores it. Idempotent: a re-install
    never appends the entry twice.
    """
    gitignore = target / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if INSTALL_STAMP_PATH in {line.strip() for line in existing.splitlines()}:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    addition = (
        f"{prefix}\n# Local-only install version stamp (see `shiki doctor` drift checks)\n"
        f"{INSTALL_STAMP_PATH}\n"
    )
    with gitignore.open("a", encoding="utf-8") as handle:
        handle.write(addition)


def read_install_stamp(target: Path) -> dict[str, Any] | None:
    """Parse a target's install stamp, or None when absent/unreadable."""
    path = target / INSTALL_STAMP_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_install_stamp(target: Path) -> None:
    """Write (or refresh) the target's install stamp.

    Never overwrites a stamp from a newer platform commit with older content,
    and leaves an already-current stamp byte-for-byte untouched so a no-op
    re-install churns nothing.
    """
    commit = platform_commit(ROOT)
    digests = target_stamp_digests(target)
    existing = read_install_stamp(target)
    if existing is not None:
        # A non-force install deliberately leaves legacy Shiki ADRs in place.
        # Preserve only still-matching ownership proof so a later forced
        # upgrade can authorize cleanup; never carry a missing or stale digest.
        existing_digests = existing.get("digests")
        if isinstance(existing_digests, dict):
            for relative in legacy_shiki_adr_paths():
                expected = existing_digests.get(relative)
                path = target / relative
                if not isinstance(expected, str) or not path.is_file():
                    continue
                try:
                    actual = _sha256_file(path)
                except OSError:
                    continue
                if actual == expected:
                    digests[relative] = actual
        if _incoming_is_older(commit, existing.get("platform_commit")):
            warn(
                "kept existing install stamp: incoming install "
                f"({commit}) is older than the recorded platform commit "
                f"({existing.get('platform_commit')}); not overwriting {INSTALL_STAMP_PATH}"
            )
            return
        if existing.get("platform_commit") == commit and existing.get("digests") == digests:
            return
    stamp = {
        "version": INSTALL_STAMP_VERSION,
        "platform_commit": commit,
        "installed_at": utc_now(),
        "digests": digests,
    }
    path = target / INSTALL_STAMP_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    info(f"wrote install stamp: {path}")


def cmd_install_target(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser().resolve()
    if not target.exists():
        raise ShikiError(f"target does not exist: {target}")
    if not target.is_dir():
        raise ShikiError(f"target is not a directory: {target}")

    if not args.local_only:
        raise ShikiError("install-target is template-only; use shiki init TARGET --repo OWNER/NAME for GitHub-first setup, or pass --local-only explicitly")

    install_template(target, force=args.force, validate=args.validate)

    return 0


def cmd_install_command(args: argparse.Namespace) -> int:
    destination = Path(args.path).expanduser()
    install_cli_command(destination)
    info("ensure the parent directory is on PATH")
    return 0


def install_cli_command(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    destination.symlink_to(ROOT / "bin" / "shiki")
    info(f"installed command: {destination}")


def install_file(source: Path, destination: Path) -> None:
    if not source.exists():
        raise ShikiError(f"source file not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    info(f"installed {destination}")


def cmd_install_global(args: argparse.Namespace) -> int:
    install_cli_command(Path(args.path).expanduser())

    if args.claude_command:
        install_file(
            ROOT / ".claude" / "commands" / "shiki.md",
            Path(args.claude_command_path).expanduser(),
        )

    if args.codex_skill:
        install_file(
            ROOT / ".codex" / "skills" / "shiki" / "SKILL.md",
            Path(args.codex_skill_path).expanduser(),
        )

    info("global install complete")
    info("restart Codex or Claude Code if the running client does not reload commands dynamically")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    config = load_default_config()
    status = {
        "root": str(ROOT),
        "config": config,
        "command": shutil.which("shiki"),
        "claude_command": str(Path(DEFAULT_CLAUDE_COMMAND_PATH).expanduser()),
        "claude_command_installed": Path(DEFAULT_CLAUDE_COMMAND_PATH).expanduser().exists(),
        "codex_skill": str(Path(DEFAULT_CODEX_SKILL_PATH).expanduser()),
        "codex_skill_installed": Path(DEFAULT_CODEX_SKILL_PATH).expanduser().exists(),
    }
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0
