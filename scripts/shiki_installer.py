#!/usr/bin/env python3
"""Template copy, manifest staging, and local/global install helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
from typing import Any

from shiki_config import load_shiki_config
from shiki_manifest import load_manifest, manifest_create_directories, manifest_directories, manifest_exclude_from_commit, manifest_install_include
from shiki_migrations import migration_status
from shiki_process import ROOT, ShikiError, info, warn, validate_target_shiki, load_default_config

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
    "scripts/build_cca_evidence_manifest.py",
    "scripts/guardian_approval_signal.py",
    "scripts/mergegate_check.py",
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
    ".shiki/config.yaml": ("mergegate.required_checks",),
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


def install_template(target: Path, *, force: bool, validate: bool) -> list[NewFileNote]:
    # Preconditions first: never rewrite the tree of a target that cannot be
    # upgraded cleanly (see refuse_pending_migrations).
    refuse_pending_migrations(target)

    notes: list[NewFileNote] = []
    for relative in TEMPLATE_PATHS:
        source = ROOT / relative
        if not source.exists():
            warn(f"template path missing, skipped: {relative}")
            continue
        copy_path(source, target / relative, force=force, target_install=True, notes=notes)

    manifest = load_manifest(ROOT)
    directories = manifest_directories(manifest)
    for relative in manifest_create_directories(manifest):
        state_dir = target / relative
        state_dir.mkdir(parents=True, exist_ok=True)
        metadata = directories.get(relative, {})
        if metadata.get("tracked") is True and metadata.get("required") is True and not any(state_dir.iterdir()):
            (state_dir / ".gitkeep").write_text("", encoding="utf-8")
        info(f"ensured empty state directory: {state_dir}")

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
) -> None:
    if should_skip(source, target_install=target_install):
        return
    if source.is_dir():
        for child in source.iterdir():
            copy_path(child, target / child.name, force=force, target_install=target_install, notes=notes)
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
