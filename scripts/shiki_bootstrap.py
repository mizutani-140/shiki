#!/usr/bin/env python3
"""Bootstrap, init, preflight, and start orchestration for Shiki."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from shiki_config import branch_protection_review_count, configured_required_checks
from shiki_contracts import DEFAULT_REQUIRED_CHECKS
from shiki_git import check_remote_adoption, commit_manifest, current_branch, ensure_git_repo, ensure_remote, github_origin, is_git_repo, push_branch
from shiki_github import claude_secret_remediation, configure_claude_code_secret, configure_workflow_permissions, create_github_issue_for_task, ensure_github_repo, github_secret_status, protect_branch, require_github_repo_slug, set_default_branch
from shiki_installer import install_template
from shiki_provider import ProviderConfig, ProviderConfigError, canonical_remote_url, github_env, provider_config_as_json, provider_from_values
from shiki_process import ROOT, ShikiError, ensure_control_dirs, info, load_default_config
from shiki_process import print_json, prompt_default, prompt_list, prompt_value, read_json, require_tool, resolve_engineering_skills_dir, run, save_default_config, shiki_path, start_target_value, target_path, utc_now, validate_local_shiki, write_json
from shiki_tasks import append_ledger, load_task, next_control_id, orchestrate_plan, require_github_first_target, shiki_path as _unused_shiki_path, write_handoff

START_QUESTIONS = [
    "GitHub repo slug (OWNER/REPO)",
    "Project name",
    "Goal title",
    "Outcome / success result",
    "Completion conditions",
    "Non-goals",
    "First vertical-slice task title",
    "First task scope",
    "First task acceptance checks",
    "First task locks",
]

def execution_confirmed(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "execute", False) or getattr(args, "i_understand", False))


def provider_config_from_args(args: argparse.Namespace, repo: str) -> ProviderConfig:
    try:
        return provider_from_values(
            repo=repo,
            provider=getattr(args, "provider", None),
            host=getattr(args, "github_host", None),
            protocol=getattr(args, "remote_protocol", None),
            api_base_url=getattr(args, "github_api_url", None),
        )
    except ProviderConfigError as error:
        raise ShikiError(str(error)) from error


def bootstrap_dry_run_lines(
    *,
    command: str,
    target: Path,
    repo: str,
    branch: str,
    visibility: str,
    commit: bool,
    push: bool,
    set_secret_enabled: bool,
    protect: bool,
    required_checks: list[str],
    provider_config: ProviderConfig,
    platform: bool = False,
) -> list[str]:
    target_label = "platform repository" if platform else str(target)
    lines = [
        "dry-run: no bootstrap/init mutations were executed",
        f"dry-run: rerun with --execute to apply these operations for {command}",
        f"filesystem: create target directory {target_label}",
    ]
    if platform:
        lines.append("filesystem: validate local Shiki platform files")
    else:
        lines.extend(
            [
                f"filesystem: install Shiki template files into {target_label}",
                f"filesystem: write .shiki/repo.json for {repo}",
                "filesystem: ensure .shiki state directories",
            ]
        )
    lines.extend(
        [
            f"provider: {provider_config.provider}",
            f"github-host: {provider_config.host}",
            f"remote-protocol: {provider_config.protocol}",
            f"github-api: use {provider_config.api_base_url}",
            f"git: initialize repository on {branch}",
            f"git: configure origin {canonical_remote_url(provider_config)}",
            f"github-repo: create or reuse {repo} as {visibility}",
        ]
    )
    if commit:
        lines.append("commit: create manifest commit")
    else:
        lines.append("commit: skipped by --no-commit")
    if push:
        lines.append(f"push: push {branch} to origin")
        lines.append(f"default-branch: set {branch}")
    else:
        lines.append("push: skipped by --no-push")
        lines.append("default-branch: skipped because push is disabled")
    if set_secret_enabled:
        lines.append("secret: set CLAUDE_CODE_OAUTH_TOKEN")
    else:
        lines.append("secret: skipped by --no-set-secret")
    if protect:
        lines.append(f"branch-protection: configure required checks {', '.join(required_checks)}")
        lines.append("workflow-permissions: allow GitHub Actions to create and approve pull requests")
    else:
        lines.append("branch-protection: skipped by --no-protect")
        lines.append("workflow-permissions: skipped by --no-protect")
    return lines


def print_bootstrap_dry_run(lines: list[str]) -> None:
    for line in lines:
        info(line)


def cmd_bootstrap_github(args: argparse.Namespace) -> int:
    require_tool("git")
    require_tool("gh")

    config = load_default_config()
    repo = args.repo or config.get("repo")
    if not repo:
        raise ShikiError("missing --repo OWNER/NAME and no default repo configured")
    require_github_repo_slug(repo)
    provider_config = provider_config_from_args(args, repo)

    branch = args.branch or config.get("default_branch") or "main"
    visibility = "private" if args.private else "public"

    if not execution_confirmed(args):
        check_remote_adoption(repo, ROOT, adopt_existing_repo=args.adopt_existing_repo, provider_config=provider_config)
        print_bootstrap_dry_run(
            bootstrap_dry_run_lines(
                command="bootstrap-platform",
                target=ROOT,
                repo=repo,
                branch=branch,
                visibility=visibility,
                commit=args.commit,
                push=args.push,
                set_secret_enabled=args.set_secret,
                protect=args.protect,
                required_checks=args.required_check or configured_required_checks(ROOT, DEFAULT_REQUIRED_CHECKS),
                provider_config=provider_config,
                platform=True,
            )
        )
        return 0

    validate_local_shiki()
    run(["gh", "auth", "status"], env=github_env(provider_config))
    ensure_git_repo(ROOT, branch)
    ensure_github_repo(repo, visibility, provider_config=provider_config)
    ensure_remote(repo, ROOT, adopt_existing_repo=args.adopt_existing_repo, provider_config=provider_config)

    active_branch = current_branch(ROOT)
    if active_branch != branch:
        run(["git", "checkout", "-B", branch], cwd=ROOT)

    if args.commit:
        commit_manifest(ROOT, args.commit_message)

    if args.push:
        push_branch(ROOT, branch)
        set_default_branch(repo, branch, provider_config=provider_config)

    configure_claude_code_secret(repo, enabled=args.set_secret, secret_env=args.secret_env, provider_config=provider_config)

    if args.protect:
        required_checks = args.required_check or configured_required_checks(ROOT, DEFAULT_REQUIRED_CHECKS)
        protect_branch(repo, branch, required_checks, review_count=branch_protection_review_count(ROOT), provider_config=provider_config)
        configure_workflow_permissions(repo, provider_config=provider_config)

    save_default_config(repo, branch)
    info("bootstrap complete")
    return 0


def write_target_repo_config(target: Path, repo: str, branch: str, provider_config: ProviderConfig) -> None:
    payload = {
        "source_of_truth": "github",
        "default_branch": branch,
        "mirror": ".shiki",
    }
    payload.update(provider_config_as_json(provider_config))
    path = target / ".shiki" / "repo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    info(f"wrote target GitHub config: {path}")


def cmd_init(args: argparse.Namespace) -> int:
    require_tool("git")
    require_tool("gh")

    target = Path(args.target).expanduser().resolve()

    if not args.repo:
        raise ShikiError("shiki init requires --repo OWNER/NAME because Shiki is GitHub-first")
    repo = args.repo
    require_github_repo_slug(repo)
    provider_config = provider_config_from_args(args, repo)

    branch = args.branch
    visibility = "private" if args.private else "public"

    if not execution_confirmed(args):
        check_remote_adoption(repo, target, adopt_existing_repo=args.adopt_existing_repo, provider_config=provider_config)
        print_bootstrap_dry_run(
            bootstrap_dry_run_lines(
                command="init",
                target=target,
                repo=repo,
                branch=branch,
                visibility=visibility,
                commit=args.commit,
                push=args.push,
                set_secret_enabled=args.set_secret,
                protect=args.protect,
                required_checks=args.required_check or configured_required_checks(target, DEFAULT_REQUIRED_CHECKS),
                provider_config=provider_config,
            )
        )
        return 0

    target.mkdir(parents=True, exist_ok=True)
    run(["gh", "auth", "status"], env=github_env(provider_config))
    ensure_git_repo(target, branch)
    ensure_github_repo(repo, visibility, provider_config=provider_config)
    ensure_remote(repo, target, adopt_existing_repo=args.adopt_existing_repo, provider_config=provider_config)
    install_template(target, force=args.force, validate=args.validate)
    write_target_repo_config(target, repo, branch, provider_config)

    active_branch = current_branch(target)
    if active_branch != branch:
        run(["git", "checkout", "-B", branch], cwd=target)

    if args.commit:
        commit_manifest(target, args.commit_message)

    if args.push:
        push_branch(target, branch)
        set_default_branch(repo, branch, provider_config=provider_config)

    configure_claude_code_secret(repo, enabled=args.set_secret, secret_env=args.secret_env, provider_config=provider_config)

    if args.protect:
        required_checks = args.required_check or configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
        protect_branch(repo, branch, required_checks, review_count=branch_protection_review_count(target), provider_config=provider_config)
        configure_workflow_permissions(repo, provider_config=provider_config)

    info("GitHub-first init complete")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser().resolve()
    blocking: list[str] = []

    if not is_git_repo(target):
        blocking.append("not a git repository")
    elif args.require_github and not github_origin(target):
        blocking.append("missing GitHub origin")

    repo_config = target / ".shiki" / "repo.json"
    if args.require_github and not repo_config.exists():
        blocking.append("missing .shiki/repo.json GitHub config")

    result = {
        "target": str(target),
        "github_required": args.require_github,
        "status": "blocked" if blocking else "ready",
        "blocking_reasons": blocking,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if blocking else 0


def load_start_answers(args: argparse.Namespace) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    if args.answers_file:
        answers = read_json(Path(args.answers_file).expanduser().resolve())

    repo = args.repo or answers.get("repo")
    goal = args.goal or answers.get("goal") or answers.get("title")
    outcome = args.outcome or answers.get("outcome")
    project_name = args.project_name or answers.get("project_name") or goal
    skills_dir = resolve_engineering_skills_dir(args.skills_dir or answers.get("skills_dir"))

    repo = prompt_value("GitHub repo slug (OWNER/REPO)", repo)
    require_github_repo_slug(repo)
    goal = prompt_value("Goal title", goal)
    outcome = prompt_value("Outcome / success result", outcome)
    project_name = prompt_default("Project name", project_name) if project_name else prompt_value("Project name", project_name)

    completion_conditions = args.completion_condition or answers.get("completion_conditions") or []
    if not completion_conditions:
        completion_conditions = prompt_list("Completion conditions")
    if not completion_conditions:
        completion_conditions = [outcome]

    non_goals = args.non_goal or answers.get("non_goals") or []
    if not non_goals:
        non_goals = prompt_list("Non-goals")

    required_skills = args.required_skill or answers.get("required_skills") or [
        "grill-with-docs",
        "to-prd",
        "to-issues",
        "tdd",
    ]
    tasks = answers.get("tasks")
    if not tasks:
        if sys.stdin.isatty():
            task_title = prompt_default("First vertical-slice task title", args.task_title or f"Implement first vertical slice for {goal}")
            task_scope = prompt_default("First task scope", args.task_scope or f"Create the smallest end-to-end implementation path for {outcome}")
            acceptance_checks = prompt_list("First task acceptance checks", args.acceptance_check) or [f"User can verify: {outcome}"]
            locks = prompt_list("First task locks", args.lock) or ["path:**/*"]
        else:
            task_title = args.task_title or f"Implement first vertical slice for {goal}"
            task_scope = args.task_scope or f"Create the smallest end-to-end implementation path for {outcome}"
            acceptance_checks = args.acceptance_check or [f"User can verify: {outcome}"]
            locks = args.lock or ["path:**/*"]
        tasks = [
            {
                "title": task_title,
                "scope": task_scope,
                "acceptance_checks": acceptance_checks,
                "locks": locks,
                "required_skills": ["tdd", "code-review"],
            }
        ]

    approved = bool(getattr(args, "approve_spec_freeze", False))
    spec_freeze_source = "shiki start --approve-spec-freeze flag"
    if not approved and answers.get("approve_spec_freeze"):
        approved = True
        spec_freeze_source = "answers file approve_spec_freeze: true"
    if not approved and sys.stdin.isatty():
        reply = prompt_value("Approve these requirements and freeze the spec? (yes/no)")
        if reply.strip().lower() in {"yes", "y"}:
            approved = True
            spec_freeze_source = "shiki start interactive approval question"
    if not approved:
        raise ShikiError(
            "Spec Freeze was not approved. Re-run with --approve-spec-freeze, set "
            "approve_spec_freeze: true in the answers file, or answer yes interactively. "
            "Plans cannot run without an operator-approved spec_freeze (ADR 0009)."
        )

    return {
        "repo": repo,
        "project_name": project_name,
        "goal": goal,
        "outcome": outcome,
        "completion_conditions": completion_conditions,
        "non_goals": non_goals,
        "risk_level": args.risk_level or answers.get("risk_level", "medium"),
        "required_skills": required_skills,
        "skills_dir": skills_dir,
        "spec_freeze_source": spec_freeze_source,
        "tasks": tasks,
    }


def plan_from_start_answers(answers: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": answers["goal"],
        "outcome": answers["outcome"],
        "completion_conditions": answers["completion_conditions"],
        "non_goals": answers["non_goals"],
        "risk_level": answers["risk_level"],
        "required_skills": answers["required_skills"],
        "grill_with_docs": {
            "status": "complete",
            "source": "shiki start interactive questions",
            "decisions": [
                f"Project name: {answers['project_name']}",
                f"GitHub repository: {answers['repo']}",
                f"Engineering skills directory: {answers['skills_dir']}",
                "Use GitHub-first Shiki setup.",
                "Use engineering skills as mandatory planning and implementation gates.",
                "Use a guided one-question-at-a-time start flow before creating the Task DAG.",
            ],
        },
        "spec_freeze": {
            "status": "frozen",
            "approved_by": "operator",
            "source": answers["spec_freeze_source"],
        },
        "skill_gate": {
            "skills_dir": answers["skills_dir"],
            "required_skills": answers["required_skills"],
            "entry_policy": "Ask missing Goal and repository values one at a time, then run shiki start as the single command.",
        },
        "tasks": answers["tasks"],
    }


def initialize_target_from_start(args: argparse.Namespace, target: Path, repo: str) -> None:
    init_args = argparse.Namespace(
        target=str(target),
        repo=repo,
        branch=args.branch,
        private=args.private,
        public=not args.private,
        force=args.force,
        validate=args.validate,
        commit=args.commit,
        commit_message=args.commit_message,
        push=args.push,
        set_secret=args.set_secret,
        secret_env=args.secret_env,
        protect=args.protect,
        required_check=args.required_check,
        adopt_existing_repo=args.adopt_existing_repo,
        execute=args.execute,
        i_understand=args.i_understand,
        provider=args.provider,
        github_host=args.github_host,
        github_api_url=args.github_api_url,
        remote_protocol=args.remote_protocol,
    )
    cmd_init(init_args)


def create_issues_for_dispatchable_tasks(target: Path, task_ids: list[str]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for task_id in task_ids:
        issues.append(create_github_issue_for_task(target, task_id))
    return issues


def cmd_start(args: argparse.Namespace) -> int:
    target = target_path(start_target_value(args))
    answers = load_start_answers(args)

    already_initialized = (
        target.exists()
        and (target / ".shiki" / "repo.json").exists()
        and is_git_repo(target)
        and github_origin(target)
    )
    if not already_initialized:
        initialize_target_from_start(args, target, answers["repo"])
        if not execution_confirmed(args):
            return 0
    else:
        require_github_first_target(target)
        ensure_control_dirs(target)

    plan = plan_from_start_answers(answers)
    plan_id = next_control_id(target, "P")
    plan["id"] = plan_id
    plan["status"] = "ingested"
    plan["source_file"] = "shiki start"
    plan["ingested_at"] = utc_now()
    plan_file = shiki_path(target, "plans", f"{plan_id}.json")
    write_json(plan_file, plan)

    result = orchestrate_plan(target, plan)
    issues: list[dict[str, Any]] = []
    if args.create_issues and result["dispatchable_task_ids"]:
        issues = create_issues_for_dispatchable_tasks(target, result["dispatchable_task_ids"])

    handoffs: list[str] = []
    if args.create_handoffs:
        for task_id in result["dispatchable_task_ids"]:
            task = load_task(target, task_id)
            handoff_file = write_handoff(
                target,
                f"{task_id}-task.md",
                "\n".join(
                    [
                        f"# Codex Task Handoff: {task_id}",
                        "",
                        f"Goal: {task['goal_id']}",
                        f"Task: {task_id}",
                        f"Branch: {task['expected_branch']}",
                        "",
                        "## Scope",
                        task["scope"],
                        "",
                        "## Required Skills",
                        *[f"- {skill}" for skill in task.get("required_skills", [])],
                        "",
                        "## Engineering Skills Directory",
                        answers["skills_dir"],
                        "",
                        "## Acceptance Checks",
                        *[f"- {check}" for check in task.get("acceptance_checks", [])],
                        "",
                    ]
                ),
            )
            handoffs.append(str(handoff_file.relative_to(target)))

    start_id = next_control_id(target, "START")
    start_file = shiki_path(target, "starts", f"{start_id}.json")
    provider_config = provider_config_from_args(args, answers["repo"])
    claude_secret = github_secret_status(answers["repo"], "CLAUDE_CODE_OAUTH_TOKEN", provider_config=provider_config)
    if claude_secret.get("configured") is False:
        claude_secret["remediation"] = claude_secret_remediation(answers["repo"], args.secret_env)
    start_record = {
        "id": start_id,
        "repo": answers["repo"],
        "project_name": answers["project_name"],
        "skills_dir": answers["skills_dir"],
        "questions": START_QUESTIONS,
        "plan_id": plan_id,
        "goal_id": result["goal_id"],
        "run_id": result["run_id"],
        "dispatchable_task_ids": result["dispatchable_task_ids"],
        "issues": issues,
        "handoffs": handoffs,
        "claude_code_oauth_secret": claude_secret,
        "created_at": utc_now(),
    }
    write_json(start_file, start_record)
    ledger_id = append_ledger(
        target,
        goal_id=result["goal_id"],
        ledger_type="handoff",
        summary=f"Shiki start {start_id} initialized {answers['repo']}",
        evidence=[str(start_file.relative_to(target)), str(plan_file.relative_to(target))],
        links=[issue["url"] for issue in issues],
    )

    if args.commit:
        commit_manifest(target, "shiki: start project control plane")
    if args.push:
        push_branch(target, args.branch)

    output = {
        "start_id": start_id,
        "repo": answers["repo"],
        "project_name": answers["project_name"],
        "skills_dir": answers["skills_dir"],
        "plan_id": plan_id,
        "goal_id": result["goal_id"],
        "run_id": result["run_id"],
        "dispatchable_task_ids": result["dispatchable_task_ids"],
        "issues": issues,
        "handoffs": handoffs,
        "claude_code_oauth_secret": claude_secret,
        "start_file": str(start_file),
        "ledger_id": ledger_id,
    }
    print_json(output)
    return 0
