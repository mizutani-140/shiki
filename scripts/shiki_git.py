#!/usr/bin/env python3
"""Local git helpers for Shiki bootstrap, staging, and worktrees."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from shiki_installer import manifest_stage_paths
from shiki_provider import ProviderConfig, ProviderConfigError, canonical_remote_url, canonicalize_remote_url, default_provider_config, provider_from_repo_json, remote_matches_provider
from shiki_process import ROOT, ShikiError, info, run

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


def existing_origin_url(path: Path) -> str | None:
    result = run(["git", "remote", "get-url", "origin"], cwd=path, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def canonical_github_remote_url(url: str) -> str:
    return canonicalize_remote_url(url)


def check_remote_adoption(
    repo: str,
    path: Path,
    *,
    adopt_existing_repo: bool = False,
    provider_config: ProviderConfig | None = None,
) -> None:
    if not path.exists() or not is_git_repo(path):
        return
    config = provider_config or default_provider_config(repo)
    remote_url = canonical_remote_url(config)
    current = existing_origin_url(path)
    if (
        current
        and not remote_matches_provider(current, config)
        and not adopt_existing_repo
    ):
        raise ShikiError(
            "origin already points to "
            f"{current}; refusing to rewrite it to {remote_url}. "
            "Pass --adopt-existing-repo to explicitly adopt this repository."
        )


def ensure_remote(
    repo: str,
    path: Path,
    *,
    adopt_existing_repo: bool = False,
    provider_config: ProviderConfig | None = None,
) -> None:
    config = provider_config or default_provider_config(repo)
    remote_url = canonical_remote_url(config)
    current = existing_origin_url(path)
    if current:
        if not remote_matches_provider(current, config):
            if not adopt_existing_repo:
                raise ShikiError(
                    "origin already points to "
                    f"{current}; refusing to rewrite it to {remote_url}. "
                    "Pass --adopt-existing-repo to explicitly adopt this repository."
                )
            run(["git", "remote", "set-url", "origin", remote_url], cwd=path)
            info(f"updated origin to {remote_url}")
        else:
            info(f"origin already configured: {current}")
        return
    run(["git", "remote", "add", "origin", remote_url], cwd=path)
    info(f"added origin {remote_url}")


def has_commits(path: Path) -> bool:
    return run(["git", "rev-parse", "--verify", "HEAD"], cwd=path, check=False).returncode == 0


def commit_manifest(path: Path, message: str) -> None:
    stage_paths = manifest_stage_paths(path)
    if not stage_paths:
        info("nothing to commit")
        return
    run(["git", "add", "--", *stage_paths], cwd=path)
    staged = run(["git", "diff", "--cached", "--quiet"], cwd=path, check=False)
    if staged.returncode == 0:
        info("nothing to commit")
        return
    run(["git", "commit", "-m", message], cwd=path)
    info(f"created commit: {message}")


def push_branch(path: Path, branch: str) -> None:
    run(["git", "push", "-u", "origin", branch], cwd=path)
    info(f"pushed {branch} to origin")


def github_origin(path: Path) -> str | None:
    result = run(["git", "remote", "get-url", "origin"], cwd=path, check=False)
    if result.returncode != 0:
        return None
    origin = result.stdout.strip()
    repo_config = path / ".shiki" / "repo.json"
    if repo_config.exists():
        try:
            config = provider_from_repo_json(json.loads(repo_config.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ProviderConfigError):
            return None
        return origin if remote_matches_provider(origin, config) else None
    try:
        canonical = canonicalize_remote_url(origin)
    except ValueError:
        return None
    if not canonical.startswith("https://github.com/"):
        return None
    return origin


def branch_exists(target: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=str(target),
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0
