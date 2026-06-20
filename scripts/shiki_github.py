#!/usr/bin/env python3
"""GitHub CLI/API helpers for Shiki repository, secret, review, and PR evidence operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from shiki_git import github_origin
from shiki_provider import ProviderConfig, ProviderConfigError, canonicalize_remote_url, default_provider_config, github_env, provider_from_repo_json, repo_api_path, validate_repo_slug
from shiki_process import ShikiError, first_line, info, print_json, read_json, require_tool, run, warn, write_json, shiki_path, target_path
from shiki_tasks import append_ledger, load_task, require_github_first_target, worktree_record

GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

def require_github_repo_slug(repo: str) -> None:
    try:
        validate_repo_slug(repo)
    except ValueError as error:
        raise ShikiError("repo must be a GitHub slug like OWNER/NAME") from error


def github_repo_exists(repo: str, provider_config: ProviderConfig | None = None) -> bool:
    config = provider_config or default_provider_config(repo)
    return run(["gh", "repo", "view", config.repo, "--json", "name"], env=github_env(config), check=False).returncode == 0


def ensure_github_repo(repo: str, visibility: str, provider_config: ProviderConfig | None = None) -> None:
    config = provider_config or default_provider_config(repo)
    if github_repo_exists(repo, provider_config=config):
        info(f"GitHub repository already exists: {config.repo}")
        return
    args = ["gh", "repo", "create", config.repo]
    args.append(f"--{visibility}")
    args.extend(["--confirm"])
    run(args, env=github_env(config))
    info(f"created GitHub repository: {config.repo}")


def set_default_branch(repo: str, branch: str, provider_config: ProviderConfig | None = None) -> None:
    config = provider_config or default_provider_config(repo)
    result = run(
        ["gh", "api", repo_api_path(config), "-X", "PATCH", "-f", f"default_branch={branch}"],
        env=github_env(config),
        check=False,
    )
    if result.returncode == 0:
        info(f"set default branch to {branch}")
    else:
        warn(f"could not set default branch: {result.stderr.strip()}")


def set_secret(repo: str, secret_name: str, value: str, provider_config: ProviderConfig | None = None) -> None:
    config = provider_config or default_provider_config(repo)
    run(["gh", "secret", "set", secret_name, "--repo", config.repo], input_text=value, env=github_env(config))
    info(f"set GitHub secret: {secret_name}")


def claude_secret_remediation(repo: str, secret_env: str) -> str:
    return (
        f"Create a long-lived Claude Code token with `claude setup-token`, "
        f"export it as {secret_env}, then run "
        f"`gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo {repo}` or rerun Shiki init/start."
    )


def configure_claude_code_secret(
    repo: str,
    *,
    enabled: bool,
    secret_env: str,
    provider_config: ProviderConfig | None = None,
) -> dict[str, Any]:
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

    set_secret(repo, "CLAUDE_CODE_OAUTH_TOKEN", secret_value, provider_config=provider_config)
    status["configured"] = True
    status["source"] = f"env:{secret_env}"
    return status


def github_secret_status(repo: str, secret_name: str, provider_config: ProviderConfig | None = None) -> dict[str, Any]:
    config = provider_config or default_provider_config(repo)
    result = run(["gh", "secret", "list", "--repo", config.repo], env=github_env(config), check=False)
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


def protect_branch(
    repo: str,
    branch: str,
    required_checks: list[str],
    *,
    review_count: int,
    provider_config: ProviderConfig | None = None,
) -> None:
    config = provider_config or default_provider_config(repo)
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
            repo_api_path(config, f"branches/{branch}/protection"),
            "-X",
            "PUT",
            "--input",
            "-",
        ],
        input_text=json.dumps(payload),
        env=github_env(config),
        check=False,
    )
    if result.returncode == 0:
        info(f"configured branch protection for {branch}")
    else:
        raise ShikiError(
            f"could not configure branch protection: {result.stderr.strip()}. "
            "Branch protection is required; rerun with --no-protect only for an explicit non-protected setup."
        )


def configure_workflow_permissions(
    repo: str,
    *,
    can_approve_pull_requests: bool = True,
    default_permissions: str = "read",
    provider_config: ProviderConfig | None = None,
) -> None:
    """Configure repository Actions workflow permissions for the CCA Review Bridge.

    Sets the default workflow token permission (``read``) and whether GitHub
    Actions may create and approve pull request reviews. The Review Bridge needs
    ``can_approve_pull_request_reviews=true`` to satisfy ``required_review: true``
    in solo operation after CCA returns ``complete`` (see ADR 0013 and
    ``docs/agents/decision-control.md``).

    Mirrors ``protect_branch``'s ``gh api ... -X PUT --input -`` pattern but
    warns instead of raising on failure: branch protection is the hard gate, and
    this default can also be set in repository Settings -> Actions -> General, so
    a missing Actions-admin scope must not abort an otherwise-complete bootstrap.
    """
    config = provider_config or default_provider_config(repo)
    payload = {
        "default_workflow_permissions": default_permissions,
        "can_approve_pull_request_reviews": can_approve_pull_requests,
    }
    result = run(
        [
            "gh",
            "api",
            repo_api_path(config, "actions/permissions/workflow"),
            "-X",
            "PUT",
            "--input",
            "-",
        ],
        input_text=json.dumps(payload),
        env=github_env(config),
        check=False,
    )
    if result.returncode == 0:
        info(
            "configured workflow permissions: "
            f"default={default_permissions}, can approve pull requests={can_approve_pull_requests}"
        )
    else:
        warn(
            f"could not configure workflow permissions: {result.stderr.strip()}. "
            "The CCA Review Bridge needs GitHub Actions allowed to create and approve "
            "pull requests; set default workflow permissions to read and enable "
            '"Allow GitHub Actions to create and approve pull requests" under '
            "repository Settings -> Actions -> General, or rerun Shiki init/start."
        )


def github_repo_from_origin(target: Path) -> str | None:
    origin = github_origin(target)
    if not origin:
        return None
    try:
        canonical = canonicalize_remote_url(origin)
    except ProviderConfigError:
        return None
    return "/".join(canonical.removeprefix("https://").split("/", 1)[1:])


def parse_github_number(value: str, kind: str) -> int:
    pattern = rf"/{kind}/([0-9]+)"
    match = re.search(pattern, value)
    if not match:
        raise ShikiError(f"could not parse GitHub {kind} number from: {value}")
    return int(match.group(1))


def target_provider_config(target: Path) -> ProviderConfig | None:
    repo_config = target / ".shiki" / "repo.json"
    if not repo_config.exists():
        return None
    try:
        return provider_from_repo_json(read_json(repo_config))
    except (ProviderConfigError, ShikiError) as error:
        raise ShikiError(f"{repo_config}: invalid provider config: {error}") from error


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


def pre_pr_code_review_section(task: dict[str, Any]) -> list[str]:
    """The PR-12 ``## Pre-PR code review`` body section (ADR 0011).

    Rendered from the loop-recorded ``pre_pr_code_review`` block — the verdict of
    the independent read-only reviewer the loop ran before opening the PR. A PR is
    only ever opened on a ``clean`` verdict (a blocking/failed review fail-closes
    the loop before create_pr), so this section documents that the independent
    gate passed and links its ledger; it is never the implementer self-attesting.
    """
    review = task.get("pre_pr_code_review") or {}
    lines = ["## Pre-PR code review"]
    if not review:
        # Defensive: the section is always present for CCA PR-12, even when the
        # verdict block was not recorded (e.g. a manually opened PR).
        lines.append("- No independent pre-PR review verdict recorded.")
        return lines
    verdict = str(review.get("verdict", "unknown"))
    lines.append(f"- Verdict: {verdict}")
    lines.append("- Independent read-only reviewer (claude -p, read tools only) — ADR 0011")
    ledger_id = review.get("ledger_id")
    if ledger_id:
        lines.append(f"- Ledger: {ledger_id}")
    findings = review.get("findings") or []
    if findings:
        for finding in findings:
            title = str(finding.get("title", "finding")) if isinstance(finding, dict) else str(finding)
            lines.append(f"- Finding: {title}")
    else:
        lines.append("- Findings: none")
    return lines
def _task_test_command_for_body(task: dict[str, Any]) -> str:
    """The structured test command for the PR body's loop-observed TDD line.

    Mirrors the loop's ``task_test_command`` selection (the task's
    ``test_command`` or the safe unittest-discover default) so the PR records
    exactly what the loop exec'd. ``acceptance_checks`` is free-form prose and is
    never exec'd, so it is never shown here as the command.
    """
    from shiki_tasks import DEFAULT_TEST_COMMAND

    command = task.get("test_command")
    if isinstance(command, str) and command.strip():
        return command
    return DEFAULT_TEST_COMMAND


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
            *pre_pr_code_review_section(task),
            "## TDD evidence (loop-observed)",
            "- The goal loop ran the task's tests in the worktree and recorded a "
            "type:check ledger (skill tdd, EXEC evidence) before opening this PR "
            "(ADR 0011); a red run blocks the PR.",
            f"- Test command: {_task_test_command_for_body(task)}",
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
    config = target_provider_config(target)
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
        env=github_env(config) if config else None,
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
    config = target_provider_config(target)
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
        env=github_env(config) if config else None,
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
