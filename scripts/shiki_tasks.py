#!/usr/bin/env python3
"""Goal, task, DAG, ledger, lock, and handoff lifecycle helpers."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import Any

from shiki_git import github_origin, is_git_repo
from shiki_locks import active_lock_conflicts, path_matches_lock
from shiki_process import ShikiError, print_json, read_json, run, shiki_path, slugify, target_path, utc_now, write_json, ensure_control_dirs
from shiki_state import append_ledger_entry, new_control_id

# Loop task-lock guard (PRD 0002 T4, gap #5 / Q5).
#
# A loop-executed task is dispatched into an isolated worktree where the goal
# loop syncs the FULL .shiki evidence set onto the task branch (see
# shiki_loop._sync_state_to_branch / _evidence_relatives_for_task: task file,
# worktree record, every ledger, and the runner/EXEC + report files those
# ledgers reference). MergeGate's files-outside-locks gate then sees those .shiki
# mutations on the branch, so a loop task must hold a lock that covers all of
# .shiki — otherwise the loop's own evidence sync lands outside the task's locks.
#
# LOOP_EXECUTED_RUNTIMES is the explicit set of runtimes the goal loop dispatches
# into a worktree today (claude-code, codex). It is intentionally narrower than
# every runner-role runtime: the placeholders `other` and `hermes-runner` carry a
# runner role but have no loop adapter, and flagging them would spam advisories.
LOOP_EXECUTED_RUNTIMES: frozenset[str] = frozenset({"claude-code", "codex"})

# Canonical lock that covers the whole .shiki evidence tree the loop syncs.
LOOP_SHIKI_STATE_LOCK = "path:.shiki/**"

# Representative paths across every .shiki subtree the loop syncs. A lock-set
# "covers .shiki state" only if one lock matches ALL of them; a single-subtree
# lock such as path:.shiki/tasks/** does not.
_SHIKI_STATE_PROBES: tuple[str, ...] = (
    ".shiki/tasks/probe.json",
    ".shiki/worktrees/probe.json",
    ".shiki/ledger/probe.json",
    ".shiki/runner/probe.json",
    ".shiki/reports/probe.json",
)


def is_loop_executed_runtime(runtime: str | None) -> bool:
    """True when the goal loop dispatches this runtime into a worktree."""
    return isinstance(runtime, str) and runtime in LOOP_EXECUTED_RUNTIMES


def locks_cover_shiki_state(locks: list[str] | None) -> bool:
    """True when some single lock in ``locks`` covers the full .shiki tree.

    The loop syncs evidence across many .shiki subtrees, so coverage requires a
    lock that matches every representative subtree path, not a per-subtree lock.
    """
    if not locks:
        return False
    for lock in locks:
        if not isinstance(lock, str):
            continue
        if all(path_matches_lock(probe, lock) for probe in _SHIKI_STATE_PROBES):
            return True
    return False

# Safe default for the loop-observed TDD gate (ADR 0011): the command the goal
# loop exec's in the task worktree before opening the PR. A task may override it
# with its own structured `test_command`; the loop NEVER exec's the free-form
# `acceptance_checks` prose.
DEFAULT_TEST_COMMAND = "python3 -m unittest discover -s tests"


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
        "MEM": ["memories"],
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
        "MEM": ["memories"],
    }.get(prefix, [])
    base = target / ".shiki"
    for _ in range(10):
        candidate = new_control_id(prefix)
        if not directories:
            return candidate
        if not any((base / directory / f"{candidate}.json").exists() for directory in directories):
            return candidate
    raise ShikiError(f"could not allocate a unique {prefix} id after 10 attempts")


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
    return active_lock_conflicts(target, task_id, locks)


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


def require_grilled_plan(plan: dict[str, Any]) -> None:
    grill = plan.get("grill_with_docs")
    if not isinstance(grill, dict) or grill.get("status") != "complete":
        raise ShikiError("plan must include grill_with_docs.status=complete before Shiki can run it")
    freeze = plan.get("spec_freeze")
    if not isinstance(freeze, dict) or freeze.get("status") != "frozen":
        raise ShikiError(
            "plan must include spec_freeze.status=frozen (operator approval of the PRD/requirements) "
            "before Shiki can run it; see CONTEXT.md 'Spec Freeze' and ADR 0009"
        )
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
    try:
        return append_ledger_entry(
            target,
            lambda ledger_id: {
                "id": ledger_id,
                "timestamp": utc_now(),
                "goal_id": goal_id,
                "task_id": task_id,
                "type": ledger_type,
                "actor": "shiki-cli",
                "summary": summary,
                "evidence": evidence,
                "links": links or [],
            },
        )
    except FileExistsError as error:
        raise ShikiError(str(error)) from error


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
        "assigned_runtime": task_plan.get("runtime", "claude-code"),
        "risk_level": task_plan.get("risk_level", "low"),
        "required_skills": task_plan.get("required_skills") or ["tdd", "code-review"],
        "acceptance_checks": task_plan["acceptance_checks"],
        # The loop-observed TDD gate (ADR 0011) exec's THIS structured command in
        # the worktree before opening the PR. acceptance_checks is free-form
        # prose+commands and is never exec'd; test_command is the safe, explicit
        # surface (default: the repo's unittest-discover suite).
        "test_command": task_plan.get("test_command") or DEFAULT_TEST_COMMAND,
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
    # Dispatch-time .shiki/** guarantee: the lock record a loop task holds must
    # cover the evidence the loop syncs to its branch. The registered task file
    # is left untouched (the orchestrator owns registration / frozen-plan
    # lock-match); only the lock record derived here gains the guarantee.
    locks = task.get("locks", [])
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
            "Obtain the operator's explicit approval of the PRD/requirements (Spec Freeze).",
            "Write a machine-readable plan JSON with grill_with_docs.status=complete and spec_freeze.status=frozen.",
            "Run shiki plan ingest --plan-file PLAN.json.",
            "Run shiki run --plan P-0001 to create the Goal, Task DAG, locks, and first dispatchable worktree.",
        ],
        "plan_contract": {
            "required_goal_fields": ["title", "outcome", "grill_with_docs", "spec_freeze", "tasks"],
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
        # Structured loop-observed TDD command (ADR 0011); falls back to the safe
        # unittest-discover default when the CLI did not supply one.
        "test_command": getattr(args, "test_command", None) or DEFAULT_TEST_COMMAND,
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


def create_repair_packet(
    target: Path,
    *,
    task_id: str,
    pr: int,
    attempt: int,
    failing_items: list[str],
    failing_acceptance_criteria: list[str],
    minimal_changes: list[str],
    prohibited_changes: list[str],
    required_skill: str,
    verification_commands: list[str],
    evidence_required: list[str],
    stop_condition: str,
) -> tuple[str, Path, str]:
    ensure_control_dirs(target)
    task = load_task(target, task_id)
    if attempt > 3:
        raise ShikiError("repair attempt limit is 3")
    if int(pr) < 1:
        raise ShikiError("repair packets require an existing PR (pr >= 1)")

    repair_id = next_control_id(target, "RP")
    packet = {
        "repair_id": repair_id,
        "goal_id": task["goal_id"],
        "task_id": task_id,
        "pr": pr,
        "attempt": attempt,
        "failing_checklist_items": failing_items or [],
        "failing_acceptance_criteria": failing_acceptance_criteria or [],
        "minimal_required_changes": minimal_changes,
        "prohibited_changes": prohibited_changes or [],
        "required_skill": required_skill,
        "verification_commands": verification_commands,
        "evidence_required": evidence_required or ["Attach verification output to the PR."],
        "stop_condition": stop_condition,
        "created_at": utc_now(),
    }
    repair_file = shiki_path(target, "repairs", f"{repair_id}.json")
    write_json(repair_file, packet)
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task_id,
        ledger_type="repair",
        summary=f"Repair packet {repair_id} created for PR #{pr}",
        evidence=[str(repair_file.relative_to(target))],
    )
    task["status"] = "repair-needed"
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{task_id}.json"), task)
    # Auto-capture (proposal 3.3, source=repair). Reference the repair ledger
    # only — never the failing output, repair prompt, or model response.
    from shiki_memory import capture_failure

    capture_failure(
        target,
        source_kind="repair",
        area="loop",
        claim=f"Repair packet created for {task_id} (PR #{pr}, attempt {attempt}); failing checks need a bounded fix.",
        goal_id=task["goal_id"],
        task_id=task_id,
        evidence_refs=[f".shiki/ledger/{ledger_id}.json"],
    )
    return repair_id, repair_file, ledger_id


def cmd_repair_packet(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    repair_id, repair_file, ledger_id = create_repair_packet(
        target,
        task_id=args.task_id,
        pr=args.pr,
        attempt=args.attempt,
        failing_items=args.failing_item or [],
        failing_acceptance_criteria=args.failing_acceptance_criteria or [],
        minimal_changes=args.minimal_change,
        prohibited_changes=args.prohibited_change or [],
        required_skill=args.required_skill,
        verification_commands=args.verification_command,
        evidence_required=args.evidence_required or [],
        stop_condition=args.stop_condition,
    )
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
    # Scorecard (proposal 3.6) lives INSIDE the report file, never on stdout, so
    # the goal-complete stdout stays json_get_last compatible. Generation is
    # failure tolerant: a scorecard error must not block goal completion.
    try:
        from shiki_memory import compute_scorecard

        report["scorecard"] = compute_scorecard(target, args.goal_id, tasks=tasks)
    except Exception as error:  # noqa: BLE001 - scorecard is failure tolerant (3.6)
        report["scorecard"] = {"goal_id": args.goal_id, "warnings": [f"scorecard generation failed: {error}"]}
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


def write_handoff(target: Path, name: str, body: str) -> Path:
    path = shiki_path(target, "handoffs", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def write_task_handoff(target: Path, task_id: str) -> tuple[Path, str]:
    # Lazy import keeps the shiki_tasks <-> shiki_memory edge deferred (shiki_memory
    # imports shiki_tasks at module load), matching the capture/scorecard hooks.
    from shiki_memory import (
        load_all_memories,
        render_distilled_rules_section,
        select_distilled_rules,
    )

    task = load_task(target, task_id)
    # Consult injection (proposal 0001 v2 §3.5): always emit a Distilled Rules
    # section. Selection is failure-tolerant — any read error degrades to "none
    # applicable" so handoff generation can never break dispatch.
    try:
        goal = load_goal(target, task["goal_id"])
    except Exception:
        # A missing/desynced goal mirror (read_json raises ShikiError) must
        # degrade to "none applicable", never crash the now-unconditional
        # dispatch regeneration.
        goal = None
    try:
        distilled = select_distilled_rules(task, goal, load_all_memories(target))
    except Exception:
        distilled = []
    body = "\n".join(
        [
            f"# Codex Task Handoff: {task['id']}",
            "",
            f"Goal: {task['goal_id']}",
            f"Task: {task['id']}",
            f"Runtime: {task.get('assigned_runtime')}",
            f"Branch: {task.get('expected_branch')}",
            "",
            # Loop-owns-delivery guardrail (precedes Scope so the implementer reads
            # the constraints first): the Shiki goal loop owns the commit/push/PR/
            # merge state transitions. An implementer that opens its own commit or
            # PR breaks the loop's create_pr transition and fails MergeGate.
            "## Execution Protocol (the Shiki loop owns delivery)",
            "- Implement ONLY by editing files in this worktree to satisfy the Scope and Acceptance Checks below.",
            "- Do NOT run `git commit`, `git push`, `git checkout`/`git switch`, or any `gh` command.",
            "- Do NOT create, update, comment on, or merge a pull request.",
            "- The Shiki goal loop owns commit, push, PR creation (with the required MergeGate PR body) and merge; opening your own commit or PR breaks the loop's create_pr state transition and fails MergeGate.",
            "- Stay strictly within the declared Locks below; touch no files outside them.",
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
            "",
            *render_distilled_rules_section(distilled),
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
    return handoff_file, ledger_id


def cmd_handoff_task(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    handoff_file, ledger_id = write_task_handoff(target, args.task_id)
    print_json({"task_id": args.task_id, "handoff_file": str(handoff_file), "ledger_id": ledger_id})
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
