#!/usr/bin/env python3
"""GitHub CLI/API helpers for Shiki repository, secret, review, and PR evidence operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from shiki_git import github_origin
from shiki_process import ShikiError, first_line, info, print_json, require_tool, run, warn, write_json, shiki_path, target_path
from shiki_tasks import append_ledger, load_task, require_github_first_target, worktree_record

GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

def require_github_repo_slug(repo: str) -> None:
    if not GITHUB_REPO.match(repo):
        raise ShikiError("repo must be a GitHub slug like OWNER/NAME")


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


def claude_secret_remediation(repo: str, secret_env: str) -> str:
    return (
        f"Create a long-lived Claude Code token with `claude setup-token`, "
        f"export it as {secret_env}, then run "
        f"`gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo {repo}` or rerun Shiki init/start."
    )


def configure_claude_code_secret(repo: str, *, enabled: bool, secret_env: str) -> dict[str, Any]:
    status: dict[str, Any] = {
        "name": "CLAUDE_CODE_OAUTH_TOKEN",
        "enabled": enabled,
        "configured": False,
        "source": None,
        "remediation": "",
    }
    if not enabled:
        status["remediation"] = "Secret setup was disabled with --no-set-secret."
        return status

    secret_value = os.environ.get(secret_env, "")
    if not secret_value:
        status["remediation"] = claude_secret_remediation(repo, secret_env)
        raise ShikiError(
            f"missing required GitHub Actions secret source: {secret_env}. "
            "Claude Code login does not automatically expose a GitHub Actions token to Shiki. "
            f"{status['remediation']}"
        )

    set_secret(repo, "CLAUDE_CODE_OAUTH_TOKEN", secret_value)
    status["configured"] = True
    status["source"] = f"env:{secret_env}"
    return status


def github_secret_status(repo: str, secret_name: str) -> dict[str, Any]:
    result = run(["gh", "secret", "list", "--repo", repo], check=False)
    if result.returncode != 0:
        return {
            "name": secret_name,
            "checked": False,
            "configured": None,
            "error": first_line(result.stderr) or first_line(result.stdout),
        }
    names = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    return {
        "name": secret_name,
        "checked": True,
        "configured": secret_name in names,
    }


def protect_branch(repo: str, branch: str, required_checks: list[str], *, review_count: int) -> None:
    payload = {
        "required_status_checks": {
            "strict": True,
            "contexts": required_checks,
        },
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": review_count > 0,
            "required_approving_review_count": review_count,
        },
        "restrictions": None,
        "required_conversation_resolution": True,
        "required_linear_history": False,
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
        raise ShikiError(
            f"could not configure branch protection: {result.stderr.strip()}. "
            "Branch protection is required; rerun with --no-protect only for an explicit non-protected setup."
        )


def github_repo_from_origin(target: Path) -> str | None:
    origin = github_origin(target)
    if not origin:
        return None
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", origin)
    if not match:
        return None
    return match.group(1)


def parse_github_number(value: str, kind: str) -> int:
    pattern = rf"/{kind}/([0-9]+)"
    match = re.search(pattern, value)
    if not match:
        raise ShikiError(f"could not parse GitHub {kind} number from: {value}")
    return int(match.group(1))


def github_issue_body(task: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"## Shiki",
            f"Goal: {task['goal_id']}",
            f"Task: {task['id']}",
            "",
            "## Scope",
            task["scope"],
            "",
            "## Acceptance",
            *[f"- {check}" for check in task.get("acceptance_checks", [])],
            "",
            "## Locks",
            *[f"- {lock}" for lock in task.get("locks", [])],
            "",
            "## Runtime",
            str(task.get("assigned_runtime", "codex")),
        ]
    )


def github_pr_body(task: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"## Shiki",
            f"Goal: {task['goal_id']}",
            f"Task: {task['id']}",
            "CCA checklist profile: PR, TDD, V, CCA",
            "",
            "## Scope",
            task["scope"],
            "",
            "## Non-goals",
            *[f"- {item}" for item in task.get("non_goals", [])],
            "",
            "## Acceptance",
            *[f"- {check}" for check in task.get("acceptance_checks", [])],
            "",
            "## Evidence",
            "- python3 scripts/validate_shiki.py",
            "",
            "## Ledger evidence",
            *[f"- {entry}" for entry in task.get("ledger_evidence", [])],
            "",
            "## MergeGate",
            f"- Locks: {', '.join(task.get('locks', [])) or 'none'}",
            f"- Risk: {task.get('risk_level', 'low')}",
            "- CCA required: yes",
        ]
    )


def create_github_issue_for_task(target: Path, task_id: str) -> dict[str, Any]:
    require_tool("gh")
    task = load_task(target, task_id)
    result = run(
        [
            "gh",
            "issue",
            "create",
            "--title",
            f"{task['id']}: {task['title']}",
            "--body",
            github_issue_body(task),
        ],
        cwd=target,
    )
    url = result.stdout.strip().splitlines()[-1]
    issue_number = parse_github_number(url, "issues")
    task["github_issue"] = issue_number
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task["id"],
        ledger_type="handoff",
        summary=f"GitHub Issue #{issue_number} created for {task['id']}",
        evidence=[url],
        links=[url],
    )
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), task)
    return {"task_id": task["id"], "issue": issue_number, "url": url, "ledger_id": ledger_id}


def cmd_github_issue(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(create_github_issue_for_task(target, args.task_id))
    return 0


def create_github_pr_for_task(target: Path, task_id: str, *, base: str, head: str | None = None) -> dict[str, Any]:
    require_tool("gh")
    task = load_task(target, task_id)
    result = run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--head",
            head or task["expected_branch"],
            "--title",
            f"{task['id']}: {task['title']}",
            "--body",
            github_pr_body(task),
        ],
        cwd=target,
    )
    url = result.stdout.strip().splitlines()[-1]
    pr_number = parse_github_number(url, "pull")
    task["expected_pr"] = pr_number
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task["id"],
        ledger_type="handoff",
        summary=f"GitHub PR #{pr_number} created for {task['id']}",
        evidence=[url],
        links=[url],
    )
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), task)
    worktree = worktree_record(target, task["id"])
    if worktree:
        worktree["pr"] = pr_number
        write_json(shiki_path(target, "worktrees", f"{task['id']}.json"), worktree)
    return {"task_id": task["id"], "pr": pr_number, "url": url, "ledger_id": ledger_id}


def cmd_github_pr(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(create_github_pr_for_task(target, args.task_id, base=args.base, head=args.head))
    return 0
