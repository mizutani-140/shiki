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
from shiki_runtime_adapters import REVIEWER_ADAPTER, get_runner_adapter, parse_code_review_verdict
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
        # Regenerate unconditionally: the handoff embeds the live Distilled
        # Rules section, so a stale cached handoff must never be reused (§3.7).
        write_task_handoff(target, task["id"])
        unblocked.append(task["id"])
    return unblocked


def _dispatch(target: Path, task: dict[str, Any], *, repair_id: str | None = None) -> int:
    runtime = str(task.get("assigned_runtime", "claude-code"))
    adapter = get_runner_adapter(runtime)
    # Dispatch always regenerates the task handoff so injected distilled rules are
    # never stale (§3.7 — the write-if-missing cache is removed). Repair dispatch
    # uses its own repair handoff and is left untouched.
    if not repair_id:
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


def _commit_and_push_implementation(target: Path, task_id: str) -> str:
    """Commit and push the implementer runtime's work to the task branch.

    The headless runner (``claude -p`` / ``codex exec``) writes its changes into
    the task worktree but does not commit; ``create_github_pr_for_task`` opens
    the PR with ``gh pr create`` and needs a pushed branch that has commits.
    Stage everything the runner produced (the worktree is task-scoped), commit
    it, and push the branch (setting upstream) so the PR can be opened and
    later ``.shiki`` syncs can ``git push`` without arguments. Returns a status
    string and never raises into the loop.
    """
    record = worktree_record(target, task_id)
    if not record:
        return "no worktree record; implementation commit skipped"
    worktree_path = Path(record["path"]).expanduser().resolve()
    if not worktree_path.exists() or worktree_path == target.resolve():
        return "worktree unavailable; commit the implementation manually"
    task = load_task(target, task_id)
    branch = str(task.get("expected_branch") or "")
    if not branch:
        return "task has no expected_branch; cannot push the implementation"
    run(["git", "add", "-A"], cwd=worktree_path, check=False)
    # The commit may be a no-op when the runner already committed its own work;
    # that is fine — we decide whether to push from the commit count ahead of
    # main, not from this commit's return code.
    run(
        ["git", "commit", "-m", f"shiki: {task.get('title', task_id)} ({task_id})"],
        cwd=worktree_path,
        check=False,
    )
    ahead = run(["git", "rev-list", "--count", "main..HEAD"], cwd=worktree_path, check=False)
    try:
        count = int((ahead.stdout or "0").strip())
    except (TypeError, ValueError):
        count = 1  # fail open: attempt the push rather than silently skip
    if count == 0:
        return "no implementation changes to commit"
    push = run(["git", "push", "-u", "origin", branch], cwd=worktree_path, check=False)
    if push.returncode != 0:
        return "implementation committed; push failed — push the task branch manually"
    return "implementation committed and pushed to the task branch"


def _evidence_relatives_for_task(target: Path, task: dict[str, Any]) -> list[str]:
    """Every ``.shiki``-relative path that must ride on the task branch.

    MergeGate judges the PR HEAD checkout and fails closed when a referenced
    ledger (or a ``.shiki`` file that ledger's evidence points at) is absent on
    the branch. So the branch needs the task file, its worktree record, every
    ledger in ``task.ledger_evidence``, AND every ``.shiki``-relative path those
    ledgers reference (e.g. ``runner/EXEC``, ``reports/R``) — not a hardcoded
    subset. Only existing files are returned; deduped, deterministically ordered.
    """
    task_id = str(task.get("id"))
    relatives: list[str] = []
    shiki_root = (target / ".shiki").resolve()

    def add(rel: str) -> None:
        # Containment: a ledger evidence ref is untrusted input. A prefix check
        # alone (`startswith('.shiki/')`) does NOT stop traversal — '.shiki/../x'
        # passes it but resolves outside the subtree. Resolve and require the
        # path to stay within target/.shiki before it is synced/copied.
        candidate = (target / rel).resolve()
        try:
            candidate.relative_to(shiki_root)
        except ValueError:
            return  # escapes the .shiki subtree — reject
        if rel not in relatives and candidate.is_file():
            relatives.append(rel)

    add(f".shiki/tasks/{task_id}.json")
    add(f".shiki/worktrees/{task_id}.json")
    for ledger_id in task.get("ledger_evidence") or []:
        ledger_rel = f".shiki/ledger/{ledger_id}.json"
        add(ledger_rel)
        ledger_path = target / ledger_rel
        if not ledger_path.is_file():
            continue
        try:
            entry = read_json(ledger_path)
        except Exception:
            # read_json raises ShikiError (not OSError/ValueError) on a non-dict
            # ledger; a malformed ledger must never crash the sync.
            continue
        for ref in entry.get("evidence") or []:
            ref = str(ref)
            if ref.startswith(".shiki/"):
                add(ref)
    return relatives


def _sync_state_to_branch(target: Path, task_id: str, ledger_id: str | None) -> str:
    """Commit the full ledger-evidence set into the task branch.

    MergeGate judges the PR HEAD checkout, so the task file, worktree record,
    and EVERY file referenced by ``task.ledger_evidence`` (each ledger plus the
    ``.shiki`` paths those ledgers point at) must ride on the task branch — not
    only in the coordinator checkout. ``ledger_id`` is the just-created PR ledger
    (already appended to ``ledger_evidence`` by ``create_github_pr_for_task``);
    it is included defensively.
    """
    import shutil

    record = worktree_record(target, task_id)
    if not record:
        return "no worktree record; state sync skipped"
    worktree_path = Path(record["path"]).expanduser().resolve()
    if not worktree_path.exists() or worktree_path == target.resolve():
        return "worktree unavailable for state sync; reconcile the PR branch manually"
    task = load_task(target, task_id)
    relatives = _evidence_relatives_for_task(target, task)
    if ledger_id:
        extra = f".shiki/ledger/{ledger_id}.json"
        if extra not in relatives and (target / extra).is_file():
            relatives.append(extra)
    shiki_root = (target / ".shiki").resolve()
    worktree_shiki_root = (worktree_path / ".shiki").resolve()
    for relative in relatives:
        source = (target / relative).resolve()
        destination = (worktree_path / relative).resolve()
        # Belt-and-suspenders containment: never read from outside the
        # coordinator's .shiki nor write outside the worktree's .shiki, whatever
        # produced `relatives` (defense in depth against a traversal ref).
        try:
            source.relative_to(shiki_root)
            destination.relative_to(worktree_shiki_root)
        except ValueError:
            continue
        if source.is_file():
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


# The independent read-only reviewer's prompt. The diff is the ONLY thing it
# judges; it returns the structured verdict the loop parses. It is explicitly
# told it is read-only and may not edit — the --allowedTools confinement is the
# real guarantee, this is belt-and-suspenders.
_CODE_REVIEW_PROMPT = (
    "You are an INDEPENDENT pre-PR code reviewer running read-only in a separate "
    "context (ADR 0011). Review ONLY the diff below for correctness bugs, broken "
    "contracts, security issues, data loss, and missing tests. You may use read "
    "tools to inspect the worktree; you may NOT edit anything. Emit a single JSON "
    'object matching the verdict schema: verdict "clean" when nothing blocking is '
    'found, "blocking" when a blocking issue exists, with a findings array. Do not '
    "wrap the JSON in prose.\n\n## Task diff (git diff main...HEAD)\n"
)


def _run_pre_pr_code_review(target: Path, task_id: str) -> dict[str, Any]:
    """Run the independent read-only code-review verifier over the task diff.

    Loop-owned quality-gate step (ADR 0011): the reviewer is the same model as the
    implementer but in a separate context, confined to read tools (no edit tools),
    bound to a structured verdict. The loop parses that verdict deterministically.

    Returns a dict with ``status`` in {clean, blocking, fail}:

    * ``clean``    — verdict parsed as clean; a type:"check" "code-review" ledger
      is recorded and ``pre_pr_code_review`` is written onto the task so the
      PR-12 ``## Pre-PR code review`` body section renders from it.
    * ``blocking`` — verdict parsed as blocking. Fail-closed: a blocking pre-PR
      review CANNOT anchor a repair packet (no PR exists yet at create_pr time by
      construction), so the caller stops the loop for diagnosis. NOT a repair.
    * ``fail``     — dispatch failed, the worktree/diff is unavailable, or the
      verdict could not be parsed. Fail-closed: review-not-done is never silently
      passed.

    Never raises into the loop.
    """
    record = worktree_record(target, task_id)
    if not record:
        return {"status": "fail", "reason": "no worktree record; pre-PR code review skipped"}
    worktree_path = Path(record["path"]).expanduser().resolve()
    if not worktree_path.exists() or worktree_path == target.resolve():
        return {"status": "fail", "reason": "worktree unavailable; cannot run the pre-PR code review"}

    # The review runs BEFORE commit/push, so the implementer's work may still be
    # uncommitted (and new test files untracked) in the worktree. Stage everything
    # first (non-destructive — the commit/push step re-stages anyway) so the diff
    # is complete, then diff the index against main. This shows the FULL task
    # change set the reviewer must judge, committed or not, tracked or new.
    run(["git", "add", "-A"], cwd=worktree_path, check=False)
    diff = run(["git", "diff", "--cached", "main"], cwd=worktree_path, check=False)
    if diff.returncode != 0:
        # Fall back to the committed-only diff (e.g. main is unrelated/missing).
        diff = run(["git", "diff", "main...HEAD"], cwd=worktree_path, check=False)
        if diff.returncode != 0:
            return {"status": "fail", "reason": "could not compute the task diff for review"}
    prompt = _CODE_REVIEW_PROMPT + (diff.stdout or "")

    try:
        exec_result = REVIEWER_ADAPTER.execute(worktree_path, prompt)
    except Exception:
        # Effectors fail closed and never raise into the loop (T1 style).
        return {"status": "fail", "reason": "reviewer dispatch raised; failing closed"}
    if exec_result.returncode != 0:
        return {"status": "fail", "reason": f"reviewer exited {exec_result.returncode}; failing closed"}

    verdict = parse_code_review_verdict(exec_result.stdout)
    if verdict is None:
        return {"status": "fail", "reason": "reviewer verdict could not be parsed; failing closed"}

    if verdict.get("verdict") == "blocking":
        # Record the blocking verdict as a check ledger for the audit trail, then
        # fail closed. No PR exists yet, so this cannot become a repair packet.
        findings = verdict.get("findings") or []
        ledger_id = append_ledger(
            target,
            goal_id=load_task(target, task_id)["goal_id"],
            task_id=task_id,
            ledger_type="check",
            summary=f"Pre-PR code-review verdict BLOCKING for {task_id} ({len(findings)} finding(s)); loop stops for diagnosis",
            evidence=["independent read-only reviewer (claude -p) — ADR 0011"],
        )
        task = load_task(target, task_id)
        task.setdefault("ledger_evidence", []).append(ledger_id)
        task["pre_pr_code_review"] = {"verdict": "blocking", "findings": findings, "ledger_id": ledger_id}
        _save_task(target, task)
        return {"status": "blocking", "reason": "independent pre-PR code review found blocking issues", "ledger_id": ledger_id}

    # Clean verdict: record the code-review check ledger and the PR-12 evidence.
    findings = verdict.get("findings") or []
    ledger_id = append_ledger(
        target,
        goal_id=load_task(target, task_id)["goal_id"],
        task_id=task_id,
        ledger_type="check",
        summary=f"Pre-PR code-review verdict CLEAN for {task_id} (independent read-only reviewer, code-review skill)",
        evidence=["independent read-only reviewer (claude -p) — ADR 0011"],
    )
    task = load_task(target, task_id)
    task.setdefault("ledger_evidence", []).append(ledger_id)
    task["pre_pr_code_review"] = {"verdict": "clean", "findings": findings, "ledger_id": ledger_id}
    _save_task(target, task)
    return {"status": "clean", "reason": "independent pre-PR code review passed", "ledger_id": ledger_id}


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
        # (a) Pre-PR code-review gate (ADR 0011). An INDEPENDENT read-only
        # reviewer judges the diff in a separate context BEFORE the PR exists.
        # A blocking verdict OR any dispatch/parse failure fails closed to
        # stop_blocked: a blocking pre-PR review cannot anchor a repair packet
        # (no PR exists yet by construction), so the loop stops for diagnosis
        # rather than dispatching a repair. Only a clean verdict proceeds.
        review = _run_pre_pr_code_review(target, task_id)
        result["code_review"] = review.get("status")
        if review.get("status") != "clean":
            result["action"] = "stop_blocked"
            result["reason"] = (
                f"pre-PR code review did not pass ({review.get('reason')}); "
                "no PR exists to anchor a repair — diagnose or re-dispatch"
            )
            return result
        # (b) Persist the implementer runtime's work to the branch before opening
        # the PR — the runner implements in the worktree but does not commit/push
        # (gap #1). Only open the PR once the branch actually has the pushed
        # implementation; otherwise `gh pr create` would raise on an empty/
        # unpushed branch and crash the loop, so fail closed to stop_blocked.
        impl = _commit_and_push_implementation(target, task_id)
        result["impl_commit"] = impl
        if "pushed to the task branch" not in impl:
            result["action"] = "stop_blocked"
            result["reason"] = f"implementation is not on the task branch ({impl}); diagnose or re-dispatch"
            return result
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
        # Auto-capture (proposal 3.3, source=cca_fail). The structured check
        # state — not free-text gh output — drove this rerun; the memory stores a
        # short claim and the rerun ledger reference only.
        from shiki_memory import capture_failure

        capture_failure(
            target,
            source_kind="cca_fail",
            area="cca",
            claim=f"CCA verdict failed for {task_id}; loop reran CCA (rerun {task['cca_rerun_count']}).",
            goal_id=goal_id,
            task_id=task_id,
            evidence_refs=[f".shiki/ledger/{ledger_id}.json"],
        )
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
    result = execute_action(target, goal_id, decision, repair_limit=repair_limit)
    # Auto-capture (proposal 3.3, source=loop_stop). Captured from the POST-result
    # action so that merge-failure / unblock-failure conversions to a stop are
    # recorded with their real stop kind. capture_failure is fail-open.
    if result.get("action") in STOP_ACTIONS:
        from shiki_memory import capture_failure

        capture_failure(
            target,
            source_kind="loop_stop",
            area="loop",
            claim=f"Goal loop stopped: {result.get('action')} for task {result.get('task_id')} ({result.get('reason')}).",
            goal_id=goal_id,
            task_id=result.get("task_id"),
            evidence_refs=[],
        )
    return result


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
