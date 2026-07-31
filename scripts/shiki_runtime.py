#!/usr/bin/env python3
"""Current Shiki daemon, runner, smoke, and runtime auth helpers."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from shiki_git import branch_exists
from shiki_github import create_github_issue_for_task, create_github_pr_for_task, github_repo_from_origin
from shiki_installer import DEFAULT_CLAUDE_COMMAND_PATH, DEFAULT_CODEX_SKILL_PATH
from shiki_process import ROOT, ShikiError
from shiki_process import ensure_control_dirs, first_line, load_default_config, print_json, read_json, require_tool, run, shiki_path, slugify, target_path, utc_now, write_json
from shiki_runtime_adapters import (
    RunnerAdapter,
    claude_auth_status,
    codex_auth_status,
    combined_output,
    command_exists,
    command_probe,
    get_runner_adapter,
)
from shiki_runtime_registry import RuntimeRegistryError, get_runtime, runtime_registry_as_json
from shiki_tasks import append_ledger, load_task, orchestrate_plan, require_github_first_target, require_grilled_plan, next_control_id, task_files, worktree_record


def validate_task_runtime_for_execution(task: dict[str, Any]) -> None:
    runtime = str(task.get("assigned_runtime", ""))
    try:
        get_runtime(runtime)
    except RuntimeRegistryError as error:
        raise ShikiError(f"task {task.get('id', '<unknown>')} has invalid assigned_runtime: {error}") from error

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
    validate_task_runtime_for_execution(task)
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


DEFAULT_BRANCH_FALLBACK = "main"


def _default_branch_name(target: Path) -> str:
    """Name of the repository's default branch.

    Prefers git's own clone-time record of the remote default
    (``refs/remotes/origin/HEAD``), then Shiki's saved config, then the platform
    default the goal loop already assumes everywhere (``main``).
    """
    head = run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=target,
        check=False,
    )
    if head.returncode == 0:
        ref = head.stdout.strip()
        prefix = "origin/"
        name = ref[len(prefix):] if ref.startswith(prefix) else ref
        if name:
            return name
    configured = load_default_config().get("default_branch")
    if configured:
        return str(configured)
    return DEFAULT_BRANCH_FALLBACK


def resolve_default_branch_ref(target: Path) -> str:
    """Resolve a git commit-ish for the default branch to cut a task branch from.

    A new task branch must be cut from the default branch, not from whatever
    branch the coordinator currently sits on: ``git worktree add -b <branch>
    <path>`` with no start-point uses HEAD, so a task dispatched while the
    coordinator sits on a feature branch inherits that branch's tree — the
    failure that put foreign task/goal files and unlisted ledgers into a task
    PR's diff. shiki_loop.py asserts the opposite: a task branch "is cut from
    main".

    The resolved name is bound to a commit-ish, preferring the remote-tracking
    ref ``origin/<name>`` over the local ``<name>`` and refreshing it first with
    a best-effort ``git fetch origin <name>``. This mirrors the loop's own
    closeout path, which fetches and cuts from ``origin/main`` (shiki_loop.py).
    The local branch can lag its remote after other goals merge, so cutting from
    it would drag already-merged foreign state back into a fresh task branch;
    the remote-tracking ref is the authority. The fetch is non-fatal so dispatch
    still works offline when the refs are already present, and the local branch
    remains the fallback when no remote-tracking ref exists. When neither the
    remote-tracking ref nor the local branch exists the default branch cannot be
    resolved, so this fails with a named reason instead of silently cutting the
    new branch from HEAD.
    """
    name = _default_branch_name(target)
    run(["git", "fetch", "origin", name], cwd=target, check=False)
    for ref in (f"origin/{name}", name):
        verified = run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=target,
            check=False,
        )
        if verified.returncode == 0:
            return ref
    raise ShikiError(
        f"cannot resolve default branch {name!r} to cut a task worktree from: "
        f"neither origin/{name!r} nor local {name!r} exists in {target}. "
        "Fetch or create the default branch before dispatch."
    )


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
            base = resolve_default_branch_ref(target)
            run(["git", "worktree", "add", "-b", branch, str(path), base], cwd=target)
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
    if returncode != 0:
        # Auto-capture (proposal 3.3, source=runner_fail). The EXEC record holds
        # the stdout/stderr bodies; the memory stores only a reference to it and
        # a short claim — never the output itself. capture_failure is fail-open.
        from shiki_memory import capture_failure

        capture_failure(
            target,
            source_kind="runner_fail",
            area="runner",
            claim=f"Runner execution failed with a non-zero return code ({returncode}).",
            goal_id=task.get("goal_id"),
            task_id=task.get("id"),
            evidence_refs=[str(record_file.relative_to(target))],
        )
    return record_file, ledger_id


def sync_contract_into_worktree(target: Path, task: dict[str, Any], worktree_path: Path) -> list[str]:
    """Carry the coordinator's CURRENT contract into the dispatch worktree.

    A dispatched session renders its handoff from the coordinator but verifies
    against the worktree's own ``.shiki``. When the contract was amended after
    the worktree was cut — a repair dispatched after a Spec Amendment tightened
    the locks — the worktree still holds the stale terms and the implementer
    judges against the wrong contract. The observed failure mode: a repair read a
    6-lock contract while the coordinator held 9. The safe direction (a lock
    still present in the worktree that the coordinator dropped) merely refuses
    authorised work; the mirror image (a constraint removed in the worktree but
    still enforced) produces a wrong implementation.

    Copy this task's own file, its goal file, and its lock record from the
    coordinator's ``.shiki`` into the worktree's ``.shiki`` before the session
    starts, so it judges against current terms. Only THIS task's / goal's
    id-named files are carried — never a sibling task's or a foreign goal's — so
    the worktree cannot inherit unrelated mirror state. Copy-in-place over
    deterministic id-named paths is idempotent: re-running a dispatch overwrites
    rather than duplicating. Missing coordinator files are skipped, never
    fabricated. Returns the ``.shiki``-relative paths actually synced.
    """
    task_id = str(task.get("id") or "")
    goal_id = str(task.get("goal_id") or "")
    worktree_path = worktree_path.expanduser().resolve()
    if not task_id or not worktree_path.exists() or worktree_path == target.resolve():
        # Headless dispatch requires an isolated worktree; syncing onto the
        # target checkout itself would be a no-op self-copy.
        return []

    relatives = [f".shiki/tasks/{task_id}.json", f".shiki/locks/{task_id}.json"]
    if goal_id:
        relatives.append(f".shiki/goals/{goal_id}.json")

    shiki_root = (target / ".shiki").resolve()
    worktree_shiki_root = (worktree_path / ".shiki").resolve()
    synced: list[str] = []
    for relative in relatives:
        source = (target / relative).resolve()
        destination = (worktree_path / relative).resolve()
        # Containment: never read from outside the coordinator's .shiki nor write
        # outside the worktree's .shiki (defense in depth; these ids are trusted
        # coordinator state, but the copy target is a worktree we must not escape).
        try:
            source.relative_to(shiki_root)
            destination.relative_to(worktree_shiki_root)
        except ValueError:
            continue
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            synced.append(relative)
    return synced


def dispatch_runner_task(args: argparse.Namespace, adapter: RunnerAdapter) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    ensure_control_dirs(target)

    task = load_task(target, args.task_id)
    validate_task_runtime_for_execution(task)
    runtime = str(task.get("assigned_runtime", adapter.name))
    if runtime != adapter.name and not args.force:
        raise ShikiError(f"task {args.task_id} is assigned to {runtime}, not {adapter.name}")
    allowed_statuses = {"ready", "running"}
    if getattr(args, "repair_id", None):
        allowed_statuses.add("repair-needed")
    if task.get("status") not in allowed_statuses:
        raise ShikiError(f"task {args.task_id} is not ready for {adapter.display_name} execution")

    require_tool(adapter.required_tool)
    auth = adapter.auth_status()
    if not auth["ready"]:
        raise ShikiError(f"{adapter.display_name} is not ready. {adapter.auth_remediation}")

    repair_id = getattr(args, "repair_id", None)
    if repair_id:
        handoff_file = shiki_path(target, "handoffs", f"{repair_id}-repair.md")
        if not handoff_file.exists():
            raise ShikiError(f"missing repair handoff file: {handoff_file}. Run `shiki handoff repair {repair_id}` first.")
    else:
        handoff_file = shiki_path(target, "handoffs", f"{args.task_id}-task.md")
        if not handoff_file.exists():
            raise ShikiError(f"missing handoff file: {handoff_file}. Run `shiki handoff task {args.task_id}` first.")

    worktree = ensure_physical_worktree(target, task)
    worktree_path = Path(worktree["path"]).expanduser().resolve()
    if worktree_path == target.resolve():
        raise ShikiError(
            f"task {args.task_id} worktree record points at the target checkout itself; "
            "headless dispatch requires an isolated worktree. Re-run `shiki worktree allocate` with a dedicated path."
        )
    command_label = adapter.command_label(str(handoff_file.relative_to(target)))

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
    # Refresh the worktree's contract mirror from the coordinator before the
    # session starts, so the implementer judges against the CURRENT task, goal
    # and lock record — not the stale copy the worktree was cut with. This closes
    # the repair-after-amendment gap where a session verifies against outdated
    # locks in its own .shiki.
    sync_contract_into_worktree(target, task, worktree_path)
    prompt = handoff_file.read_text()
    result = adapter.execute(worktree_path, prompt)
    record_file, ledger_id = record_runner_result(
        target,
        task,
        command_label,
        result.returncode,
        result.stdout,
        result.stderr,
    )
    task = load_task(target, args.task_id)
    task.setdefault("ledger_evidence", []).append(ledger_id)
    task["status"] = "review" if result.returncode == 0 else "repair-needed"
    write_json(shiki_path(target, "tasks", f"{args.task_id}.json"), task)
    print_json(
        {
            "task_id": args.task_id,
            "returncode": result.returncode,
            "runner_record": str(record_file),
            "ledger_id": ledger_id,
            "worktree": str(worktree_path),
            "status": task["status"],
        }
    )
    return result.returncode


def cmd_runner_codex(args: argparse.Namespace) -> int:
    return dispatch_runner_task(args, get_runner_adapter("codex"))


def cmd_runner_claude(args: argparse.Namespace) -> int:
    return dispatch_runner_task(args, get_runner_adapter("claude-code"))


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
        "runtime_registry": runtime_registry_as_json(),
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
