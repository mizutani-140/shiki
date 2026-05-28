#!/usr/bin/env python3
"""Shiki control CLI.

This file intentionally uses only the Python standard library so the bootstrap
command can run before a target repository has installed dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG = Path.home() / ".shiki" / "config.json"

TEMPLATE_PATHS = [
    "bin/shiki",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTEXT.md",
    "SYSTEM_PROMPT.md",
    ".shiki",
    ".github/ISSUE_TEMPLATE",
    ".github/PULL_REQUEST_TEMPLATE",
    ".github/prompts",
    ".github/workflows/shiki-validate.yml",
    ".github/workflows/shiki-claude-review.yml",
    ".github/workflows/shiki-cca-completion.yml",
    ".github/workflows/shiki-mergegate.yml",
    "docs/agents",
    "docs/adr",
    "scripts/validate_shiki.py",
    "scripts/enforce_cca_verdict.py",
    "scripts/mergegate_check.py",
    "scripts/shiki.py",
]

DEFAULT_REQUIRED_CHECKS = [
    "Validate Shiki mirror",
    "CCA verdict",
    "MergeGate policy check",
]


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class ShikiError(Exception):
    pass


def run(
    args: list[str],
    *,
    cwd: Path = ROOT,
    input_text: str | None = None,
    check: bool = True,
) -> CommandResult:
    process = subprocess.run(
        args,
        cwd=str(cwd),
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    result = CommandResult(args, process.returncode, process.stdout, process.stderr)
    if check and process.returncode != 0:
        command = " ".join(args)
        raise ShikiError(f"{command} failed\n{process.stderr.strip()}")
    return result


def info(message: str) -> None:
    print(f"[shiki] {message}")


def warn(message: str) -> None:
    print(f"[shiki] warning: {message}", file=sys.stderr)


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise ShikiError(f"required tool not found: {name}")


def is_git_repo(path: Path) -> bool:
    return run(["git", "rev-parse", "--show-toplevel"], cwd=path, check=False).returncode == 0


def current_branch(path: Path) -> str | None:
    result = run(["git", "branch", "--show-current"], cwd=path, check=False)
    branch = result.stdout.strip()
    return branch or None


def ensure_git_repo(path: Path, branch: str) -> None:
    if is_git_repo(path):
        return
    info(f"initializing git repository on {branch}")
    result = run(["git", "init", "-b", branch], cwd=path, check=False)
    if result.returncode != 0:
        run(["git", "init"], cwd=path)
        run(["git", "checkout", "-B", branch], cwd=path)


def ensure_remote(repo: str, path: Path) -> None:
    remote_url = f"https://github.com/{repo}.git"
    existing = run(["git", "remote", "get-url", "origin"], cwd=path, check=False)
    if existing.returncode == 0:
        current = existing.stdout.strip()
        if current != remote_url:
            run(["git", "remote", "set-url", "origin", remote_url], cwd=path)
            info(f"updated origin to {remote_url}")
        else:
            info(f"origin already configured: {current}")
        return
    run(["git", "remote", "add", "origin", remote_url], cwd=path)
    info(f"added origin {remote_url}")


def github_repo_exists(repo: str) -> bool:
    return run(["gh", "repo", "view", repo, "--json", "name"], check=False).returncode == 0


def ensure_github_repo(repo: str, visibility: str) -> None:
    if github_repo_exists(repo):
        info(f"GitHub repository already exists: {repo}")
        return
    args = ["gh", "repo", "create", repo]
    args.append(f"--{visibility}")
    args.extend(["--confirm"])
    run(args)
    info(f"created GitHub repository: {repo}")


def has_commits(path: Path) -> bool:
    return run(["git", "rev-parse", "--verify", "HEAD"], cwd=path, check=False).returncode == 0


def has_staged_or_unstaged_changes(path: Path) -> bool:
    result = run(["git", "status", "--porcelain"], cwd=path)
    return bool(result.stdout.strip())


def commit_all(path: Path, message: str) -> None:
    run(["git", "add", "."], cwd=path)
    if not has_staged_or_unstaged_changes(path):
        info("nothing to commit")
        return
    run(["git", "commit", "-m", message], cwd=path)
    info(f"created commit: {message}")


def push_branch(path: Path, branch: str) -> None:
    run(["git", "push", "-u", "origin", branch], cwd=path)
    info(f"pushed {branch} to origin")


def set_default_branch(repo: str, branch: str) -> None:
    result = run(
        ["gh", "api", f"repos/{repo}", "-X", "PATCH", "-f", f"default_branch={branch}"],
        check=False,
    )
    if result.returncode == 0:
        info(f"set default branch to {branch}")
    else:
        warn(f"could not set default branch: {result.stderr.strip()}")


def set_secret(repo: str, secret_name: str, value: str) -> None:
    run(["gh", "secret", "set", secret_name, "--repo", repo], input_text=value)
    info(f"set GitHub secret: {secret_name}")


def protect_branch(repo: str, branch: str, required_checks: list[str]) -> None:
    payload = {
        "required_status_checks": {
            "strict": True,
            "contexts": required_checks,
        },
        "enforce_admins": False,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 1,
        },
        "restrictions": None,
        "required_linear_history": True,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }
    result = run(
        [
            "gh",
            "api",
            f"repos/{repo}/branches/{branch}/protection",
            "-X",
            "PUT",
            "--input",
            "-",
        ],
        input_text=json.dumps(payload),
        check=False,
    )
    if result.returncode == 0:
        info(f"configured branch protection for {branch}")
    else:
        warn(f"could not configure branch protection: {result.stderr.strip()}")
        warn("configure required checks manually if your GitHub plan or permissions block the API")


def validate_local_shiki() -> None:
    run(["python3", "scripts/validate_shiki.py"], cwd=ROOT)
    info("local Shiki validation passed")


def save_default_config(repo: str, branch: str) -> None:
    LOCAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "repo": repo,
        "default_branch": branch,
        "shiki_root": str(ROOT),
    }
    LOCAL_CONFIG.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    info(f"saved defaults to {LOCAL_CONFIG}")


def load_default_config() -> dict[str, str]:
    if not LOCAL_CONFIG.exists():
        return {}
    return json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))


def cmd_bootstrap_github(args: argparse.Namespace) -> int:
    require_tool("git")
    require_tool("gh")

    config = load_default_config()
    repo = args.repo or config.get("repo")
    if not repo:
        raise ShikiError("missing --repo OWNER/NAME and no default repo configured")

    branch = args.branch or config.get("default_branch") or "main"
    visibility = "private" if args.private else "public"

    validate_local_shiki()
    run(["gh", "auth", "status"])
    ensure_git_repo(ROOT, branch)
    ensure_github_repo(repo, visibility)
    ensure_remote(repo, ROOT)

    active_branch = current_branch(ROOT)
    if active_branch != branch:
        run(["git", "checkout", "-B", branch], cwd=ROOT)

    if args.commit:
        commit_all(ROOT, args.commit_message)

    if args.push:
        push_branch(ROOT, branch)
        set_default_branch(repo, branch)

    secret_value = os.environ.get(args.secret_env, "")
    if args.set_secret:
        if not secret_value:
            warn(f"{args.secret_env} is not set; skipping GitHub secret")
        else:
            set_secret(repo, "CLAUDE_CODE_OAUTH_TOKEN", secret_value)

    if args.protect:
        protect_branch(repo, branch, args.required_check)

    save_default_config(repo, branch)
    info("bootstrap complete")
    return 0


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    return "__pycache__" in parts or path.name == ".DS_Store" or path.suffix == ".pyc"


def copy_path(source: Path, target: Path, *, force: bool) -> None:
    if should_skip(source):
        return
    if source.is_dir():
        for child in source.iterdir():
            copy_path(child, target / child.name, force=force)
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

    for relative in TEMPLATE_PATHS:
        source = ROOT / relative
        if not source.exists():
            warn(f"template path missing, skipped: {relative}")
            continue
        copy_path(source, target / relative, force=args.force)

    if args.validate:
        run(["python3", "scripts/validate_shiki.py"], cwd=target)
        info("target Shiki validation passed")

    return 0


def cmd_install_command(args: argparse.Namespace) -> int:
    destination = Path(args.path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    destination.symlink_to(ROOT / "bin" / "shiki")
    info(f"installed command: {destination}")
    info("ensure the parent directory is on PATH")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    config = load_default_config()
    print(json.dumps({"root": str(ROOT), "config": config}, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shiki")
    subcommands = parser.add_subparsers(dest="command", required=True)

    github = subcommands.add_parser("bootstrap-github", help="Initialize and publish this Shiki repo to GitHub")
    github.add_argument("--repo", help="GitHub repository as OWNER/NAME")
    github.add_argument("--branch", default=None, help="Default branch, default main")
    github.add_argument("--private", action="store_true", help="Create a private repo")
    github.add_argument("--public", action="store_true", help=argparse.SUPPRESS)
    github.add_argument("--commit", action=argparse.BooleanOptionalAction, default=True)
    github.add_argument("--commit-message", default="shiki: bootstrap control plane")
    github.add_argument("--push", action=argparse.BooleanOptionalAction, default=True)
    github.add_argument("--set-secret", action=argparse.BooleanOptionalAction, default=True)
    github.add_argument("--secret-env", default="CLAUDE_CODE_OAUTH_TOKEN")
    github.add_argument("--protect", action=argparse.BooleanOptionalAction, default=True)
    github.add_argument("--required-check", action="append", default=list(DEFAULT_REQUIRED_CHECKS))
    github.set_defaults(func=cmd_bootstrap_github)

    target = subcommands.add_parser("install-target", help="Install Shiki template files into a target repo")
    target.add_argument("target", help="Target repository path")
    target.add_argument("--force", action="store_true", help="Overwrite existing files")
    target.add_argument("--validate", action=argparse.BooleanOptionalAction, default=True)
    target.set_defaults(func=cmd_install_target)

    install = subcommands.add_parser("install-command", help="Install a shiki command symlink")
    install.add_argument("--path", default="~/.local/bin/shiki")
    install.set_defaults(func=cmd_install_command)

    status = subcommands.add_parser("status", help="Show local Shiki CLI configuration")
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if getattr(args, "public", False):
        args.private = False
    try:
        return args.func(args)
    except ShikiError as error:
        print(f"[shiki] error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
