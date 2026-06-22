#!/usr/bin/env python3
"""Template copy, manifest staging, and local/global install helpers."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from shiki_manifest import load_manifest, manifest_create_directories, manifest_directories, manifest_exclude_from_commit, manifest_install_include
from shiki_process import ROOT, ShikiError, info, warn, validate_target_shiki, load_default_config

TEMPLATE_PATHS = [
    "bin/shiki",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTEXT.md",
    "SYSTEM_PROMPT.md",
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
    "scripts/shiki_doctor.py",
    "scripts/shiki_git.py",
    "scripts/shiki_github.py",
    "scripts/shiki_guardian.py",
    "scripts/shiki_guardian_review.py",
    "scripts/shiki_installer.py",
    "scripts/shiki_process.py",
    "scripts/shiki_runtime.py",
    "scripts/shiki_runtime_adapters.py",
    "scripts/shiki_runtime_registry.py",
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


def install_template(target: Path, *, force: bool, validate: bool) -> None:
    for relative in TEMPLATE_PATHS:
        source = ROOT / relative
        if not source.exists():
            warn(f"template path missing, skipped: {relative}")
            continue
        copy_path(source, target / relative, force=force, target_install=True)

    manifest = load_manifest(ROOT)
    directories = manifest_directories(manifest)
    for relative in manifest_create_directories(manifest):
        state_dir = target / relative
        state_dir.mkdir(parents=True, exist_ok=True)
        metadata = directories.get(relative, {})
        if metadata.get("tracked") is True and metadata.get("required") is True and not any(state_dir.iterdir()):
            (state_dir / ".gitkeep").write_text("", encoding="utf-8")
        info(f"ensured empty state directory: {state_dir}")

    if validate:
        validate_target_shiki(target)


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
        # Provider metadata is created per-target by shiki init/start; copying it
        # into a new target would point that target at this repository's origin.
        if relative_text == ".shiki/repo.json":
            return True
        state_prefixes = tuple(f"{directory}/" for directory in manifest_create_directories(manifest))
        if relative_text.startswith(state_prefixes):
            return True
        return excluded_from_commit(relative_text, manifest_exclude_from_commit(manifest))
    return False


def copy_path(source: Path, target: Path, *, force: bool, target_install: bool = False) -> None:
    if should_skip(source, target_install=target_install):
        return
    if source.is_dir():
        for child in source.iterdir():
            copy_path(child, target / child.name, force=force, target_install=target_install)
        return

    if target.exists() and not force:
        warn(f"kept existing file: {target}")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    info(f"installed {target}")


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
