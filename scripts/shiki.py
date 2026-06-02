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
import time
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from shiki_contracts import DEFAULT_REQUIRED_CHECKS, TARGET_STATE_DIRECTORIES


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
    ".github/workflows/shiki-orchestrator.yml",
    "docs/agents",
    "docs/adr",
    "skills/engineering",
    "scripts/shiki_schema.py",
    "scripts/validate_shiki.py",
    "scripts/shiki_contracts.py",
    "scripts/enforce_cca_verdict.py",
    "scripts/mergegate_check.py",
    "scripts/shiki.py",
    "scripts/test_shiki_init.sh",
    "scripts/test_shiki_control_plane.sh",
    "scripts/test_shiki_run_orchestrator.sh",
    "scripts/test_shiki_daemon_runner.sh",
    "scripts/test_shiki_runner_codex.sh",
    "scripts/test_shiki_start.sh",
    "scripts/test_shiki_runtime_auth.sh",
]

DEFAULT_GLOBAL_COMMAND_PATH = "~/.local/bin/shiki"
DEFAULT_CLAUDE_COMMAND_PATH = "~/.claude/commands/shiki.md"
DEFAULT_CODEX_SKILL_PATH = "~/.codex/skills/shiki/SKILL.md"
DEFAULT_ENGINEERING_SKILLS_DIRS = [
    "~/Documents/lead-os/skills/engineering",
    "~/skills/skills/engineering",
]
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


def ensure_remote(repo: str, path: Path, *, adopt_existing_repo: bool = False) -> None:
    remote_url = f"https://github.com/{repo}.git"
    existing = run(["git", "remote", "get-url", "origin"], cwd=path, check=False)
    if existing.returncode == 0:
        current = existing.stdout.strip()
        if current != remote_url:
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


def manifest_stage_paths(path: Path) -> list[str]:
    candidates = list(TEMPLATE_PATHS)
    candidates.append(".shiki/repo.json")
    return [relative for relative in candidates if (path / relative).exists()]


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
        raise ShikiError(
            f"could not configure branch protection: {result.stderr.strip()}. "
            "Branch protection is required; rerun with --no-protect only for an explicit non-protected setup."
        )


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


def prompt_value(label: str, current: str | None = None, *, required: bool = True) -> str:
    if current:
        return current
    if not sys.stdin.isatty():
        if required:
            raise ShikiError(f"missing {label}; pass it as an option or use --answers-file")
        return ""
    while True:
        value = input(f"{label}: ").strip()
        if value or not required:
            return value


def prompt_default(label: str, default: str) -> str:
    if not sys.stdin.isatty():
        return default
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def prompt_list(label: str, current: list[str] | None = None) -> list[str]:
    if current:
        return current
    if not sys.stdin.isatty():
        return []
    print(f"{label}: enter one item per line, then an empty line.")
    values: list[str] = []
    while True:
        value = input("> ").strip()
        if not value:
            break
        values.append(value)
    return values


def default_engineering_skills_dir() -> str:
    configured = os.environ.get("SHIKI_ENGINEERING_SKILLS_DIR")
    if configured:
        return configured
    for candidate in DEFAULT_ENGINEERING_SKILLS_DIRS:
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return DEFAULT_ENGINEERING_SKILLS_DIRS[0]


def resolve_engineering_skills_dir(value: str | None) -> str:
    skills_dir = value or default_engineering_skills_dir()
    if value and not Path(value).expanduser().exists():
        raise ShikiError(f"engineering skills directory does not exist: {value}")
    return skills_dir


def start_target_value(args: argparse.Namespace) -> str:
    positional = getattr(args, "target_positional", None)
    option = getattr(args, "target", ".")
    if positional and option != "." and Path(positional).expanduser().resolve() != Path(option).expanduser().resolve():
        raise ShikiError("pass the target repository either positionally or with --target, not both")
    return positional or option


def scan_ids(target: Path, prefix: str) -> list[int]:
    pattern = re.compile(rf"\b{re.escape(prefix)}-([0-9]{{4,}})\b")
    numbers: list[int] = []
    directories = {
        "G": ["goals", "dag"],
        "T": ["tasks"],
        "L": ["ledger"],
        "P": ["plans"],
        "RUN": ["runs"],
        "EXEC": ["runner"],
        "SMOKE": ["smoke"],
        "START": ["starts"],
        "INBOX": ["inbox"],
        "RP": ["repairs"],
        "R": ["reports"],
    }.get(prefix, [])
    base = target / ".shiki"
    if not base.exists():
        return numbers
    for directory in directories:
        for path in (base / directory).glob("*.json"):
            for match in pattern.finditer(path.name):
                numbers.append(int(match.group(1)))
    if numbers or directories:
        return numbers
    for path in base.rglob("*.json"):
        for match in pattern.finditer(path.name):
            numbers.append(int(match.group(1)))
    return numbers


def next_control_id(target: Path, prefix: str) -> str:
    number = max(scan_ids(target, prefix), default=0) + 1
    return f"{prefix}-{number:04d}"


def load_task(target: Path, task_id: str) -> dict[str, Any]:
    return read_json(shiki_path(target, "tasks", f"{task_id}.json"))


def load_goal(target: Path, goal_id: str) -> dict[str, Any]:
    return read_json(shiki_path(target, "goals", f"{goal_id}.json"))


def load_plan(target: Path, plan_id: str) -> dict[str, Any]:
    return read_json(shiki_path(target, "plans", f"{plan_id}.json"))


def load_repair(target: Path, repair_id: str) -> dict[str, Any]:
    return read_json(shiki_path(target, "repairs", f"{repair_id}.json"))


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


def require_grilled_plan(plan: dict[str, Any]) -> None:
    grill = plan.get("grill_with_docs")
    if not isinstance(grill, dict) or grill.get("status") != "complete":
        raise ShikiError("plan must include grill_with_docs.status=complete before Shiki can run it")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ShikiError("plan must include at least one vertical-slice task")
    required_goal_fields = ["title", "outcome"]
    for field in required_goal_fields:
        if not isinstance(plan.get(field), str) or not plan[field].strip():
            raise ShikiError(f"plan is missing required field: {field}")
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise ShikiError(f"plan task {index} must be an object")
        for field in ("title", "scope", "acceptance_checks"):
            if field not in task:
                raise ShikiError(f"plan task {index} is missing required field: {field}")
        if not isinstance(task["acceptance_checks"], list) or not task["acceptance_checks"]:
            raise ShikiError(f"plan task {index} must include acceptance_checks")


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


def register_goal_from_plan(target: Path, plan: dict[str, Any], *, github_issue: int | None = None) -> tuple[str, str]:
    goal_id = next_control_id(target, "G")
    goal_file = shiki_path(target, "goals", f"{goal_id}.json")
    payload = {
        "id": goal_id,
        "github_issue": github_issue,
        "title": plan["title"],
        "outcome": plan["outcome"],
        "completion_conditions": plan.get("completion_conditions") or [plan["outcome"]],
        "non_goals": plan.get("non_goals") or [],
        "risk_level": plan.get("risk_level", "low"),
        "required_skills": plan.get("required_skills") or ["grill-with-docs"],
        "acceptance_evidence": plan.get("acceptance_evidence") or [
            "GitHub Issue records the goal.",
            "Task DAG is registered in .shiki/dag.",
            "CCA verdict and MergeGate evidence are recorded before completion.",
        ],
        "grill_with_docs": plan.get("grill_with_docs"),
        "source_plan": plan.get("id"),
        "status": "planned",
        "created_at": utc_now(),
    }
    write_json(goal_file, payload)
    ledger_id = append_ledger(
        target,
        goal_id=goal_id,
        ledger_type="goal-created",
        summary=f"Goal registered from plan: {plan['title']}",
        evidence=[str(goal_file.relative_to(target)), f".shiki/plans/{plan.get('id')}.json"],
    )
    payload["ledger_evidence"] = [ledger_id]
    write_json(goal_file, payload)
    return goal_id, ledger_id


def register_task_from_plan(
    target: Path,
    *,
    goal_id: str,
    task_plan: dict[str, Any],
    dependencies: list[str],
) -> tuple[str, str]:
    task_id = next_control_id(target, "T")
    branch = task_plan.get("expected_branch") or f"shiki/{task_id.lower()}-{slugify(task_plan['title'])}"
    ledger_id = append_ledger(
        target,
        goal_id=goal_id,
        task_id=task_id,
        ledger_type="task-registered",
        summary=f"Task registered from plan: {task_plan['title']}",
        evidence=[f".shiki/tasks/{task_id}.json"],
    )
    payload = {
        "id": task_id,
        "goal_id": goal_id,
        "github_issue": task_plan.get("github_issue"),
        "title": task_plan["title"],
        "scope": task_plan["scope"],
        "non_goals": task_plan.get("non_goals") or [],
        "dependencies": dependencies,
        "locks": task_plan.get("locks") or [],
        "assigned_runtime": task_plan.get("runtime", "codex"),
        "risk_level": task_plan.get("risk_level", "low"),
        "required_skills": task_plan.get("required_skills") or ["tdd"],
        "acceptance_checks": task_plan["acceptance_checks"],
        "expected_branch": branch,
        "expected_pr": task_plan.get("expected_pr"),
        "ledger_evidence": [ledger_id],
        "status": "planned",
    }
    task_file = shiki_path(target, "tasks", f"{task_id}.json")
    write_json(task_file, payload)
    return task_id, ledger_id


def update_goal_dag(target: Path, goal_id: str, task_ids: list[str], dependency_edges: list[dict[str, str]]) -> Path:
    dag_file = shiki_path(target, "dag", f"{goal_id}.json")
    existing = {"goal_id": goal_id, "nodes": [], "edges": []}
    if dag_file.exists():
        existing = read_json(dag_file)
    nodes = list(dict.fromkeys([*existing.get("nodes", []), *task_ids]))
    edge_keys = {(edge.get("from"), edge.get("to")) for edge in existing.get("edges", [])}
    edges = list(existing.get("edges", []))
    for edge in dependency_edges:
        key = (edge["from"], edge["to"])
        if key not in edge_keys:
            edges.append(edge)
            edge_keys.add(key)
    write_json(dag_file, {"goal_id": goal_id, "nodes": nodes, "edges": edges})
    return dag_file


def try_acquire_locks(target: Path, task_id: str) -> tuple[bool, list[str], str | None]:
    task = load_task(target, task_id)
    locks = list(task.get("locks", []))
    conflicts = has_active_lock_conflict(target, task_id, locks)
    if conflicts:
        return False, conflicts, None
    lock_file = shiki_path(target, "locks", f"{task_id}.json")
    write_json(
        lock_file,
        {
            "task_id": task_id,
            "goal_id": task["goal_id"],
            "locks": locks,
            "state": "active",
            "owner": "shiki-run",
            "created_at": utc_now(),
        },
    )
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task_id,
        ledger_type="lock",
        summary=f"Locks acquired for {task_id}",
        evidence=[str(lock_file.relative_to(target))],
    )
    task["status"] = "ready"
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{task_id}.json"), task)
    return True, [], ledger_id


def allocate_worktree_record(target: Path, task_id: str) -> tuple[Path, str]:
    task = load_task(target, task_id)
    branch = task["expected_branch"]
    worktree_path = (target.parent / ".worktrees" / slugify(branch)).resolve()
    record = {
        "task_id": task_id,
        "goal_id": task["goal_id"],
        "branch": branch,
        "path": str(worktree_path),
        "runtime": task["assigned_runtime"],
        "state": "registered",
        "locks": task.get("locks", []),
        "created_by": "shiki-run",
        "created_at": utc_now(),
        "pr": task.get("expected_pr"),
    }
    worktree_file = shiki_path(target, "worktrees", f"{task_id}.json")
    write_json(worktree_file, record)
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task_id,
        ledger_type="handoff",
        summary=f"Worktree registered for {task_id}",
        evidence=[str(worktree_file.relative_to(target))],
    )
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{task_id}.json"), task)
    return worktree_file, ledger_id


def orchestrate_plan(target: Path, plan: dict[str, Any]) -> dict[str, Any]:
    require_grilled_plan(plan)
    if "id" not in plan:
        plan["id"] = next_control_id(target, "P")
        plan["status"] = "ingested"
        plan["ingested_at"] = utc_now()
        write_json(shiki_path(target, "plans", f"{plan['id']}.json"), plan)

    goal_id, goal_ledger = register_goal_from_plan(target, plan)
    task_ids: list[str] = []
    task_ids_by_title: dict[str, str] = {}
    dependency_edges: list[dict[str, str]] = []

    for task_plan in plan["tasks"]:
        dependency_refs = task_plan.get("dependencies") or []
        dependencies: list[str] = []
        for dependency in dependency_refs:
            if dependency in task_ids_by_title:
                dependencies.append(task_ids_by_title[dependency])
            elif isinstance(dependency, str) and re.match(r"^T-[0-9]{4,}$", dependency):
                dependencies.append(dependency)
            else:
                raise ShikiError(f"task {task_plan['title']} references unknown dependency: {dependency}")

        task_id, _ = register_task_from_plan(
            target,
            goal_id=goal_id,
            task_plan=task_plan,
            dependencies=dependencies,
        )
        task_ids.append(task_id)
        task_ids_by_title[task_plan["title"]] = task_id
        for dependency in dependencies:
            dependency_edges.append({"from": dependency, "to": task_id, "reason": "declared plan dependency"})

    dag_file = update_goal_dag(target, goal_id, task_ids, dependency_edges)

    dispatchable: list[str] = []
    blocked: dict[str, list[str]] = {}
    worktrees: list[str] = []
    for task_id in task_ids:
        task = load_task(target, task_id)
        if task.get("dependencies"):
            blocked[task_id] = ["dependencies are not complete"]
            continue
        lock_ok, lock_blockers, _ = try_acquire_locks(target, task_id)
        if not lock_ok:
            blocked[task_id] = lock_blockers
            continue
        worktree_file, _ = allocate_worktree_record(target, task_id)
        dispatchable.append(task_id)
        worktrees.append(str(worktree_file.relative_to(target)))

    run_id = next_control_id(target, "RUN")
    run_file = shiki_path(target, "runs", f"{run_id}.json")
    run_payload = {
        "id": run_id,
        "plan_id": plan["id"],
        "goal_id": goal_id,
        "task_ids": task_ids,
        "dispatchable_task_ids": dispatchable,
        "blocked_task_ids": blocked,
        "dag": str(dag_file.relative_to(target)),
        "worktrees": worktrees,
        "created_at": utc_now(),
    }
    write_json(run_file, run_payload)
    ledger_id = append_ledger(
        target,
        goal_id=goal_id,
        ledger_type="handoff",
        summary=f"Shiki run {run_id} created {len(task_ids)} task(s) from plan {plan['id']}",
        evidence=[str(run_file.relative_to(target)), str(dag_file.relative_to(target))],
    )
    return {
        "run_id": run_id,
        "plan_id": plan["id"],
        "goal_id": goal_id,
        "goal_ledger_id": goal_ledger,
        "task_ids": task_ids,
        "dispatchable_task_ids": dispatchable,
        "blocked_task_ids": blocked,
        "run_file": str(run_file),
        "ledger_id": ledger_id,
    }


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


def cmd_plan_ingest(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)

    source = Path(args.plan_file).expanduser().resolve()
    plan = read_json(source)
    require_grilled_plan(plan)

    plan_id = next_control_id(target, "P")
    plan["id"] = plan_id
    plan["status"] = "ingested"
    plan["source_file"] = str(source)
    plan["ingested_at"] = utc_now()
    plan_file = shiki_path(target, "plans", f"{plan_id}.json")
    write_json(plan_file, plan)
    print_json({"plan_id": plan_id, "plan_file": str(plan_file), "status": "ingested"})
    return 0


def cmd_plan_guide(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    prompt = args.prompt or "unspecified goal"
    result = {
        "target": str(target),
        "prompt": prompt,
        "entry_skill": "grill-with-docs",
        "required_next_steps": [
            "Run grill-with-docs until terminology, boundaries, risks, and ADR-worthy decisions are settled.",
            "Write a machine-readable plan JSON with grill_with_docs.status=complete.",
            "Run shiki plan ingest --plan-file PLAN.json.",
            "Run shiki run --plan P-0001 to create the Goal, Task DAG, locks, and first dispatchable worktree.",
        ],
        "plan_contract": {
            "required_goal_fields": ["title", "outcome", "grill_with_docs", "tasks"],
            "required_task_fields": ["title", "scope", "acceptance_checks"],
        },
    }
    print_json(result)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)

    if Path(args.plan).expanduser().exists():
        plan = read_json(Path(args.plan).expanduser().resolve())
        require_grilled_plan(plan)
        if "id" not in plan:
            plan_id = next_control_id(target, "P")
            plan["id"] = plan_id
            plan["status"] = "ingested"
            plan["source_file"] = str(Path(args.plan).expanduser().resolve())
            plan["ingested_at"] = utc_now()
            write_json(shiki_path(target, "plans", f"{plan_id}.json"), plan)
    else:
        plan = load_plan(target, args.plan)

    print_json(orchestrate_plan(target, plan))
    return 0


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
                "required_skills": ["tdd"],
            }
        ]

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
    )
    cmd_init(init_args)


def create_issues_for_dispatchable_tasks(target: Path, task_ids: list[str]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for task_id in task_ids:
        issues.append(create_github_issue_for_task(target, task_id))
    return issues


def cmd_start(args: argparse.Namespace) -> int:
    target = target_path(start_target_value(args))
    target.mkdir(parents=True, exist_ok=True)
    answers = load_start_answers(args)

    already_initialized = (target / ".shiki" / "repo.json").exists() and is_git_repo(target) and github_origin(target)
    if not already_initialized:
        initialize_target_from_start(args, target, answers["repo"])
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
    claude_secret = github_secret_status(answers["repo"], "CLAUDE_CODE_OAUTH_TOKEN")
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


def write_handoff(target: Path, name: str, body: str) -> Path:
    path = shiki_path(target, "handoffs", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def cmd_handoff_task(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    task = load_task(target, args.task_id)
    body = "\n".join(
        [
            f"# Codex Task Handoff: {task['id']}",
            "",
            f"Goal: {task['goal_id']}",
            f"Task: {task['id']}",
            f"Runtime: {task.get('assigned_runtime')}",
            f"Branch: {task.get('expected_branch')}",
            "",
            "## Scope",
            task["scope"],
            "",
            "## Acceptance Checks",
            *[f"- {check}" for check in task.get("acceptance_checks", [])],
            "",
            "## Locks",
            *[f"- {lock}" for lock in task.get("locks", [])],
            "",
            "## Required Skills",
            *[f"- {skill}" for skill in task.get("required_skills", [])],
        ]
    )
    handoff_file = write_handoff(target, f"{task['id']}-task.md", body + "\n")
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task["id"],
        ledger_type="handoff",
        summary=f"Task handoff written for {task['id']}",
        evidence=[str(handoff_file.relative_to(target))],
    )
    print_json({"task_id": task["id"], "handoff_file": str(handoff_file), "ledger_id": ledger_id})
    return 0


def cmd_handoff_repair(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    repair = load_repair(target, args.repair_id)
    task = load_task(target, repair["task_id"])
    body = "\n".join(
        [
            f"# Codex Repair Handoff: {repair['repair_id']}",
            "",
            f"Goal: {repair['goal_id']}",
            f"Task: {repair['task_id']}",
            f"PR: {repair['pr']}",
            f"Attempt: {repair['attempt']}",
            f"Required skill: {repair['required_skill']}",
            "",
            "## Minimal Required Changes",
            *[f"- {item}" for item in repair.get("minimal_required_changes", [])],
            "",
            "## Prohibited Changes",
            *[f"- {item}" for item in repair.get("prohibited_changes", [])],
            "",
            "## Verification Commands",
            *[f"- `{command}`" for command in repair.get("verification_commands", [])],
            "",
            "## Task Scope",
            task["scope"],
        ]
    )
    handoff_file = write_handoff(target, f"{repair['repair_id']}-repair.md", body + "\n")
    ledger_id = append_ledger(
        target,
        goal_id=repair["goal_id"],
        task_id=repair["task_id"],
        ledger_type="handoff",
        summary=f"Repair handoff written for {repair['repair_id']}",
        evidence=[str(handoff_file.relative_to(target))],
    )
    print_json({"repair_id": repair["repair_id"], "handoff_file": str(handoff_file), "ledger_id": ledger_id})
    return 0


def cmd_daemon_enqueue_plan(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)
    source = Path(args.plan_file).expanduser().resolve()
    plan = read_json(source)
    require_grilled_plan(plan)
    inbox_id = next_control_id(target, "INBOX")
    inbox_file = shiki_path(target, "inbox", f"{inbox_id}.json")
    write_json(
        inbox_file,
        {
            "id": inbox_id,
            "type": "plan",
            "state": "pending",
            "source_file": str(source),
            "plan": plan,
            "created_at": utc_now(),
        },
    )
    print_json({"inbox_id": inbox_id, "inbox_file": str(inbox_file), "state": "pending"})
    return 0


def process_inbox_item(target: Path, path: Path) -> dict[str, Any]:
    item = read_json(path)
    if item.get("state") != "pending":
        return {"inbox_id": item.get("id"), "state": "skipped"}
    if item.get("type") != "plan":
        raise ShikiError(f"unsupported inbox item type: {item.get('type')}")
    result = orchestrate_plan(target, item["plan"])
    archive = shiki_path(target, "inbox", "processed", path.name)
    archive.parent.mkdir(parents=True, exist_ok=True)
    item.update({"state": "processed", "processed_at": utc_now(), "result": result})
    write_json(archive, item)
    path.unlink()
    return result


def cmd_daemon_run(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)
    processed: list[dict[str, Any]] = []

    while True:
        pending = sorted(
            path
            for path in shiki_path(target, "inbox").glob("*.json")
            if path.is_file()
        )
        for path in pending:
            processed.append(process_inbox_item(target, path))
            if args.once:
                result = processed[-1]
                result["processed_count"] = len(processed)
                print_json(result)
                return 0
        if args.once:
            print_json({"processed_count": 0, "state": "idle"})
            return 0
        time.sleep(args.interval)


def dispatchable_task_ids(target: Path) -> list[str]:
    ids: list[str] = []
    for path in task_files(target):
        task = read_json(path)
        if task.get("status") != "ready":
            continue
        if worktree_record(target, task["id"]) is None:
            continue
        if task.get("dependencies"):
            dependencies = [load_task(target, dep) for dep in task.get("dependencies", [])]
            if any(dep.get("status") != "done" for dep in dependencies):
                continue
        ids.append(task["id"])
    return ids


def cmd_runner_next(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ids = dispatchable_task_ids(target)
    if not ids:
        print_json({"dispatchable": False, "task_id": None, "blocking_reasons": ["no ready task with worktree record"]})
        return 1
    task = load_task(target, ids[0])
    print_json({"dispatchable": True, "task_id": task["id"], "goal_id": task["goal_id"], "branch": task["expected_branch"]})
    return 0


def cmd_runner_execute(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)
    task = load_task(target, args.task_id)
    if task.get("status") not in {"ready", "running"}:
        raise ShikiError(f"task {args.task_id} is not ready for runner execution")
    task["status"] = "running"
    write_json(shiki_path(target, "tasks", f"{args.task_id}.json"), task)

    process = subprocess.run(args.command, cwd=str(target), shell=True, text=True, capture_output=True, check=False)
    record_id = next_control_id(target, "EXEC")
    record_file = shiki_path(target, "runner", f"{record_id}.json")
    record = {
        "id": record_id,
        "task_id": args.task_id,
        "goal_id": task["goal_id"],
        "command": args.command,
        "returncode": process.returncode,
        "stdout": process.stdout,
        "stderr": process.stderr,
        "created_at": utc_now(),
    }
    write_json(record_file, record)
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=args.task_id,
        ledger_type="check",
        summary=f"Runner command exited {process.returncode} for {args.task_id}",
        evidence=[str(record_file.relative_to(target))],
    )
    task = load_task(target, args.task_id)
    task.setdefault("ledger_evidence", []).append(ledger_id)
    task["status"] = "ready" if process.returncode == 0 else "repair-needed"
    write_json(shiki_path(target, "tasks", f"{args.task_id}.json"), task)
    print_json({"task_id": args.task_id, "returncode": process.returncode, "runner_record": str(record_file), "ledger_id": ledger_id})
    return process.returncode


def branch_exists(target: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=str(target),
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0


def ensure_physical_worktree(target: Path, task: dict[str, Any]) -> dict[str, Any]:
    record = worktree_record(target, task["id"])
    branch = str((record or {}).get("branch") or task["expected_branch"])
    path = Path((record or {}).get("path") or (target.parent / ".worktrees" / slugify(branch))).expanduser().resolve()

    if record is None:
        record = {
            "task_id": task["id"],
            "goal_id": task["goal_id"],
            "branch": branch,
            "path": str(path),
            "runtime": task["assigned_runtime"],
            "state": "registered",
            "locks": task.get("locks", []),
            "created_by": "shiki-cli",
            "created_at": utc_now(),
            "pr": task.get("expected_pr"),
        }
        write_json(shiki_path(target, "worktrees", f"{task['id']}.json"), record)
        ledger_id = append_ledger(
            target,
            goal_id=task["goal_id"],
            task_id=task["id"],
            ledger_type="handoff",
            summary=f"Worktree registered for {task['id']}",
            evidence=[f".shiki/worktrees/{task['id']}.json"],
        )
        task.setdefault("ledger_evidence", []).append(ledger_id)
        write_json(shiki_path(target, "tasks", f"{task['id']}.json"), task)

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if branch_exists(target, branch):
            run(["git", "worktree", "add", str(path), branch], cwd=target)
        else:
            run(["git", "worktree", "add", "-b", branch, str(path)], cwd=target)
        record["state"] = "active"
        record["path"] = str(path)
        write_json(shiki_path(target, "worktrees", f"{task['id']}.json"), record)

    return record


def record_runner_result(target: Path, task: dict[str, Any], command: str, returncode: int, stdout: str, stderr: str) -> tuple[Path, str]:
    record_id = next_control_id(target, "EXEC")
    record_file = shiki_path(target, "runner", f"{record_id}.json")
    record = {
        "id": record_id,
        "task_id": task["id"],
        "goal_id": task["goal_id"],
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "created_at": utc_now(),
    }
    write_json(record_file, record)
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task["id"],
        ledger_type="check",
        summary=f"Runner command exited {returncode} for {task['id']}",
        evidence=[str(record_file.relative_to(target))],
    )
    return record_file, ledger_id


def cmd_runner_codex(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)
    require_tool("codex")

    task = load_task(target, args.task_id)
    runtime = str(task.get("assigned_runtime", "codex"))
    if runtime != "codex" and not args.force:
        raise ShikiError(f"task {args.task_id} is assigned to {runtime}, not codex")
    if task.get("status") not in {"ready", "running"}:
        raise ShikiError(f"task {args.task_id} is not ready for Codex execution")

    codex = codex_auth_status()
    if not codex["ready"]:
        raise ShikiError("Codex CLI is not ready. Run `codex login` or sign in to Codex App before dispatch.")

    handoff_file = shiki_path(target, "handoffs", f"{args.task_id}-task.md")
    if not handoff_file.exists():
        raise ShikiError(f"missing handoff file: {handoff_file}. Run `shiki handoff task {args.task_id}` first.")

    worktree = ensure_physical_worktree(target, task)
    worktree_path = Path(worktree["path"]).expanduser().resolve()
    command_label = f"codex exec <{handoff_file.relative_to(target)}>"

    if args.dry_run:
        print_json(
            {
                "task_id": args.task_id,
                "would_execute": command_label,
                "cwd": str(worktree_path),
                "handoff_file": str(handoff_file),
            }
        )
        return 0

    task["status"] = "running"
    write_json(shiki_path(target, "tasks", f"{args.task_id}.json"), task)
    prompt = handoff_file.read_text()
    process = subprocess.run(
        ["codex", "exec", "-"],
        cwd=str(worktree_path),
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    record_file, ledger_id = record_runner_result(
        target,
        task,
        command_label,
        process.returncode,
        process.stdout,
        process.stderr,
    )
    task = load_task(target, args.task_id)
    task.setdefault("ledger_evidence", []).append(ledger_id)
    task["status"] = "review" if process.returncode == 0 else "repair-needed"
    write_json(shiki_path(target, "tasks", f"{args.task_id}.json"), task)
    print_json(
        {
            "task_id": args.task_id,
            "returncode": process.returncode,
            "runner_record": str(record_file),
            "ledger_id": ledger_id,
            "worktree": str(worktree_path),
            "status": task["status"],
        }
    )
    return process.returncode


def cmd_smoke_live(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    require_tool("gh")
    if args.dry_run and args.execute_github:
        raise ShikiError("--dry-run and --execute-github cannot be used together")
    repo = github_repo_from_origin(target)
    if not repo:
        raise ShikiError("could not infer GitHub repo from origin")
    run(["gh", "auth", "status"], cwd=target)
    run(["gh", "repo", "view", repo, "--json", "name"], cwd=target)

    plan = read_json(Path(args.plan_file).expanduser().resolve())
    require_grilled_plan(plan)
    if args.dry_run:
        smoke_id = next_control_id(target, "SMOKE")
        smoke_file = shiki_path(target, "smoke", f"{smoke_id}.json")
        payload = {
            "id": smoke_id,
            "repo": repo,
            "dry_run": True,
            "execute_github": False,
            "plan_title": plan["title"],
            "task_count": len(plan["tasks"]),
            "created_at": utc_now(),
        }
        write_json(smoke_file, payload)
        print_json({"smoke_id": smoke_id, "smoke_file": str(smoke_file), "dry_run": True, "task_count": len(plan["tasks"])})
        return 0

    result = orchestrate_plan(target, plan)
    first_task = result["dispatchable_task_ids"][0] if result["dispatchable_task_ids"] else None
    github_result: dict[str, Any] = {"executed": False}
    if args.execute_github and first_task:
        issue_result = create_github_issue_for_task(target, first_task)
        if args.push_branch:
            task = load_task(target, first_task)
            run(["git", "checkout", "-B", task["expected_branch"]], cwd=target)
            run(["git", "add", ".shiki"], cwd=target)
            staged = run(["git", "diff", "--cached", "--quiet"], cwd=target, check=False)
            if staged.returncode != 0:
                run(["git", "commit", "-m", f"shiki: smoke evidence for {first_task}"], cwd=target)
            run(["git", "push", "-u", "origin", task["expected_branch"]], cwd=target)
        pr_result = create_github_pr_for_task(target, first_task, base=args.base)
        github_result = {"executed": True, "task_id": first_task, "issue": issue_result, "pr": pr_result}

    smoke_id = next_control_id(target, "SMOKE")
    smoke_file = shiki_path(target, "smoke", f"{smoke_id}.json")
    payload = {
        "id": smoke_id,
        "repo": repo,
        "dry_run": args.dry_run,
        "execute_github": args.execute_github,
        "result": result,
        "github": github_result,
        "created_at": utc_now(),
    }
    write_json(smoke_file, payload)
    output = {"smoke_id": smoke_id, "smoke_file": str(smoke_file), **result, "github": github_result}
    print_json(output)
    return 0


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
    ensure_remote(repo, ROOT, adopt_existing_repo=args.adopt_existing_repo)

    active_branch = current_branch(ROOT)
    if active_branch != branch:
        run(["git", "checkout", "-B", branch], cwd=ROOT)

    if args.commit:
        commit_manifest(ROOT, args.commit_message)

    if args.push:
        push_branch(ROOT, branch)
        set_default_branch(repo, branch)

    configure_claude_code_secret(repo, enabled=args.set_secret, secret_env=args.secret_env)

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
    ensure_git_repo(target, branch)
    ensure_github_repo(repo, visibility)
    ensure_remote(repo, target, adopt_existing_repo=args.adopt_existing_repo)
    install_template(target, force=args.force, validate=args.validate)
    write_target_repo_config(target, repo, branch)

    active_branch = current_branch(target)
    if active_branch != branch:
        run(["git", "checkout", "-B", branch], cwd=target)

    if args.commit:
        commit_manifest(target, args.commit_message)

    if args.push:
        push_branch(target, branch)
        set_default_branch(repo, branch)

    configure_claude_code_secret(repo, enabled=args.set_secret, secret_env=args.secret_env)

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


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def first_line(value: str) -> str:
    return value.strip().splitlines()[0] if value.strip() else ""


def combined_output(probe: dict[str, Any]) -> str:
    return "\n".join(
        part
        for part in [str(probe.get("stdout", "")).strip(), str(probe.get("stderr", "")).strip()]
        if part
    )


def command_probe(name: str, args: list[str]) -> dict[str, Any]:
    if not command_exists(name):
        return {
            "installed": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    result = run([name, *args], cwd=ROOT, check=False)
    return {
        "installed": True,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def claude_auth_status() -> dict[str, Any]:
    version = command_probe("claude", ["--version"])
    auth = command_probe("claude", ["auth", "status"])
    logged_in = False
    auth_method = "unknown"
    api_provider = "unknown"

    if auth["stdout"]:
        try:
            data = json.loads(auth["stdout"])
            logged_in = bool(data.get("loggedIn"))
            auth_method = str(data.get("authMethod", "unknown"))
            api_provider = str(data.get("apiProvider", "unknown"))
        except json.JSONDecodeError:
            logged_in = auth["returncode"] == 0
    elif auth["returncode"] == 0:
        logged_in = True

    ready = bool(version["installed"] and logged_in)
    blocking = []
    if not version["installed"]:
        blocking.append("Claude Code CLI is not installed.")
    elif not logged_in:
        blocking.append("Claude Code is not authenticated; /shiki cannot run inside Claude Code until Claude Code login succeeds.")

    return {
        "installed": version["installed"],
        "version": first_line(version["stdout"]),
        "logged_in": logged_in,
        "auth_method": auth_method,
        "api_provider": api_provider,
        "ready": ready,
        "blocking_reasons": blocking,
        "remediation": "Run `claude auth login` in a terminal or `/login` inside Claude Code, then rerun `/shiki`." if blocking else "",
    }


def codex_auth_status() -> dict[str, Any]:
    version = command_probe("codex", ["--version"])
    auth = command_probe("codex", ["login", "status"])
    logged_in = auth["returncode"] == 0 and "logged in" in combined_output(auth).lower()
    ready = bool(version["installed"] and logged_in)
    blocking = []
    if not version["installed"]:
        blocking.append("Codex CLI is not installed.")
    elif not logged_in:
        blocking.append("Codex CLI is not authenticated.")

    return {
        "installed": version["installed"],
        "version": first_line(combined_output(version)),
        "logged_in": logged_in,
        "ready": ready,
        "blocking_reasons": blocking,
        "remediation": "Run `codex login` or sign in to Codex App before using the Codex entrypoint." if blocking else "",
    }


def github_auth_status() -> dict[str, Any]:
    version = command_probe("gh", ["--version"])
    auth = command_probe("gh", ["auth", "status"])
    logged_in = auth["returncode"] == 0
    ready = bool(version["installed"] and logged_in)
    blocking = []
    if not version["installed"]:
        blocking.append("GitHub CLI is not installed.")
    elif not logged_in:
        detail = first_line(auth["stderr"]) or first_line(auth["stdout"])
        blocking.append(f"GitHub CLI is not authenticated or token is invalid: {detail}".rstrip())

    return {
        "installed": version["installed"],
        "version": first_line(version["stdout"]),
        "logged_in": logged_in,
        "ready": ready,
        "blocking_reasons": blocking,
        "remediation": "Run `gh auth login -h github.com` before Shiki creates GitHub repositories, issues, PRs, or branch protection." if blocking else "",
    }


def shiki_entrypoints_status() -> dict[str, Any]:
    claude = claude_auth_status()
    codex = codex_auth_status()
    github = github_auth_status()
    shiki_command = shutil.which("shiki")
    claude_command = Path(DEFAULT_CLAUDE_COMMAND_PATH).expanduser()
    codex_skill = Path(DEFAULT_CODEX_SKILL_PATH).expanduser()

    entrypoints = {
        "cli": {
            "ready": bool(shiki_command),
            "path": shiki_command,
            "remediation": "" if shiki_command else "Run `shiki install-global` and ensure ~/.local/bin is on PATH.",
        },
        "codex": {
            "ready": codex_skill.exists() and codex["ready"],
            "skill": str(codex_skill),
            "installed": codex_skill.exists(),
            "slash_command_supported": False,
            "usage": "In Codex CLI/App, invoke Shiki with natural language such as `Shiki: create a new GitHub-backed target repo ...`, or run the shell command `shiki start ...`.",
            "remediation": "" if codex_skill.exists() and codex["ready"] else "Run `shiki install-global`, then sign in to Codex. Do not expect `/shiki` in Codex CLI; Codex skills are not custom slash commands.",
        },
        "claude_code": {
            "ready": claude_command.exists() and claude["ready"],
            "slash_command": str(claude_command),
            "installed": claude_command.exists(),
            "remediation": "" if claude_command.exists() and claude["ready"] else "Run `shiki install-global`, then run `claude auth login` or `/login` in Claude Code.",
        },
        "github_backed_operations": {
            "ready": github["ready"],
            "remediation": github["remediation"],
        },
    }
    usable = [name for name, data in entrypoints.items() if data["ready"]]
    blockers = [
        reason
        for status in (claude, codex, github)
        for reason in status["blocking_reasons"]
    ]

    return {
        "root": str(ROOT),
        "config": load_default_config(),
        "entrypoints": entrypoints,
        "runtimes": {
            "codex_front": codex,
            "claude_code": claude,
            "github_cli": github,
        },
        "usable_entrypoints": usable,
        "blocking_reasons": blockers,
        "note": "Claude Code supports `/shiki` through its command file. Codex uses the installed Shiki skill through natural language, not a `/shiki` slash command. Use `shiki start` from Codex or a terminal when a slash command is unavailable.",
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    status = shiki_entrypoints_status()
    if args.json:
        print_json(status)
        return 0

    print("Shiki doctor")
    print(f"root: {status['root']}")
    print(f"usable entrypoints: {', '.join(status['usable_entrypoints']) or 'none'}")
    for name, entrypoint in status["entrypoints"].items():
        marker = "ready" if entrypoint["ready"] else "blocked"
        print(f"- {name}: {marker}")
        remediation = entrypoint.get("remediation")
        if remediation:
            print(f"  remediation: {remediation}")
    if status["blocking_reasons"]:
        print("blocking reasons:")
        for reason in status["blocking_reasons"]:
            print(f"- {reason}")
    print(status["note"])
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
    init.add_argument("--adopt-existing-repo", action="store_true", help="Explicitly rewrite an existing origin to the requested GitHub repo")
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
    github.add_argument("--adopt-existing-repo", action="store_true", help="Explicitly rewrite an existing origin to the requested GitHub repo")
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
    deprecated.add_argument("--adopt-existing-repo", action="store_true", help="Explicitly rewrite an existing origin to the requested GitHub repo")
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

    doctor = subcommands.add_parser("doctor", help="Check Shiki runtime auth and entrypoint readiness")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable status")
    doctor.set_defaults(func=cmd_doctor)

    start = subcommands.add_parser("start", help="One-command interactive Shiki project setup and first run")
    start.add_argument("target_positional", nargs="?", help="Target repository path")
    start.add_argument("--target", default=".", help="Target repository path")
    start.add_argument("--answers-file", help="JSON answers for non-interactive start")
    start.add_argument("--repo", help="GitHub repository as OWNER/NAME")
    start.add_argument("--project-name")
    start.add_argument("--goal")
    start.add_argument("--outcome")
    start.add_argument("--skills-dir", help="Engineering skills directory used by Skill Gate")
    start.add_argument("--completion-condition", action="append", default=[])
    start.add_argument("--non-goal", action="append", default=[])
    start.add_argument("--risk-level", choices=["low", "medium", "high", "critical"])
    start.add_argument("--required-skill", action="append", default=[])
    start.add_argument("--task-title")
    start.add_argument("--task-scope")
    start.add_argument("--acceptance-check", action="append", default=[])
    start.add_argument("--lock", action="append", default=[])
    start.add_argument("--branch", default="main")
    start.add_argument("--private", action="store_true", help="Create a private repo")
    start.add_argument("--public", action="store_true", help=argparse.SUPPRESS)
    start.add_argument("--force", action="store_true", help="Overwrite existing target files during init")
    start.add_argument("--validate", action=argparse.BooleanOptionalAction, default=True)
    start.add_argument("--commit", action=argparse.BooleanOptionalAction, default=True)
    start.add_argument("--commit-message", default="shiki: initialize GitHub-first control plane")
    start.add_argument("--push", action=argparse.BooleanOptionalAction, default=True)
    start.add_argument("--set-secret", action=argparse.BooleanOptionalAction, default=True)
    start.add_argument("--secret-env", default="CLAUDE_CODE_OAUTH_TOKEN")
    start.add_argument("--protect", action=argparse.BooleanOptionalAction, default=True)
    start.add_argument("--adopt-existing-repo", action="store_true", help="Explicitly rewrite an existing origin during init")
    start.add_argument("--required-check", action="append", default=list(DEFAULT_REQUIRED_CHECKS))
    start.add_argument("--create-issues", action=argparse.BooleanOptionalAction, default=True)
    start.add_argument("--create-handoffs", action=argparse.BooleanOptionalAction, default=True)
    start.set_defaults(func=cmd_start)

    plan = subcommands.add_parser("plan", help="Ingest and guide grill-with-docs plans")
    plan_subcommands = plan.add_subparsers(dest="plan_command", required=True)
    plan_ingest = plan_subcommands.add_parser("ingest", help="Persist a grill-with-docs plan as machine-readable Shiki input")
    plan_ingest.add_argument("--target", default=".", help="Target repository path")
    plan_ingest.add_argument("--plan-file", required=True, help="JSON plan produced after grill-with-docs")
    plan_ingest.set_defaults(func=cmd_plan_ingest)
    plan_guide = plan_subcommands.add_parser("guide", help="Show the user-facing path from a prompt to a runnable plan")
    plan_guide.add_argument("--target", default=".", help="Target repository path")
    plan_guide.add_argument("--prompt", help="Goal or task prompt to guide")
    plan_guide.set_defaults(func=cmd_plan_guide)

    run_command = subcommands.add_parser("run", help="Run an ingested plan through Goal, Task DAG, locks, and dispatch setup")
    run_command.add_argument("--target", default=".", help="Target repository path")
    run_command.add_argument("--plan", required=True, help="Plan id like P-0001 or path to a grilled plan JSON")
    run_command.set_defaults(func=cmd_run)

    daemon = subcommands.add_parser("daemon", help="Run the Shiki background inbox processor")
    daemon_subcommands = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_enqueue = daemon_subcommands.add_parser("enqueue-plan", help="Queue a grilled plan for daemon processing")
    daemon_enqueue.add_argument("--target", default=".", help="Target repository path")
    daemon_enqueue.add_argument("--plan-file", required=True)
    daemon_enqueue.set_defaults(func=cmd_daemon_enqueue_plan)
    daemon_run = daemon_subcommands.add_parser("run", help="Process queued Shiki inbox items")
    daemon_run.add_argument("--target", default=".", help="Target repository path")
    daemon_run.add_argument("--once", action="store_true", help="Process at most one item and exit")
    daemon_run.add_argument("--interval", type=float, default=5.0, help="Polling interval in seconds")
    daemon_run.set_defaults(func=cmd_daemon_run)

    runner = subcommands.add_parser("runner", help="Pick up and execute dispatchable Shiki tasks")
    runner_subcommands = runner.add_subparsers(dest="runner_command", required=True)
    runner_next = runner_subcommands.add_parser("next", help="Return the next dispatchable task")
    runner_next.add_argument("--target", default=".", help="Target repository path")
    runner_next.set_defaults(func=cmd_runner_next)
    runner_execute = runner_subcommands.add_parser("execute", help="Execute a command for a ready task and record evidence")
    runner_execute.add_argument("--target", default=".", help="Target repository path")
    runner_execute.add_argument("--task-id", required=True)
    runner_execute.add_argument("--command", required=True)
    runner_execute.set_defaults(func=cmd_runner_execute)
    runner_codex = runner_subcommands.add_parser("codex", help="Run Codex autonomously for a ready Shiki task")
    runner_codex.add_argument("--target", default=".", help="Target repository path")
    runner_codex.add_argument("--task-id", required=True)
    runner_codex.add_argument("--dry-run", action="store_true", help="Show the Codex dispatch without executing it")
    runner_codex.add_argument("--force", action="store_true", help="Run even if the task runtime is not codex")
    runner_codex.set_defaults(func=cmd_runner_codex)

    smoke = subcommands.add_parser("smoke", help="Run live Shiki smoke checks against a GitHub-backed target")
    smoke_subcommands = smoke.add_subparsers(dest="smoke_command", required=True)
    smoke_live = smoke_subcommands.add_parser("live", help="Verify plan/run and optional GitHub issue/PR creation")
    smoke_live.add_argument("--target", default=".", help="Target repository path")
    smoke_live.add_argument("--plan-file", required=True)
    smoke_live.add_argument("--dry-run", action="store_true", help="Run local plan orchestration without GitHub issue/PR creation")
    smoke_live.add_argument("--execute-github", action="store_true", help="Also create GitHub issue and PR evidence")
    smoke_live.add_argument("--push-branch", action="store_true", help="Create, commit, and push the smoke task branch before PR creation")
    smoke_live.add_argument("--base", default="main")
    smoke_live.set_defaults(func=cmd_smoke_live)

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
    repair_packet.add_argument(
        "--required-skill",
        default="tdd",
        choices=["tdd", "diagnose", "grill-with-docs", "improve-codebase-architecture", "evidence-only", "none"],
    )
    repair_packet.add_argument("--verification-command", action="append", required=True)
    repair_packet.add_argument("--evidence-required", action="append", default=[])
    repair_packet.add_argument("--stop-condition", default="Stop after this packet is satisfied or after three failed attempts.")
    repair_packet.set_defaults(func=cmd_repair_packet)

    github_control = subcommands.add_parser("github", help="Create GitHub evidence from Shiki state")
    github_subcommands = github_control.add_subparsers(dest="github_command", required=True)
    github_issue = github_subcommands.add_parser("issue", help="Create a GitHub issue for a Shiki task")
    github_issue.add_argument("--target", default=".", help="Target repository path")
    github_issue.add_argument("--task-id", required=True)
    github_issue.set_defaults(func=cmd_github_issue)
    github_pr = github_subcommands.add_parser("pr", help="Create a GitHub PR for a Shiki task")
    github_pr.add_argument("--target", default=".", help="Target repository path")
    github_pr.add_argument("--task-id", required=True)
    github_pr.add_argument("--base", default="main")
    github_pr.add_argument("--head")
    github_pr.set_defaults(func=cmd_github_pr)

    handoff = subcommands.add_parser("handoff", help="Write Codex handoff documents from Shiki state")
    handoff_subcommands = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_task = handoff_subcommands.add_parser("task", help="Write a Codex task handoff")
    handoff_task.add_argument("--target", default=".", help="Target repository path")
    handoff_task.add_argument("task_id")
    handoff_task.set_defaults(func=cmd_handoff_task)
    handoff_repair = handoff_subcommands.add_parser("repair", help="Write a Codex repair handoff")
    handoff_repair.add_argument("--target", default=".", help="Target repository path")
    handoff_repair.add_argument("repair_id")
    handoff_repair.set_defaults(func=cmd_handoff_repair)

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
