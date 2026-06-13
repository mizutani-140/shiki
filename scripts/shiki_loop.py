#!/usr/bin/env python3
"""Autonomous post-freeze Goal loop (ADR 0008/0009).

The decision engine (`decide_task_action`, `decide_goal_action`) is pure: it
maps a task snapshot to exactly one action and never touches the filesystem,
git, or GitHub. Effectors execute one action at a time through the existing
control-plane surfaces (runner adapters, repair packets, `gh`), so every state
transition stays deterministic and ledger-backed:

    LLM outputs may vary. State transitions must not vary.

Stop conditions are exactly: repair-limit exhaustion, Guardian-gated risk,
blocked evidence, or Goal completion. A Spec Amendment is operator-initiated:
the operator interrupts the loop, runs the scoped re-grill, re-stamps the
freeze, and restarts the loop.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any

from shiki_config import configured_required_checks
from shiki_contracts import DEFAULT_REQUIRED_CHECKS
from shiki_github import create_github_pr_for_task, github_env, target_provider_config
from shiki_process import ShikiError, print_json, read_json, run, shiki_path, target_path, write_json
from shiki_runtime import dispatch_runner_task
from shiki_runtime_adapters import get_runner_adapter
from shiki_tasks import (
    append_ledger,
    allocate_worktree_record,
    cmd_goal_complete,
    load_task,
    tasks_for_goal,
    try_acquire_locks,
    worktree_record,
    write_task_handoff,
)

AUTO_MERGE_RISKS = {"low", "medium"}
# The Guardian/policy gate. It enforces guardian-policy.json (human
# review/label/comment OR an external AI guardian review, ADR 0010) and is the
# ONLY required check that must never become an auto-repair target: an
# autonomous runner must never be instructed to "make the Guardian gate pass".
POLICY_GATE = "MergeGate policy check"
CCA_VERDICT_CHECK = "CCA verdict"
MAX_CCA_RERUNS = 2

# Engine action names, in execution priority order for a goal pass.
ACTION_PRIORITY = (
    "mark_done",
    "merge",
    "rerun_cca",
    "dispatch_repair",
    "create_pr",
    "dispatch",
    "unblock",
)
STOP_ACTIONS = {"stop_guardian", "stop_blocked"}
WAIT_ACTIONS = {"wait_checks", "wait_runner", "wait_dependencies", "none"}


def decide_task_action(
    task: dict[str, Any],
    *,
    checks: dict[str, str] | None,
    pr_state: dict[str, Any] | None,
    repair_attempts: int,
    repair_limit: int,
    required_checks: list[str],
    cca_reruns: int = 0,
) -> dict[str, Any]:
    """Pure decision for one task. checks values: pass | fail | pending."""
    task_id = str(task.get("id"))
    status = str(task.get("status", ""))

    if status == "done":
        return {"action": "none", "task_id": task_id, "reason": "task is done"}
    if status in {"planned", "blocked"}:
        return {"action": "wait_dependencies", "task_id": task_id, "reason": f"task is {status}"}
    if status == "ready":
        return {"action": "dispatch", "task_id": task_id, "reason": "task is ready for the implementer runtime"}
    if status == "running":
        return {"action": "wait_runner", "task_id": task_id, "reason": "implementer session is running"}
    if status == "repair-needed":
        if not task.get("expected_pr"):
            return {
                "action": "stop_blocked",
                "task_id": task_id,
                "reason": "implementer session failed before a PR exists; repair packets require a PR — diagnose or re-dispatch manually",
            }
        if repair_attempts >= repair_limit:
            return {
                "action": "stop_guardian",
                "task_id": task_id,
                "reason": f"repair attempt limit reached ({repair_attempts}/{repair_limit}); Guardian decision required",
            }
        return {"action": "dispatch_repair", "task_id": task_id, "reason": "task needs a bounded repair"}
    if status != "review":
        return {"action": "stop_blocked", "task_id": task_id, "reason": f"unknown task status {status!r}"}

    if pr_state and pr_state.get("error"):
        return {"action": "wait_checks", "task_id": task_id, "reason": "PR state is temporarily unavailable; retrying"}
    if not pr_state:
        return {"action": "create_pr", "task_id": task_id, "reason": "implementation is in review with no PR"}
    if pr_state.get("merged"):
        return {"action": "mark_done", "task_id": task_id, "reason": "PR is merged"}

    checks = checks or {}
    results = {name: checks.get(name, "pending") for name in required_checks}
    failed = sorted(name for name, value in results.items() if value == "fail")
    pending = sorted(name for name, value in results.items() if value == "pending")

    # A failing Guardian gate must NEVER be laundered into auto-remediation. The
    # policy gate is held apart from genuinely repairable checks: it never enters
    # a repair packet, and when it is the only thing red the loop stops for a
    # recorded authority. This closes the impersonation pathway ADR 0010 exists
    # to prevent — an autonomous runner is never told to "make the Guardian gate
    # pass" — while still letting high/critical tasks iterate real repairs (the
    # policy gate stays red until approval, which is expected, not a repair item).
    repairable_failed = sorted(name for name in failed if name != POLICY_GATE)
    repairable_pending = sorted(name for name in pending if name != POLICY_GATE)
    policy_failed = POLICY_GATE in failed

    if repairable_failed:
        # Genuine check failures exist (CCA, mirror, metadata, ...). The policy
        # gate, if also red, is stripped — it is never handed to the runner.
        cca_completion_race = set(repairable_failed) == {CCA_VERDICT_CHECK}
        if cca_completion_race and repairable_pending:
            # CCA judged while sibling checks were still in flight; let them settle.
            return {"action": "wait_checks", "task_id": task_id, "reason": f"CCA judged early; waiting for pending checks: {', '.join(pending)}"}
        if cca_completion_race and cca_reruns < MAX_CCA_RERUNS:
            return {"action": "rerun_cca", "task_id": task_id, "reason": "only the CCA verdict failed against green siblings; rerun after green"}
        if repair_attempts >= repair_limit:
            return {
                "action": "stop_guardian",
                "task_id": task_id,
                "reason": f"required checks failed ({', '.join(repairable_failed)}) and repair attempt limit reached",
            }
        return {
            "action": "dispatch_repair",
            "task_id": task_id,
            "reason": f"required checks failed: {', '.join(repairable_failed)}",
            "failed_checks": repairable_failed,
        }

    if policy_failed:
        # All repairable checks are green; only the Guardian/policy gate is red.
        # The gate said NO (or no authority has approved yet): a recorded
        # authority must resolve it. Never rerun (CCA is green) or auto-repair.
        return {
            "action": "stop_guardian",
            "task_id": task_id,
            "reason": "the MergeGate policy Guardian gate is failing with all other checks green; a recorded authority must resolve it (never auto-repaired)",
        }

    if pending:
        return {"action": "wait_checks", "task_id": task_id, "reason": f"required checks pending: {', '.join(pending)}"}

    risk = task.get("risk_level")
    if risk is None:
        return {
            "action": "stop_guardian",
            "task_id": task_id,
            "reason": "task has no recorded risk level; auto-merge fails closed",
        }
    risk = str(risk)
    if risk in AUTO_MERGE_RISKS:
        return {"action": "merge", "task_id": task_id, "reason": f"all required checks green and risk {risk} permits auto-merge"}
    # High/critical risk requires Guardian approval, but the "MergeGate policy
    # check" required check IS the Guardian gate: it enforces guardian-policy.json
    # (human review/label/comment OR an external AI guardian review, ADR 0010).
    # When it is green, Guardian approval — by whatever authority — was recorded,
    # so the loop may merge autonomously.
    if "MergeGate policy check" in required_checks:
        return {
            "action": "merge",
            "task_id": task_id,
            "reason": f"all required checks green incl. the MergeGate policy Guardian gate; risk {risk} approved by recorded authority",
        }
    return {
        "action": "stop_guardian",
        "task_id": task_id,
        "reason": f"all required checks green but risk {risk} requires Guardian approval and no MergeGate policy gate is configured",
    }


def decide_goal_action(decisions: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure choice of the single next action for a goal pass."""
    if tasks and all(task.get("status") == "done" for task in tasks):
        return {"action": "goal_complete", "reason": "every task is done"}
    for decision in decisions:
        if decision["action"] in STOP_ACTIONS:
            return decision
    for action in ACTION_PRIORITY:
        for decision in decisions:
            if decision["action"] == action:
                return decision
    # Nothing actionable: if anything is dependency-blocked while siblings
    # are merely waiting on checks/runner, keep waiting.
    for decision in decisions:
        if decision["action"] in {"wait_checks", "wait_runner"}:
            return decision
    for decision in decisions:
        if decision["action"] == "wait_dependencies":
            return {"action": "unblock", "task_id": decision["task_id"], "reason": "attempt to unblock dependency-complete tasks"}
    return {"action": "none", "reason": "no actionable task"}


def _gh(target: Path, args: list[str], *, check: bool = True):
    config = target_provider_config(target)
    return run(["gh", *args], cwd=target, env=github_env(config) if config else None, check=check)


def _check_bucket(value: str) -> str:
    if value in {"pass", "success"}:
        return "pass"
    if value in {"pending", "queued", "in_progress"}:
        return "pending"
    return "fail"


def snapshot_pr(target: Path, task: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
    pr = task.get("expected_pr")
    if not pr:
        return None, {}
    view = _gh(target, ["pr", "view", str(pr), "--json", "state,mergedAt,headRefOid"], check=False)
    if view.returncode != 0:
        # Transient gh/network/auth failure must not be read as "no PR".
        return {"number": pr, "error": True}, {}
    state = json.loads(view.stdout)
    pr_state = {
        "number": pr,
        "state": state.get("state"),
        "merged": bool(state.get("mergedAt")),
        "head_sha": state.get("headRefOid"),
    }
    checks: dict[str, str] = {}
    started: dict[str, str] = {}
    result = _gh(target, ["pr", "checks", str(pr), "--json", "name,bucket,startedAt"], check=False)
    if result.returncode in {0, 8} and result.stdout.strip():
        for entry in json.loads(result.stdout):
            name = str(entry.get("name"))
            bucket = _check_bucket(str(entry.get("bucket", "")))
            started_at = str(entry.get("startedAt") or "")
            # Duplicate check runs share one name (parallel triggers, reruns):
            # the LATEST run is authoritative; a stale pass must not mask a
            # current failure.
            if name not in checks or started_at >= started.get(name, ""):
                checks[name] = bucket
                started[name] = started_at
    return pr_state, checks


def repair_attempts_for(target: Path, task_id: str) -> int:
    repairs_dir = shiki_path(target, "repairs")
    if not repairs_dir.exists():
        return 0
    count = 0
    for path in repairs_dir.glob("RP-*.json"):
        packet = read_json(path)
        if packet.get("task_id") == task_id:
            count += 1
    return count


def _save_task(target: Path, task: dict[str, Any]) -> None:
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), task)


def _release_lock(target: Path, task_id: str) -> None:
    lock_file = shiki_path(target, "locks", f"{task_id}.json")
    if not lock_file.exists():
        return
    record = read_json(lock_file)
    if record.get("state") != "released":
        record["state"] = "released"
        write_json(lock_file, record)


def _mark_done(target: Path, task_id: str, reason: str) -> dict[str, Any]:
    _release_lock(target, task_id)
    task = load_task(target, task_id)
    task["status"] = "done"
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task_id,
        ledger_type="check",
        summary=f"Goal loop marked {task_id} done: {reason}",
        evidence=[f".shiki/tasks/{task_id}.json"],
    )
    task.setdefault("ledger_evidence", []).append(ledger_id)
    _save_task(target, task)
    return {"task_id": task_id, "status": "done", "ledger_id": ledger_id}


def _unblock_ready_tasks(target: Path, goal_id: str) -> list[str]:
    unblocked: list[str] = []
    for task in tasks_for_goal(target, goal_id):
        if task.get("status") != "planned":
            continue
        dependencies = [load_task(target, dep) for dep in task.get("dependencies", [])]
        if any(dep.get("status") != "done" for dep in dependencies):
            continue
        ok, blockers, _ = try_acquire_locks(target, task["id"])
        if not ok:
            continue
        if worktree_record(target, task["id"]) is None:
            allocate_worktree_record(target, task["id"])
        handoff = shiki_path(target, "handoffs", f"{task['id']}-task.md")
        if not handoff.exists():
            write_task_handoff(target, task["id"])
        unblocked.append(task["id"])
    return unblocked


def _dispatch(target: Path, task: dict[str, Any], *, repair_id: str | None = None) -> int:
    runtime = str(task.get("assigned_runtime", "claude-code"))
    adapter = get_runner_adapter(runtime)
    if not repair_id and not shiki_path(target, "handoffs", f"{task['id']}-task.md").exists():
        write_task_handoff(target, task["id"])
    args = argparse.Namespace(
        target=str(target),
        task_id=task["id"],
        dry_run=False,
        force=False,
        repair_id=repair_id,
    )
    return dispatch_runner_task(args, adapter)


def _dispatch_repair(target: Path, task: dict[str, Any], failed_checks: list[str], attempt: int) -> dict[str, Any]:
    from shiki_tasks import cmd_handoff_repair, create_repair_packet

    pr = task.get("expected_pr")
    if not pr:
        raise ShikiError(f"task {task['id']} has no PR; repair packets require an existing PR")
    repair_id, _, _ = create_repair_packet(
        target,
        task_id=task["id"],
        pr=int(pr),
        attempt=attempt,
        failing_items=[f"required check failed: {name}" for name in failed_checks] or ["task is repair-needed"],
        failing_acceptance_criteria=[],
        minimal_changes=["Fix the failing required checks without broadening scope."],
        prohibited_changes=["Do not modify files outside the task locks.", "Do not weaken checks or validators."],
        required_skill="diagnose",
        verification_commands=["python3 scripts/validate_shiki.py"],
        evidence_required=["Push the fix to the task branch and let required checks re-run."],
        stop_condition="Stop after this packet is satisfied or after three failed attempts.",
    )
    cmd_handoff_repair(argparse.Namespace(target=str(target), repair_id=repair_id))
    returncode = _dispatch(target, load_task(target, task["id"]), repair_id=repair_id)
    return {"repair_id": repair_id, "returncode": returncode}


def _sync_state_to_branch(target: Path, task_id: str, ledger_id: str | None) -> str:
    """Commit the PR-linkage .shiki state into the task branch.

    MergeGate judges the PR HEAD checkout, so expected_pr, the worktree PR
    field, and the PR-created ledger entry must ride on the task branch, not
    only in the coordinator checkout.
    """
    import shutil

    record = worktree_record(target, task_id)
    if not record:
        return "no worktree record; state sync skipped"
    worktree_path = Path(record["path"]).expanduser().resolve()
    if not worktree_path.exists() or worktree_path == target.resolve():
        return "worktree unavailable for state sync; reconcile the PR branch manually"
    relatives = [f".shiki/tasks/{task_id}.json", f".shiki/worktrees/{task_id}.json"]
    if ledger_id:
        relatives.append(f".shiki/ledger/{ledger_id}.json")
    for relative in relatives:
        source = target / relative
        destination = worktree_path / relative
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    run(["git", "add", ".shiki"], cwd=worktree_path, check=False)
    commit = run(
        ["git", "commit", "-m", f"shiki: link PR evidence to {task_id} (goal loop)"],
        cwd=worktree_path,
        check=False,
    )
    if commit.returncode != 0:
        return "nothing to commit for state sync"
    push = run(["git", "push"], cwd=worktree_path, check=False)
    if push.returncode != 0:
        return "state committed on the task branch; push failed — push manually"
    return "PR evidence committed and pushed to the task branch"


def execute_action(target: Path, goal_id: str, decision: dict[str, Any], *, repair_limit: int) -> dict[str, Any]:
    action = decision["action"]
    task_id = decision.get("task_id")
    result: dict[str, Any] = {"action": action, "task_id": task_id, "reason": decision.get("reason")}

    if action in WAIT_ACTIONS or action in STOP_ACTIONS or action == "goal_complete":
        if action == "goal_complete":
            cmd_goal_complete(argparse.Namespace(target=str(target), goal_id=goal_id, summary=None))
            result["goal_status"] = "complete"
        return result

    task = load_task(target, task_id)
    if action == "dispatch":
        result["returncode"] = _dispatch(target, task)
    elif action == "create_pr":
        result.update(create_github_pr_for_task(target, task_id, base="main"))
        result["state_sync"] = _sync_state_to_branch(target, task_id, result.get("ledger_id"))
    elif action == "rerun_cca":
        pr_state, _ = snapshot_pr(target, task)
        head_sha = (pr_state or {}).get("head_sha")
        runs = _gh(
            target,
            ["run", "list", "--workflow", "shiki-cca-completion.yml", "--limit", "10", "--json", "databaseId,conclusion,headSha"],
            check=False,
        )
        rerun = None
        if runs.returncode == 0 and runs.stdout.strip():
            for entry in json.loads(runs.stdout):
                if entry.get("conclusion") != "failure":
                    continue
                if head_sha and entry.get("headSha") != head_sha:
                    continue
                rerun = entry["databaseId"]
                break
        if rerun is None:
            result["rerun"] = "no failed CCA run found"
        else:
            _gh(target, ["run", "rerun", str(rerun), "--failed"], check=False)
            result["rerun"] = rerun
        ledger_id = append_ledger(
            target,
            goal_id=goal_id,
            task_id=task_id,
            ledger_type="check",
            summary=f"Goal loop reran CCA for {task_id} after sibling checks settled",
            evidence=[f"gh run rerun {rerun} --failed" if rerun else "no failed run found"],
        )
        task = load_task(target, task_id)
        task.setdefault("ledger_evidence", []).append(ledger_id)
        task["cca_rerun_count"] = int(task.get("cca_rerun_count") or 0) + 1
        _save_task(target, task)
    elif action == "dispatch_repair":
        attempt = repair_attempts_for(target, task_id) + 1
        result.update(_dispatch_repair(target, task, decision.get("failed_checks", []), attempt))
    elif action == "merge":
        pr = task.get("expected_pr")
        merge = _gh(target, ["pr", "merge", str(pr), "--merge"], check=False)
        if merge.returncode != 0:
            result["action"] = "stop_blocked"
            result["merge_error"] = (merge.stderr or merge.stdout).strip()[-300:]
            result["reason"] = f"gh pr merge {pr} failed; resolve manually (branch protection, conflicts, or auth)"
            return result
        ledger_id = append_ledger(
            target,
            goal_id=goal_id,
            task_id=task_id,
            ledger_type="mergegate",
            summary=f"Goal loop merged PR #{pr} for {task_id} (required checks green, risk {task.get('risk_level', 'low')})",
            evidence=[f"gh pr merge {pr} --merge"],
        )
        task = load_task(target, task_id)
        task.setdefault("ledger_evidence", []).append(ledger_id)
        _save_task(target, task)
        result.update(_mark_done(target, task_id, f"PR #{pr} merged by the goal loop"))
        result["unblocked"] = _unblock_ready_tasks(target, goal_id)
    elif action == "mark_done":
        result.update(_mark_done(target, task_id, "PR already merged"))
        result["unblocked"] = _unblock_ready_tasks(target, goal_id)
    elif action == "unblock":
        unblocked = _unblock_ready_tasks(target, goal_id)
        result["unblocked"] = unblocked
        if not unblocked:
            result["action"] = "stop_blocked"
            result["reason"] = "dependency-blocked tasks could not be unblocked (incomplete dependencies or lock conflicts)"
    else:
        raise ShikiError(f"goal loop cannot execute unknown action {action!r}")
    return result


def configured_repair_limit(target: Path) -> int:
    """Target-config repair limit, hard-capped at 3 by the repair-packet schema."""
    from shiki_config import load_shiki_config

    config = load_shiki_config(target)
    raw = (config.get("defaults") or {}).get("automatic_repair_limit")
    try:
        value = int(raw) if raw is not None else 3
    except (TypeError, ValueError):
        value = 3
    return max(1, min(value, 3))


def goal_loop_step(target: Path, goal_id: str) -> dict[str, Any]:
    repair_limit = configured_repair_limit(target)
    required_checks = configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)

    tasks = tasks_for_goal(target, goal_id)
    if not tasks:
        raise ShikiError(f"goal {goal_id} has no tasks")
    decisions = []
    for task in tasks:
        pr_state, checks = (None, {})
        if task.get("status") == "review":
            pr_state, checks = snapshot_pr(target, task)
        decisions.append(
            decide_task_action(
                task,
                checks=checks,
                pr_state=pr_state,
                repair_attempts=repair_attempts_for(target, str(task.get("id"))),
                repair_limit=repair_limit,
                required_checks=list(required_checks),
                cca_reruns=int(task.get("cca_rerun_count") or 0),
            )
        )
    decision = decide_goal_action(decisions, tasks)
    return execute_action(target, goal_id, decision, repair_limit=repair_limit)


def cmd_loop_step(args: argparse.Namespace) -> int:
    from shiki_tasks import require_github_first_target

    target = target_path(args.target)
    require_github_first_target(target)
    result = goal_loop_step(target, args.goal_id)
    print_json(result)
    return 1 if result["action"] in STOP_ACTIONS else 0


def cmd_loop_run(args: argparse.Namespace) -> int:
    from shiki_tasks import require_github_first_target

    target = target_path(args.target)
    require_github_first_target(target)
    history: list[dict[str, Any]] = []
    for cycle in range(1, args.max_cycles + 1):
        result = goal_loop_step(target, args.goal_id)
        result["cycle"] = cycle
        history.append({key: result.get(key) for key in ("cycle", "action", "task_id", "reason")})
        if result["action"] == "goal_complete":
            print_json({"goal_id": args.goal_id, "outcome": "complete", "cycles": cycle, "history": history})
            return 0
        if result["action"] in STOP_ACTIONS:
            print_json({"goal_id": args.goal_id, "outcome": result["action"], "reason": result.get("reason"), "cycles": cycle, "history": history})
            return 1
        if result["action"] in WAIT_ACTIONS:
            time.sleep(args.interval)
    print_json({"goal_id": args.goal_id, "outcome": "max-cycles", "cycles": args.max_cycles, "history": history})
    return 1
