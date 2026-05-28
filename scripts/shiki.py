#!/usr/bin/env python3
"""Shiki control CLI.

This file intentionally uses only the Python standard library so the bootstrap
command can run before a target repository has installed dependencies.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from typing import Any
import shutil
import subprocess
import sys
import re
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
    ".claude/commands/shiki.md",
    ".codex/skills/shiki/SKILL.md",
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

DEFAULT_GLOBAL_COMMAND_PATH = "~/.local/bin/shiki"
DEFAULT_CLAUDE_COMMAND_PATH = "~/.claude/commands/shiki.md"
DEFAULT_CODEX_SKILL_PATH = "~/.codex/skills/shiki/SKILL.md"
TARGET_STATE_DIRECTORIES = [
    ".shiki/goals",
    ".shiki/tasks",
    ".shiki/dag",
    ".shiki/ledger",
    ".shiki/locks",
    ".shiki/worktrees",
    ".shiki/repairs",
    ".shiki/reports",
]
GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


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
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 0,
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
        warn(f"could not configure branch protection: {result.stderr.strip()}")
        warn("configure required checks manually if your GitHub plan or permissions block the API")


def validate_local_shiki() -> None:
    run(["python3", "scripts/validate_shiki.py"], cwd=ROOT)
    info("local Shiki validation passed")


def validate_target_shiki(target: Path) -> None:
    run(["python3", "scripts/validate_shiki.py"], cwd=target)
    info("target Shiki validation passed")


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


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def target_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def shiki_path(target: Path, *parts: str) -> Path:
    return target / ".shiki" / Path(*parts)


def ensure_control_dirs(target: Path) -> None:
    for relative in TARGET_STATE_DIRECTORIES:
        (target / relative).mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ShikiError(f"missing file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ShikiError(f"expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def scan_ids(target: Path, prefix: str) -> list[int]:
    pattern = re.compile(rf"\b{re.escape(prefix)}-([0-9]{{4,}})\b")
    numbers: list[int] = []
    base = target / ".shiki"
    if not base.exists():
        return numbers
    for path in base.rglob("*.json"):
        for match in pattern.finditer(path.name):
            numbers.append(int(match.group(1)))
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in pattern.finditer(text):
            numbers.append(int(match.group(1)))
    return numbers


def next_control_id(target: Path, prefix: str) -> str:
    number = max(scan_ids(target, prefix), default=0) + 1
    return f"{prefix}-{number:04d}"


def load_task(target: Path, task_id: str) -> dict[str, Any]:
    return read_json(shiki_path(target, "tasks", f"{task_id}.json"))


def load_goal(target: Path, goal_id: str) -> dict[str, Any]:
    return read_json(shiki_path(target, "goals", f"{goal_id}.json"))


def task_files(target: Path) -> list[Path]:
    directory = shiki_path(target, "tasks")
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def tasks_for_goal(target: Path, goal_id: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for path in task_files(target):
        data = read_json(path)
        if data.get("goal_id") == goal_id:
            tasks.append(data)
    return tasks


def has_active_lock_conflict(target: Path, task_id: str, locks: list[str]) -> list[str]:
    conflicts: list[str] = []
    directory = shiki_path(target, "locks")
    if not directory.exists():
        return conflicts
    requested = set(locks)
    for path in sorted(directory.glob("*.json")):
        data = read_json(path)
        if data.get("task_id") == task_id or data.get("state") != "active":
            continue
        overlap = requested.intersection(set(data.get("locks", [])))
        for lock in sorted(overlap):
            conflicts.append(f"{lock} held by {data.get('task_id')}")
    return conflicts


def lock_record(target: Path, task_id: str) -> dict[str, Any] | None:
    path = shiki_path(target, "locks", f"{task_id}.json")
    if not path.exists():
        return None
    return read_json(path)


def worktree_record(target: Path, task_id: str) -> dict[str, Any] | None:
    path = shiki_path(target, "worktrees", f"{task_id}.json")
    if not path.exists():
        return None
    return read_json(path)


def require_github_first_target(target: Path) -> None:
    if not (target / ".shiki").exists():
        raise ShikiError(f"missing .shiki mirror in {target}; run shiki init TARGET --repo OWNER/NAME")
    if not is_git_repo(target):
        raise ShikiError("Shiki control commands require a git repository; run shiki init TARGET --repo OWNER/NAME")
    if not github_origin(target):
        raise ShikiError("Shiki control commands require a GitHub origin; run shiki init TARGET --repo OWNER/NAME")


def append_ledger(
    target: Path,
    *,
    goal_id: str,
    ledger_type: str,
    summary: str,
    evidence: list[str],
    task_id: str | None = None,
    links: list[str] | None = None,
) -> str:
    ledger_id = next_control_id(target, "L")
    payload: dict[str, Any] = {
        "id": ledger_id,
        "timestamp": utc_now(),
        "goal_id": goal_id,
        "task_id": task_id,
        "type": ledger_type,
        "actor": "shiki-cli",
        "summary": summary,
        "evidence": evidence,
        "links": links or [],
    }
    write_json(shiki_path(target, "ledger", f"{ledger_id}.json"), payload)
    return ledger_id


def cmd_goal_create(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)

    goal_id = next_control_id(target, "G")
    completion_conditions = args.completion_condition or [args.outcome]
    acceptance_evidence = args.acceptance_evidence or [
        "GitHub Issue records the goal.",
        "Task DAG is registered in .shiki/dag.",
        "CCA verdict and MergeGate evidence are recorded before completion.",
    ]
    payload = {
        "id": goal_id,
        "github_issue": args.github_issue,
        "title": args.title,
        "outcome": args.outcome,
        "completion_conditions": completion_conditions,
        "non_goals": args.non_goal or [],
        "risk_level": args.risk_level,
        "required_skills": args.required_skill or [],
        "acceptance_evidence": acceptance_evidence,
        "status": "planned",
        "created_at": utc_now(),
    }
    goal_file = shiki_path(target, "goals", f"{goal_id}.json")
    write_json(goal_file, payload)
    ledger_id = append_ledger(
        target,
        goal_id=goal_id,
        ledger_type="goal-created",
        summary=f"Goal registered: {args.title}",
        evidence=[str(goal_file.relative_to(target))],
    )
    print_json({"goal_id": goal_id, "goal_file": str(goal_file), "ledger_id": ledger_id, "status": "planned"})
    return 0


def cmd_issue_plan(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)
    load_goal(target, args.goal_id)

    task_id = next_control_id(target, "T")
    branch = args.expected_branch or f"shiki/{task_id.lower()}-{slugify(args.title)}"
    ledger_id = append_ledger(
        target,
        goal_id=args.goal_id,
        task_id=task_id,
        ledger_type="task-registered",
        summary=f"Task registered: {args.title}",
        evidence=[f".shiki/tasks/{task_id}.json"],
    )
    payload = {
        "id": task_id,
        "goal_id": args.goal_id,
        "github_issue": args.github_issue,
        "title": args.title,
        "scope": args.scope,
        "non_goals": args.non_goal or [],
        "dependencies": args.dependency or [],
        "locks": args.lock or [],
        "assigned_runtime": args.runtime,
        "risk_level": args.risk_level,
        "required_skills": args.required_skill or [],
        "acceptance_checks": args.acceptance_check,
        "expected_branch": branch,
        "expected_pr": args.expected_pr,
        "ledger_evidence": [ledger_id],
        "status": "planned",
    }
    task_file = shiki_path(target, "tasks", f"{task_id}.json")
    write_json(task_file, payload)

    dag_file = shiki_path(target, "dag", f"{args.goal_id}.json")
    dag = {"goal_id": args.goal_id, "nodes": [], "edges": []}
    if dag_file.exists():
        dag = read_json(dag_file)
    nodes = list(dict.fromkeys([*dag.get("nodes", []), task_id]))
    existing_edges = dag.get("edges", [])
    new_edges = [{"from": dep, "to": task_id, "reason": "declared dependency"} for dep in args.dependency or []]
    dag.update({"nodes": nodes, "edges": existing_edges + new_edges})
    write_json(dag_file, dag)

    print_json({"task_id": task_id, "task_file": str(task_file), "dag_file": str(dag_file), "ledger_id": ledger_id})
    return 0


def cmd_lock_acquire(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)
    task = load_task(target, args.task_id)
    locks = list(task.get("locks", []))
    conflicts = has_active_lock_conflict(target, args.task_id, locks)

    result = {
        "task_id": args.task_id,
        "locks_requested": locks,
        "locks_granted": not conflicts,
        "blocking_reasons": conflicts,
    }
    if conflicts:
        print_json(result)
        return 1

    record = {
        "task_id": args.task_id,
        "goal_id": task["goal_id"],
        "locks": locks,
        "state": "active",
        "owner": args.owner,
        "created_at": utc_now(),
    }
    lock_file = shiki_path(target, "locks", f"{args.task_id}.json")
    write_json(lock_file, record)
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=args.task_id,
        ledger_type="lock",
        summary=f"Locks acquired for {args.task_id}",
        evidence=[str(lock_file.relative_to(target))],
    )
    task["status"] = "ready"
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{args.task_id}.json"), task)
    result.update({"lock_file": str(lock_file), "ledger_id": ledger_id})
    print_json(result)
    return 0


def cmd_dispatch_check(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    task = load_task(target, args.task_id)

    dependency_tasks = [load_task(target, dep) for dep in task.get("dependencies", [])]
    dependencies_complete = all(dep.get("status") == "done" for dep in dependency_tasks)
    lock = lock_record(target, args.task_id)
    task_locks = set(task.get("locks", []))
    locks_granted = not task_locks or bool(lock and lock.get("state") == "active" and task_locks.issubset(set(lock.get("locks", []))))
    worktree_allocated = worktree_record(target, args.task_id) is not None
    guardian_required = task.get("risk_level") in {"high", "critical"}
    verification_present = bool(task.get("acceptance_checks"))
    handoff_complete = all(bool(task.get(key)) for key in ("title", "scope", "expected_branch", "assigned_runtime"))

    blocking: list[str] = []
    if not dependencies_complete:
        blocking.append("dependencies are not complete")
    if not locks_granted:
        blocking.append("locks are not granted")
    if guardian_required:
        blocking.append("guardian approval required for high/critical risk")
    if not verification_present:
        blocking.append("verification profile is missing")
    if not handoff_complete:
        blocking.append("handoff is incomplete")
    if args.require_worktree and not worktree_allocated:
        blocking.append("worktree is not allocated")

    result = {
        "dispatchable": not blocking,
        "task_id": args.task_id,
        "runtime": task.get("assigned_runtime"),
        "dependencies_complete": dependencies_complete,
        "locks_granted": locks_granted,
        "guardian_approval_required": guardian_required,
        "verification_profile_present": verification_present,
        "handoff_complete": handoff_complete,
        "worktree_allocated": worktree_allocated,
        "blocking_reasons": blocking,
    }
    print_json(result)
    return 1 if blocking else 0


def cmd_worktree_allocate(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)
    task = load_task(target, args.task_id)
    branch = args.branch or task["expected_branch"]
    worktree_path = Path(args.path).expanduser().resolve() if args.path else (target.parent / ".worktrees" / slugify(branch)).resolve()
    record = {
        "task_id": args.task_id,
        "goal_id": task["goal_id"],
        "branch": branch,
        "path": str(worktree_path),
        "runtime": task["assigned_runtime"],
        "state": "registered",
        "locks": task.get("locks", []),
        "created_by": "shiki-cli",
        "created_at": utc_now(),
        "pr": task.get("expected_pr"),
    }
    if args.create and not worktree_path.exists():
        run(["git", "worktree", "add", "-b", branch, str(worktree_path)], cwd=target)
        record["state"] = "active"
    worktree_file = shiki_path(target, "worktrees", f"{args.task_id}.json")
    write_json(worktree_file, record)
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=args.task_id,
        ledger_type="handoff",
        summary=f"Worktree allocated for {args.task_id}",
        evidence=[str(worktree_file.relative_to(target))],
    )
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{args.task_id}.json"), task)
    print_json({"task_id": args.task_id, "worktree_file": str(worktree_file), "ledger_id": ledger_id, "record": record})
    return 0


def cmd_repair_packet(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)
    task = load_task(target, args.task_id)
    if args.attempt > 3:
        raise ShikiError("repair attempt limit is 3")

    repair_id = next_control_id(target, "RP")
    packet = {
        "repair_id": repair_id,
        "goal_id": task["goal_id"],
        "task_id": args.task_id,
        "pr": args.pr,
        "attempt": args.attempt,
        "failing_checklist_items": args.failing_item or [],
        "failing_acceptance_criteria": args.failing_acceptance_criteria or [],
        "minimal_required_changes": args.minimal_change,
        "prohibited_changes": args.prohibited_change or [],
        "required_skill": args.required_skill,
        "verification_commands": args.verification_command,
        "evidence_required": args.evidence_required or ["Attach verification output to the PR."],
        "stop_condition": args.stop_condition,
        "created_at": utc_now(),
    }
    repair_file = shiki_path(target, "repairs", f"{repair_id}.json")
    write_json(repair_file, packet)
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=args.task_id,
        ledger_type="repair",
        summary=f"Repair packet {repair_id} created for PR #{args.pr}",
        evidence=[str(repair_file.relative_to(target))],
    )
    task["status"] = "repair-needed"
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{args.task_id}.json"), task)
    print_json({"repair_id": repair_id, "repair_file": str(repair_file), "ledger_id": ledger_id})
    return 0


def cmd_task_status(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    task = load_task(target, args.task_id)
    task["status"] = args.status
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=args.task_id,
        ledger_type="check",
        summary=f"Task {args.task_id} status changed to {args.status}",
        evidence=[f".shiki/tasks/{args.task_id}.json"],
    )
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{args.task_id}.json"), task)
    print_json({"task_id": args.task_id, "status": args.status, "ledger_id": ledger_id})
    return 0


def cmd_goal_complete(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)
    goal = load_goal(target, args.goal_id)
    tasks = tasks_for_goal(target, args.goal_id)
    incomplete = [task["id"] for task in tasks if task.get("status") != "done"]
    blocking: list[str] = []
    if not tasks:
        blocking.append("goal has no tasks")
    if incomplete:
        blocking.append(f"incomplete tasks: {', '.join(incomplete)}")

    status = "blocked" if blocking else "complete"
    report_id = next_control_id(target, "R")
    report = {
        "id": report_id,
        "goal_id": args.goal_id,
        "status": status,
        "summary": args.summary or f"Goal {args.goal_id} {status}: {goal['title']}",
        "evidence": [f".shiki/tasks/{task['id']}.json" for task in tasks],
        "blocking_reasons": blocking,
        "mergegate": {
            "dependencies": "pass" if not blocking else "blocked",
            "locks": "pass",
            "checks": "pass" if not blocking else "blocked",
            "review": "recorded",
            "ledger": "pass",
            "risk": goal.get("risk_level", "low"),
        },
        "created_at": utc_now(),
    }
    report_file = shiki_path(target, "reports", f"{report_id}.json")
    write_json(report_file, report)
    ledger_id = append_ledger(
        target,
        goal_id=args.goal_id,
        ledger_type="completion",
        summary=report["summary"],
        evidence=[str(report_file.relative_to(target))],
    )
    goal["status"] = status
    goal.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "goals", f"{args.goal_id}.json"), goal)
    print_json({"goal_id": args.goal_id, "status": status, "report_file": str(report_file), "ledger_id": ledger_id, "blocking_reasons": blocking})
    return 1 if blocking else 0


def cmd_bootstrap_github(args: argparse.Namespace) -> int:
    require_tool("git")
    require_tool("gh")

    config = load_default_config()
    repo = args.repo or config.get("repo")
    if not repo:
        raise ShikiError("missing --repo OWNER/NAME and no default repo configured")
    require_github_repo_slug(repo)

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


def write_target_repo_config(target: Path, repo: str, branch: str) -> None:
    payload = {
        "source_of_truth": "github",
        "repo": repo,
        "default_branch": branch,
        "mirror": ".shiki",
    }
    path = target / ".shiki" / "repo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    info(f"wrote target GitHub config: {path}")


def install_template(target: Path, *, force: bool, validate: bool) -> None:
    for relative in TEMPLATE_PATHS:
        source = ROOT / relative
        if not source.exists():
            warn(f"template path missing, skipped: {relative}")
            continue
        copy_path(source, target / relative, force=force, target_install=True)

    for relative in TARGET_STATE_DIRECTORIES:
        state_dir = target / relative
        state_dir.mkdir(parents=True, exist_ok=True)
        info(f"ensured empty state directory: {state_dir}")

    if validate:
        validate_target_shiki(target)


def cmd_init(args: argparse.Namespace) -> int:
    require_tool("git")
    require_tool("gh")

    target = Path(args.target).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    if not args.repo:
        raise ShikiError("shiki init requires --repo OWNER/NAME because Shiki is GitHub-first")
    repo = args.repo
    require_github_repo_slug(repo)

    branch = args.branch
    visibility = "private" if args.private else "public"

    run(["gh", "auth", "status"])
    install_template(target, force=args.force, validate=args.validate)
    write_target_repo_config(target, repo, branch)
    ensure_git_repo(target, branch)
    ensure_github_repo(repo, visibility)
    ensure_remote(repo, target)

    active_branch = current_branch(target)
    if active_branch != branch:
        run(["git", "checkout", "-B", branch], cwd=target)

    if args.commit:
        commit_all(target, args.commit_message)

    if args.push:
        push_branch(target, branch)
        set_default_branch(repo, branch)

    secret_value = os.environ.get(args.secret_env, "")
    if args.set_secret:
        if not secret_value:
            warn(f"{args.secret_env} is not set; skipping GitHub secret")
        else:
            set_secret(repo, "CLAUDE_CODE_OAUTH_TOKEN", secret_value)

    if args.protect:
        protect_branch(repo, branch, args.required_check)

    info("GitHub-first init complete")
    return 0


def github_origin(path: Path) -> str | None:
    result = run(["git", "remote", "get-url", "origin"], cwd=path, check=False)
    if result.returncode != 0:
        return None
    origin = result.stdout.strip()
    if "github.com" not in origin:
        return None
    return origin


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


def should_skip(path: Path, *, target_install: bool = False) -> bool:
    parts = set(path.parts)
    if "__pycache__" in parts or path.name == ".DS_Store" or path.suffix == ".pyc":
        return True
    if target_install:
        relative = path.relative_to(ROOT)
        relative_text = relative.as_posix()
        state_prefixes = tuple(f"{directory}/" for directory in TARGET_STATE_DIRECTORIES)
        return relative_text.startswith(state_prefixes)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shiki")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="Install Shiki into a target repo and publish it to GitHub")
    init.add_argument("target", help="Target repository path")
    init.add_argument("--repo", required=True, help="GitHub repository as OWNER/NAME")
    init.add_argument("--branch", default="main", help="Default branch, default main")
    init.add_argument("--private", action="store_true", help="Create a private repo")
    init.add_argument("--public", action="store_true", help=argparse.SUPPRESS)
    init.add_argument("--force", action="store_true", help="Overwrite existing target files")
    init.add_argument("--validate", action=argparse.BooleanOptionalAction, default=True)
    init.add_argument("--commit", action=argparse.BooleanOptionalAction, default=True)
    init.add_argument("--commit-message", default="shiki: initialize GitHub-first control plane")
    init.add_argument("--push", action=argparse.BooleanOptionalAction, default=True)
    init.add_argument("--set-secret", action=argparse.BooleanOptionalAction, default=True)
    init.add_argument("--secret-env", default="CLAUDE_CODE_OAUTH_TOKEN")
    init.add_argument("--protect", action=argparse.BooleanOptionalAction, default=True)
    init.add_argument("--required-check", action="append", default=list(DEFAULT_REQUIRED_CHECKS))
    init.set_defaults(func=cmd_init)

    preflight = subcommands.add_parser("preflight", help="Check whether a target repo is ready for Shiki")
    preflight.add_argument("target", nargs="?", default=".", help="Target repository path")
    preflight.add_argument("--require-github", action="store_true", help="Fail unless target is connected to GitHub")
    preflight.set_defaults(func=cmd_preflight)

    github = subcommands.add_parser("bootstrap-platform", help="Initialize and publish the Shiki platform repo to GitHub")
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

    deprecated = subcommands.add_parser("bootstrap-github", help="Deprecated alias for bootstrap-platform")
    deprecated.add_argument("--repo", help="GitHub repository as OWNER/NAME")
    deprecated.add_argument("--branch", default=None, help="Default branch, default main")
    deprecated.add_argument("--private", action="store_true", help="Create a private repo")
    deprecated.add_argument("--public", action="store_true", help=argparse.SUPPRESS)
    deprecated.add_argument("--commit", action=argparse.BooleanOptionalAction, default=True)
    deprecated.add_argument("--commit-message", default="shiki: bootstrap control plane")
    deprecated.add_argument("--push", action=argparse.BooleanOptionalAction, default=True)
    deprecated.add_argument("--set-secret", action=argparse.BooleanOptionalAction, default=True)
    deprecated.add_argument("--secret-env", default="CLAUDE_CODE_OAUTH_TOKEN")
    deprecated.add_argument("--protect", action=argparse.BooleanOptionalAction, default=True)
    deprecated.add_argument("--required-check", action="append", default=list(DEFAULT_REQUIRED_CHECKS))
    deprecated.set_defaults(func=cmd_bootstrap_github)

    target = subcommands.add_parser("install-target", help="Install Shiki template files only; GitHub-first setup uses init")
    target.add_argument("target", help="Target repository path")
    target.add_argument("--local-only", action="store_true", help="Allow template-only install without GitHub bootstrap")
    target.add_argument("--force", action="store_true", help="Overwrite existing files")
    target.add_argument("--validate", action=argparse.BooleanOptionalAction, default=True)
    target.set_defaults(func=cmd_install_target)

    install = subcommands.add_parser("install-command", help="Install a shiki command symlink")
    install.add_argument("--path", default=DEFAULT_GLOBAL_COMMAND_PATH)
    install.set_defaults(func=cmd_install_command)

    global_install = subcommands.add_parser("install-global", help="Install global Shiki CLI, Claude slash command, and Codex skill")
    global_install.add_argument("--path", default=DEFAULT_GLOBAL_COMMAND_PATH)
    global_install.add_argument("--claude-command", action=argparse.BooleanOptionalAction, default=True)
    global_install.add_argument("--claude-command-path", default=DEFAULT_CLAUDE_COMMAND_PATH)
    global_install.add_argument("--codex-skill", action=argparse.BooleanOptionalAction, default=True)
    global_install.add_argument("--codex-skill-path", default=DEFAULT_CODEX_SKILL_PATH)
    global_install.set_defaults(func=cmd_install_global)

    status = subcommands.add_parser("status", help="Show local Shiki CLI configuration")
    status.set_defaults(func=cmd_status)

    goal = subcommands.add_parser("goal", help="Manage Shiki goals")
    goal_subcommands = goal.add_subparsers(dest="goal_command", required=True)
    goal_create = goal_subcommands.add_parser("create", help="Register a GitHub-first Shiki goal")
    goal_create.add_argument("--target", default=".", help="Target repository path")
    goal_create.add_argument("--title", required=True)
    goal_create.add_argument("--outcome", required=True)
    goal_create.add_argument("--completion-condition", action="append", default=[])
    goal_create.add_argument("--non-goal", action="append", default=[])
    goal_create.add_argument("--risk-level", default="low", choices=["low", "medium", "high", "critical"])
    goal_create.add_argument("--required-skill", action="append", default=[])
    goal_create.add_argument("--acceptance-evidence", action="append", default=[])
    goal_create.add_argument("--github-issue", type=int)
    goal_create.set_defaults(func=cmd_goal_create)

    goal_complete = goal_subcommands.add_parser("complete", help="Judge goal completion from task evidence")
    goal_complete.add_argument("--target", default=".", help="Target repository path")
    goal_complete.add_argument("goal_id")
    goal_complete.add_argument("--summary")
    goal_complete.set_defaults(func=cmd_goal_complete)

    issue = subcommands.add_parser("issue", help="Plan vertical-slice Shiki tasks")
    issue_subcommands = issue.add_subparsers(dest="issue_command", required=True)
    issue_plan = issue_subcommands.add_parser("plan", help="Register a task and update the task DAG")
    issue_plan.add_argument("--target", default=".", help="Target repository path")
    issue_plan.add_argument("--goal-id", required=True)
    issue_plan.add_argument("--title", required=True)
    issue_plan.add_argument("--scope", required=True)
    issue_plan.add_argument("--non-goal", action="append", default=[])
    issue_plan.add_argument("--dependency", action="append", default=[])
    issue_plan.add_argument("--lock", action="append", default=[])
    issue_plan.add_argument("--runtime", default="codex", choices=["codex", "claude-code", "github-actions", "hermes-runner", "human", "other"])
    issue_plan.add_argument("--risk-level", default="low", choices=["low", "medium", "high", "critical"])
    issue_plan.add_argument("--required-skill", action="append", default=[])
    issue_plan.add_argument("--acceptance-check", action="append", required=True)
    issue_plan.add_argument("--expected-branch")
    issue_plan.add_argument("--expected-pr", type=int)
    issue_plan.add_argument("--github-issue", type=int)
    issue_plan.set_defaults(func=cmd_issue_plan)

    lock = subcommands.add_parser("lock", help="Manage Shiki task locks")
    lock_subcommands = lock.add_subparsers(dest="lock_command", required=True)
    lock_acquire = lock_subcommands.add_parser("acquire", help="Acquire declared locks for a task")
    lock_acquire.add_argument("--target", default=".", help="Target repository path")
    lock_acquire.add_argument("--owner", default="shiki-cli")
    lock_acquire.add_argument("task_id")
    lock_acquire.set_defaults(func=cmd_lock_acquire)

    dispatch = subcommands.add_parser("dispatch", help="Run dispatch readiness checks")
    dispatch_subcommands = dispatch.add_subparsers(dest="dispatch_command", required=True)
    dispatch_check = dispatch_subcommands.add_parser("check", help="Check whether a task may be dispatched")
    dispatch_check.add_argument("--target", default=".", help="Target repository path")
    dispatch_check.add_argument("--require-worktree", action="store_true")
    dispatch_check.add_argument("task_id")
    dispatch_check.set_defaults(func=cmd_dispatch_check)

    worktree = subcommands.add_parser("worktree", help="Manage Shiki worktree records")
    worktree_subcommands = worktree.add_subparsers(dest="worktree_command", required=True)
    worktree_allocate = worktree_subcommands.add_parser("allocate", help="Allocate a task worktree record")
    worktree_allocate.add_argument("--target", default=".", help="Target repository path")
    worktree_allocate.add_argument("--branch")
    worktree_allocate.add_argument("--path")
    worktree_allocate.add_argument("--create", action="store_true", help="Also run git worktree add")
    worktree_allocate.add_argument("task_id")
    worktree_allocate.set_defaults(func=cmd_worktree_allocate)

    repair = subcommands.add_parser("repair", help="Manage bounded repair packets")
    repair_subcommands = repair.add_subparsers(dest="repair_command", required=True)
    repair_packet = repair_subcommands.add_parser("packet", help="Create a bounded repair packet")
    repair_packet.add_argument("--target", default=".", help="Target repository path")
    repair_packet.add_argument("--task-id", required=True)
    repair_packet.add_argument("--pr", required=True, type=int)
    repair_packet.add_argument("--attempt", default=1, type=int)
    repair_packet.add_argument("--failing-item", action="append", default=[])
    repair_packet.add_argument("--failing-acceptance-criteria", action="append", default=[])
    repair_packet.add_argument("--minimal-change", action="append", required=True)
    repair_packet.add_argument("--prohibited-change", action="append", default=[])
    repair_packet.add_argument("--required-skill", default="tdd", choices=["tdd", "diagnose", "grill-with-docs", "improve-codebase-architecture", "none"])
    repair_packet.add_argument("--verification-command", action="append", required=True)
    repair_packet.add_argument("--evidence-required", action="append", default=[])
    repair_packet.add_argument("--stop-condition", default="Stop after this packet is satisfied or after three failed attempts.")
    repair_packet.set_defaults(func=cmd_repair_packet)

    task = subcommands.add_parser("task", help="Manage Shiki task state")
    task_subcommands = task.add_subparsers(dest="task_command", required=True)
    task_status = task_subcommands.add_parser("status", help="Set a task status and record ledger evidence")
    task_status.add_argument("--target", default=".", help="Target repository path")
    task_status.add_argument("task_id")
    task_status.add_argument("--status", required=True, choices=["planned", "ready", "running", "blocked", "review", "repair-needed", "done"])
    task_status.set_defaults(func=cmd_task_status)

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
