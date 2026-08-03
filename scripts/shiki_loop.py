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
from shiki_github import create_github_pr_for_task, github_env, parse_github_number, target_provider_config
from shiki_process import ShikiError, print_json, read_json, run, shiki_path, target_path, write_json
from shiki_runtime import dispatch_runner_task, session_lease_state
from shiki_runtime_adapters import REVIEWER_ADAPTER, get_runner_adapter, parse_code_review_verdict
from shiki_tasks import (
    append_ledger,
    allocate_worktree_record,
    cmd_goal_complete,
    load_goal,
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

# The five CCA verdict values (mirrors ``enforce_cca_verdict.VALID_VERDICTS`` and
# the ``cca-verdict.schema.json`` enum). When the ``CCA verdict`` required check is
# red, the loop resolves the verdict's VALUE from durable CI evidence and branches
# on it (DEFECT A): a red check proves the CCA did not say ``complete`` but not
# WHICH non-complete verdict it reached, and ``needs_guardian`` / ``blocked`` are
# HITL / dependency stops that must never be laundered into an auto-repair.
CCA_VERDICT_VALUES = frozenset(
    {"complete", "repair_required", "blocked", "needs_guardian", "insufficient_evidence"}
)
# Sentinel for a verdict the loop could not read (no artifact, unreadable,
# malformed, or a CCA that crashed before writing one). It is NOT a real verdict,
# so the gate fails closed on it — an unresolvable verdict may be a Guardian stop,
# and a CCA that crashed before judging is not repairable by an implementer either.
CCA_VERDICT_UNRESOLVED = "unresolved"
# The CCA workflow uploads `.shiki/gha` as the `shiki-cca-evidence` artifact; the
# verdict lands at this basename inside it.
CCA_VERDICT_BASENAME = "cca-verdict.json"

# Bound on how many times the loop re-dispatches a `running` task whose session
# lease proves the session is gone (free/absent). Once the recorded
# `dispatch_attempts` reaches this bound the loop stops for the operator instead
# of spinning: a session that keeps dying is a real failure, not a strand to
# silently retry forever.
MAX_DISPATCH_ATTEMPTS = 2

# Engine action names, in execution priority order for a goal pass.
ACTION_PRIORITY = (
    "mark_done",
    "create_closeout_pr",
    "merge",
    "rerun_cca",
    "dispatch_repair",
    "create_pr",
    "redispatch",
    "dispatch",
    "unblock",
)
STOP_ACTIONS = {"stop_guardian", "stop_blocked", "stop_lock_blocked"}
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
    lease_state: str | None = None,
    cca_verdict: str | None = None,
) -> dict[str, Any]:
    """Pure decision for one task. checks values: pass | fail | pending.

    ``lease_state`` is the session-lease probe result for a ``running`` task
    (``held`` / ``free`` / ``foreign_host`` / ``absent``; see
    ``shiki_session_lease.session_lease_state``). It is ``None`` when not probed,
    in which case a ``running`` task keeps waiting — the status field alone is
    never treated as proof a session is gone.

    ``cca_verdict`` is the CCA verdict VALUE the caller resolved read-only from
    durable CI evidence (one of ``CCA_VERDICT_VALUES`` or ``CCA_VERDICT_UNRESOLVED``)
    when the ``CCA verdict`` required check is red — otherwise ``None``. It is
    consulted ONLY in the impl-PR repair path, so a pure-engine caller that leaves
    it ``None`` keeps the prior check-conclusion behaviour (DEFECT A).
    """
    task_id = str(task.get("id"))
    status = str(task.get("status", ""))

    if status == "done":
        return {"action": "none", "task_id": task_id, "reason": "task is done"}
    if status in {"planned", "blocked"}:
        return {"action": "wait_dependencies", "task_id": task_id, "reason": f"task is {status}"}
    if status == "ready":
        return {"action": "dispatch", "task_id": task_id, "reason": "task is ready for the implementer runtime"}
    if status == "running":
        # The status field alone is not proof of a live session: a dispatched
        # session that died mid-work (the command-timeout strand) leaves the task
        # at `running` forever. Consult the OS lease instead. `held` (a live
        # holder) and `foreign_host` (a lease on another machine, never judged
        # here) keep waiting; only `free`/`absent` — a lease the kernel released
        # because the holder is gone — mean the session is dead.
        if lease_state in {"held", "foreign_host"}:
            which = (
                "the session lease is held by a live process"
                if lease_state == "held"
                else "the session lease is recorded on a foreign host and is not judged here"
            )
            return {"action": "wait_runner", "task_id": task_id, "reason": f"implementer session is running ({which})"}
        if lease_state is None:
            # Not probed (e.g. a pure-engine caller without a lease): fall back to
            # trusting the status field rather than re-dispatching on no evidence.
            return {"action": "wait_runner", "task_id": task_id, "reason": "implementer session is running"}
        # lease_state in {"free", "absent"}: no live session holds the lease.
        attempts = int(task.get("dispatch_attempts") or 0)
        if attempts >= MAX_DISPATCH_ATTEMPTS:
            return {
                "action": "stop_blocked",
                "task_id": task_id,
                "reason": (
                    f"task {task_id} has been re-dispatched {attempts} time(s) but its session lease still "
                    f"reads {lease_state} (no live session holds it); this exceeds the re-dispatch bound of "
                    f"{MAX_DISPATCH_ATTEMPTS}. The dispatched session keeps dying — diagnose before re-dispatching."
                ),
            }
        return {
            "action": "redispatch",
            "task_id": task_id,
            "reason": (
                f"task {task_id} is `running` but its session lease reads {lease_state} — no live "
                f"session; re-dispatching (attempt {attempts + 1}/{MAX_DISPATCH_ATTEMPTS})"
            ),
        }
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
        if task.get("closeout_pr"):
            # expected_pr was repointed to the closeout PR; its merge means
            # task=done + goal=complete + lock=released are now durable on main.
            return {"action": "mark_done", "task_id": task_id, "reason": "closeout PR merged; completion is on main"}
        # The IMPL PR merged. Do NOT mark done locally — that would complete the
        # goal only in the coordinator mirror (Gap B / ADR 0012). Drive a closeout
        # PR to push the terminal state to main instead.
        return {"action": "create_closeout_pr", "task_id": task_id, "reason": "impl PR merged; push completion to main via a closeout PR"}

    # Closeout PR phase: expected_pr points at the (unmerged) closeout PR. A
    # bookkeeping closeout has no implementation to repair, so the only recoverable
    # failure is the CCA same-head race (one rerun); everything else fails closed.
    if task.get("closeout_pr"):
        checks = checks or {}
        results = {name: checks.get(name, "pending") for name in required_checks}
        failed = sorted(name for name, value in results.items() if value == "fail")
        pending = sorted(name for name, value in results.items() if value == "pending")
        # The MergeGate policy (Guardian) gate is gated behind CCA, so it reports
        # skipped/missing/pending whenever CCA is red. Strip it before the CCA-race
        # decision EXACTLY like the impl-PR path (below); otherwise a lone CCA
        # failure against a pending/skipped policy gate looks like a multi-check
        # failure and drops to stop_blocked instead of the promised single rerun.
        repairable_failed = sorted(name for name in failed if name != POLICY_GATE)
        repairable_pending = sorted(name for name in pending if name != POLICY_GATE)
        policy_failed = POLICY_GATE in failed
        if repairable_failed:
            cca_completion_race = set(repairable_failed) == {CCA_VERDICT_CHECK}
            if cca_completion_race and repairable_pending:
                return {"action": "wait_checks", "task_id": task_id, "reason": f"closeout: CCA judged early; waiting for pending checks: {', '.join(pending)}"}
            if cca_completion_race and cca_reruns < MAX_CCA_RERUNS:
                return {"action": "rerun_cca", "task_id": task_id, "reason": "closeout: only the CCA verdict failed against green siblings; rerun after green"}
            return {"action": "stop_blocked", "task_id": task_id, "reason": f"closeout PR checks failed ({', '.join(repairable_failed)}); no auto-repair for a bookkeeping PR — diagnose"}
        if policy_failed:
            # The closeout's Guardian/policy gate said NO (or no authority yet);
            # never auto-repaired (CCA is green here). A recorded authority resolves.
            return {"action": "stop_guardian", "task_id": task_id, "reason": "closeout PR Guardian/policy gate failing with all other checks green; a recorded authority must resolve it"}
        if pending:
            return {"action": "wait_checks", "task_id": task_id, "reason": f"closeout PR checks pending: {', '.join(pending)}"}
        risk = str(task.get("risk_level") or "low")
        if risk in AUTO_MERGE_RISKS or POLICY_GATE in required_checks:
            return {"action": "merge", "task_id": task_id, "reason": "closeout PR checks green; merge to push completion to main"}
        return {"action": "stop_guardian", "task_id": task_id, "reason": f"closeout PR green but risk {risk} needs Guardian and no policy gate is configured"}

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
        # DEFECT A — verdict-aware repair gating. The `CCA verdict` check is red and
        # the same-head rerun race (above) is settled/exhausted. A red check does
        # NOT say which non-complete verdict the CCA reached, so gate on the VALUE
        # the caller resolved from durable CI evidence before ever dispatching a
        # repair. needs_guardian / blocked / complete / unresolvable are terminal
        # stops that never become an auto-repair and never consume a repair attempt
        # (the same principle that strips POLICY_GATE above); only repair_required /
        # insufficient_evidence proceed — and the CCA verdict check itself is then
        # dropped from the repair items, so an autonomous runner is never told to
        # "make the CCA verdict pass" with nothing concrete to fix.
        if CCA_VERDICT_CHECK in repairable_failed and cca_verdict is not None:
            verdict_stop = _cca_verdict_stop(task_id, cca_verdict)
            if verdict_stop is not None:
                return verdict_stop
            repairable_failed = [name for name in repairable_failed if name != CCA_VERDICT_CHECK]
        failure_desc = ", ".join(repairable_failed) if repairable_failed else f"CCA verdict {cca_verdict}"
        if repair_attempts >= repair_limit:
            return {
                "action": "stop_guardian",
                "task_id": task_id,
                "reason": f"required checks failed ({failure_desc}) and repair attempt limit reached",
            }
        return {
            "action": "dispatch_repair",
            "task_id": task_id,
            "reason": f"required checks failed: {failure_desc}",
            "failed_checks": repairable_failed,
            "cca_verdict": cca_verdict,
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


def _cca_verdict_stop(task_id: str, cca_verdict: str) -> dict[str, Any] | None:
    """Map a resolved CCA verdict VALUE to a terminal stop, or ``None`` to repair.

    Pure — the caller has already resolved the value read-only from durable CI
    evidence (DEFECT A). Reached only when the ``CCA verdict`` required check is
    red. ``needs_guardian`` and ``blocked`` are never auto-repaired and never
    consume a repair attempt; a ``complete`` verdict against a red check is a
    contradiction to reconcile; an unresolvable value fails closed. Only
    ``repair_required`` / ``insufficient_evidence`` return ``None`` so the caller
    dispatches a bounded repair. Each stop names the verdict with a DISTINCT reason.
    """
    if cca_verdict == "needs_guardian":
        return {
            "action": "stop_guardian",
            "task_id": task_id,
            "reason": (
                "CCA verdict is needs_guardian: a Guardian decision is required before progress; "
                "this is never auto-repaired and never consumes a repair attempt"
            ),
        }
    if cca_verdict == "blocked":
        return {
            "action": "stop_blocked",
            "task_id": task_id,
            "reason": (
                "CCA verdict is blocked: a dependency, lock, auth, or external blocker must clear "
                "before judgment; this is never auto-repaired and never consumes a repair attempt"
            ),
        }
    if cca_verdict == "complete":
        return {
            "action": "stop_blocked",
            "task_id": task_id,
            "reason": (
                "the CCA verdict check is red but the resolved verdict is `complete`: the check and "
                "the verdict disagree — reconcile the evidence before any repair, never repair on a contradiction"
            ),
        }
    if cca_verdict in {"repair_required", "insufficient_evidence"}:
        return None
    # CCA_VERDICT_UNRESOLVED or any value not in CCA_VERDICT_VALUES: fail closed.
    return {
        "action": "stop_blocked",
        "task_id": task_id,
        "reason": (
            "the CCA verdict could not be read from durable CI evidence "
            f"({cca_verdict!r}): failing closed and never auto-repairing — an unresolvable verdict "
            "may be a Guardian stop, and a CCA that crashed before judging is not implementer-repairable"
        ),
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


def _resolve_cca_verdict(target: Path, task: dict[str, Any], pr_state: dict[str, Any] | None) -> str:
    """Read the CCA verdict VALUE from durable CI evidence (read-only, never raises).

    DEFECT A: when the ``CCA verdict`` required check is red, the loop must not
    decide from the check conclusion alone. The CCA workflow uploads ``.shiki/gha``
    as the ``shiki-cca-evidence`` artifact, which contains ``cca-verdict.json``.
    Find the CCA workflow run for the PR head, download that artifact read-only, and
    return its ``verdict``. ANY failure — no run, no artifact, network/``gh`` down,
    unreadable or malformed JSON, missing or unknown verdict — returns
    ``CCA_VERDICT_UNRESOLVED`` so the caller fails closed. This is a read of durable
    evidence only; it never mutates GitHub and never crashes the loop.
    """
    import shutil
    import tempfile

    try:
        from shiki_evidence import CCA_EVIDENCE_ARTIFACT_NAME

        head_sha = (pr_state or {}).get("head_sha")
        runs = _gh(
            target,
            ["run", "list", "--workflow", "shiki-cca-completion.yml", "--limit", "20",
             "--json", "databaseId,conclusion,headSha,status"],
            check=False,
        )
        if runs.returncode != 0 or not (runs.stdout or "").strip():
            return CCA_VERDICT_UNRESOLVED
        entries = json.loads(runs.stdout)
        if not isinstance(entries, list):
            return CCA_VERDICT_UNRESOLVED
        run_id = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Bind the verdict to the PR head when the head is known: a stale run
            # for a superseded head must never masquerade as the current verdict.
            if head_sha and entry.get("headSha") != head_sha:
                continue
            run_id = entry.get("databaseId")
            if run_id is not None:
                break
        if run_id is None:
            return CCA_VERDICT_UNRESOLVED
        tmp = Path(tempfile.mkdtemp(prefix="shiki-cca-verdict-"))
        try:
            download = _gh(
                target,
                ["run", "download", str(run_id), "--name", CCA_EVIDENCE_ARTIFACT_NAME, "--dir", str(tmp)],
                check=False,
            )
            if download.returncode != 0:
                return CCA_VERDICT_UNRESOLVED
            verdict_path = tmp / CCA_VERDICT_BASENAME
            if not verdict_path.is_file():
                return CCA_VERDICT_UNRESOLVED
            data = json.loads(verdict_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return CCA_VERDICT_UNRESOLVED
            verdict = data.get("verdict")
            if isinstance(verdict, str) and verdict in CCA_VERDICT_VALUES:
                return verdict
            return CCA_VERDICT_UNRESOLVED
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        # Read-only resolution never crashes the loop: any error IS the
        # unresolvable case (fail closed), never a raised exception.
        return CCA_VERDICT_UNRESOLVED


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


# active_lock_conflicts renders each conflict as "Lock conflict: <lock> held by
# <owner>" (optionally "... overlaps <file>"). The loop groups these by owner so a
# single owner holding several overlapping locks reads as one message.
_LOCK_CONFLICT_PREFIX = "Lock conflict: "
_LOCK_CONFLICT_HOLDER = " held by "


def _split_lock_conflict(blocker: str) -> tuple[str | None, str]:
    """Parse (overlapping lock, owner task) from an ``active_lock_conflicts``
    message. An unparseable message yields ``(None, <verbatim message>)`` so it is
    surfaced under its own key rather than silently dropped."""
    body = blocker
    if body.startswith(_LOCK_CONFLICT_PREFIX):
        body = body[len(_LOCK_CONFLICT_PREFIX):]
    holder = body.find(_LOCK_CONFLICT_HOLDER)
    if holder == -1:
        return None, blocker
    held = body[:holder]
    owner = body[holder + len(_LOCK_CONFLICT_HOLDER):]
    overlaps = owner.find(" overlaps ")
    if overlaps != -1:
        owner = owner[:overlaps]
    return held, owner


def _group_lock_conflicts(blockers: list[str]) -> list[dict[str, Any]]:
    """Collapse ``active_lock_conflicts`` output to one record per owning task.

    Several overlapping locks held by the SAME owner collapse to a single record
    (one message per owner, not one per held-lock x owner pair). First-seen order
    is preserved for both owners and their locks."""
    by_owner: dict[str, list[str]] = {}
    order: list[str] = []
    for blocker in blockers:
        held, owner = _split_lock_conflict(blocker)
        if owner not in by_owner:
            by_owner[owner] = []
            order.append(owner)
        if held and held not in by_owner[owner]:
            by_owner[owner].append(held)
    return [{"owner_task": owner, "locks": by_owner[owner]} for owner in order]


def _lock_blocked_stop(lock_blocked: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn recorded lock blocks into a distinct, NAMED stop.

    Serialization behind another task's active lock is not a deadlock, so it gets
    its own action (``stop_lock_blocked``) — distinguishable in loop output from a
    Guardian stop and from a blocked-evidence ``stop_blocked`` — and a reason that
    names the owning task and the overlapping locks."""
    first = lock_blocked[0]
    task_id = first["task_id"]
    owners = "; ".join(
        f"{conflict['owner_task']} holds {', '.join(conflict['locks'])}"
        for conflict in first["conflicts"]
    )
    scope = (
        f" ({len(lock_blocked)} tasks are blocked on active locks)"
        if len(lock_blocked) > 1
        else ""
    )
    return {
        "action": "stop_lock_blocked",
        "task_id": task_id,
        "reason": (
            f"task {task_id} is blocked on an active lock: {owners}{scope} — this is "
            "lock serialization, not a deadlock; the task runs once the owning task "
            "releases its lock"
        ),
        "lock_conflicts": lock_blocked,
    }


def _unblock_ready_tasks(target: Path, goal_id: str) -> tuple[list[str], list[dict[str, Any]]]:
    unblocked: list[str] = []
    lock_blocked: list[dict[str, Any]] = []
    for task in tasks_for_goal(target, goal_id):
        if task.get("status") != "planned":
            continue
        dependencies = [load_task(target, dep) for dep in task.get("dependencies", [])]
        if any(dep.get("status") != "done" for dep in dependencies):
            continue
        ok, blockers, _ = try_acquire_locks(target, task["id"])
        if not ok:
            # A dependency-complete task whose locks overlap another task's ACTIVE
            # lock is SERIALIZED behind that owner, not deadlocked. Record the
            # conflict (owner + overlapping locks, deduped per owner) so the loop
            # NAMES the stop instead of silently skipping — a silent skip read as
            # the PR #179 deadlock stall.
            lock_blocked.append({"task_id": task["id"], "conflicts": _group_lock_conflicts(blockers)})
            continue
        if worktree_record(target, task["id"]) is None:
            allocate_worktree_record(target, task["id"])
        # Regenerate unconditionally: the handoff embeds the live Distilled
        # Rules section, so a stale cached handoff must never be reused (§3.7).
        write_task_handoff(target, task["id"])
        unblocked.append(task["id"])
    return unblocked, lock_blocked


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


def _dispatch_repair(
    target: Path,
    task: dict[str, Any],
    failed_checks: list[str],
    attempt: int,
    *,
    cca_verdict: str | None = None,
) -> dict[str, Any]:
    from shiki_tasks import LOOP_OWNS_DELIVERY_PROHIBITION, cmd_handoff_repair, create_repair_packet

    pr = task.get("expected_pr")
    if not pr:
        raise ShikiError(f"task {task['id']} has no PR; repair packets require an existing PR")
    # The `CCA verdict` check itself is never a repair item (DEFECT A): decide_task_
    # action strips it from failed_checks before dispatching, so a repair packet
    # never says "make the CCA verdict pass" with nothing concrete to fix. When the
    # verdict was repair_required / insufficient_evidence and CCA was the sole red
    # check, name the verdict's blocking findings instead of the meta check.
    if failed_checks:
        failing_items = [f"required check failed: {name}" for name in failed_checks]
    elif cca_verdict in {"repair_required", "insufficient_evidence"}:
        failing_items = [
            f"CCA verdict is {cca_verdict}; address the CCA's blocking checklist and "
            "acceptance findings recorded on the PR"
        ]
    else:
        failing_items = ["task is repair-needed"]
    repair_id, _, _ = create_repair_packet(
        target,
        task_id=task["id"],
        pr=int(pr),
        attempt=attempt,
        failing_items=failing_items,
        failing_acceptance_criteria=[],
        minimal_changes=["Fix the failing required checks without broadening scope."],
        # The commit/push prohibition is the SAME line the task handoff carries, so
        # the runner's delivery contract is unambiguous in both directions: a repair
        # is edited in the worktree and delivered by the loop, never self-pushed.
        prohibited_changes=[
            "Do not modify files outside the task locks.",
            "Do not weaken checks or validators.",
            LOOP_OWNS_DELIVERY_PROHIBITION,
        ],
        required_skill="diagnose",
        verification_commands=["python3 scripts/validate_shiki.py"],
        # Loop-owned delivery: the runner edits the worktree; the goal loop commits
        # and PUSHES the fix to the task branch so the required checks re-run against
        # the updated PR head. States the push explicitly, attributed to the loop.
        evidence_required=[
            "Make the fix in the worktree only; the goal loop commits and pushes it "
            "to the task branch so the required checks re-run against the updated PR head.",
        ],
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


def _pr_branch_head(target: Path, task_id: str) -> str | None:
    """The task branch's pushed head (the PR head) via the origin tracking ref.

    Reads ``refs/remotes/origin/<expected_branch>`` from the task worktree — the
    ref ``git push`` updates on a successful push. Returns the SHA, or ``None``
    when it cannot be resolved (no worktree, no branch, branch not yet pushed).
    The ``dispatch_repair`` branch snapshots this before and after delivery so a
    push that reports success but did NOT advance the head (a repair that produced
    no change: ``git push`` prints "Everything up-to-date") is caught and fails
    closed, rather than silently consuming a repair attempt against an unmoved PR.
    """
    record = worktree_record(target, task_id)
    if not record:
        return None
    worktree_path = Path(record["path"]).expanduser().resolve()
    if not worktree_path.exists():
        return None
    branch = str(load_task(target, task_id).get("expected_branch") or "")
    if not branch:
        return None
    result = run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=worktree_path,
        check=False,
    )
    head = (result.stdout or "").strip()
    return head or None


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
    # A locally-started goal (created by `shiki run`, never committed to main) has
    # its goal / DAG / lock only in the coordinator checkout. The task branch is
    # cut from main, so it lacks them and validate_shiki fails closed on the PR
    # HEAD with "goal_id <G> has no matching goal file" (the live #140 T5 failure).
    # Carry the goal's own goal file and this task's lock — both are goal-id /
    # task-id specific, so they stay inside MergeGate's per-file goal/lock scope
    # (mergegate_check.py:1127-1137). They must ALSO be covered by the task's
    # declared `locks` or MergeGate's separate files_outside_locks gate (:1357)
    # blocks them; loop-executed tasks declare `path:.shiki/**` (the synced
    # tasks/ledger files already rely on the same coverage). `add` no-ops on a
    # missing file and is idempotent when these already rode in from main.
    goal_id = str(task.get("goal_id") or "")
    if goal_id:
        add(f".shiki/goals/{goal_id}.json")
        # The DAG lists EVERY task node of the goal. Syncing it onto a branch that
        # carries only THIS task's file would trip validate_dag ("node <sibling>
        # has no matching task file") for a multi-task goal. Sync the DAG only when
        # its node set is covered by the task file(s) on the branch — i.e. a
        # single-task goal whose one node is this task. For registered multi-task
        # goals the DAG already rides in from main.
        try:
            dag = read_json(target / ".shiki" / "dag" / f"{goal_id}.json")
        except Exception:
            dag = None
        if isinstance(dag, dict):
            nodes = {str(node) for node in (dag.get("nodes") or [])}
            if nodes and nodes <= {task_id}:
                add(f".shiki/dag/{goal_id}.json")
    add(f".shiki/locks/{task_id}.json")
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
        # Ownership bound: expand a ledger's referenced .shiki paths onto the
        # branch ONLY when the ledger is unambiguously THIS task's own — its
        # goal_id AND task_id both equal the task's. The ledger FILE is always
        # carried (it is listed in ledger_evidence); only the transitive
        # expansion of the paths its evidence references is bounded. This is
        # deliberately STRICTER than mergegate_check.ledger_entry_allowed_for_task,
        # which also accepts a goal-scoped ledger with an empty task_id: reusing
        # that looser bound would let a PR-authored task file pad ledger_evidence
        # with a foreign or goal-level ledger and inherit the .shiki paths its
        # evidence references. The completion ledger's own goal-scoped evidence is
        # synced by _sync_goal_complete_mirror on the closeout path, not here.
        if str(entry.get("goal_id") or "") != goal_id:
            continue
        if str(entry.get("task_id") or "") != task_id:
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
def task_test_command(task: dict[str, Any]) -> str:
    """The structured command the loop exec's for the task's TDD gate.

    Reads the task's ``test_command`` field, falling back to the safe unittest
    discover default when it is absent or blank. ``acceptance_checks`` is
    free-form prose+commands and is deliberately NOT consulted here — it must
    never be handed to a shell (ADR 0011: a deterministic observable command,
    not narrative, is what the independent verifier runs).
    """
    # Lazy import keeps the shiki_loop <-> shiki_tasks edge one-directional.
    from shiki_tasks import DEFAULT_TEST_COMMAND

    command = task.get("test_command")
    if isinstance(command, str) and command.strip():
        return command
    return DEFAULT_TEST_COMMAND


def _run_task_tests_in_worktree(
    target: Path, task_id: str
) -> tuple[bool, str | None, str | None, str]:
    """Loop-observed TDD gate (ADR 0011): run the task's tests in its worktree.

    The loop — an independent verifier, not the implementer — runs the task's
    structured ``test_command`` in the registered worktree and records the run
    as durable evidence, mirroring ``record_runner_result``'s EXEC pattern:
    write ``.shiki/runner/EXEC-*.json`` with the captured output, then a
    ``type:"check"`` ledger naming skill ``tdd`` whose evidence points at that
    EXEC record, and append the ledger id to ``task.ledger_evidence``.

    Returns ``(ok, ledger_id, exec_rel, summary)``. ``ok`` is True only when the
    command exited 0 — a green run the loop OBSERVED, never the implementer's
    self-attestation. Fail-closed: any inability to observe a green run (no
    worktree, missing path, exec error) returns ``ok=False`` with
    ``ledger_id``/``exec_rel`` None. This effector never raises into the loop.
    """
    try:
        # Lazy import keeps shiki_loop's edges one-directional: shiki_runtime
        # imports shiki_github -> shiki_tasks, so importing it at module load
        # would re-enter the shiki_loop <-> shiki_github <-> shiki_tasks cycle.
        import subprocess

        from shiki_process import shiki_path, utc_now, write_json
        from shiki_tasks import append_ledger, next_control_id

        record = worktree_record(target, task_id)
        if not record:
            return False, None, None, "no worktree record; TDD gate cannot observe the tests"
        worktree_path = Path(record["path"]).expanduser().resolve()
        if not worktree_path.exists() or worktree_path == target.resolve():
            return False, None, None, "worktree unavailable; TDD gate cannot observe the tests"
        task = load_task(target, task_id)
        command = task_test_command(task)

        process = subprocess.run(
            command,
            cwd=str(worktree_path),
            shell=True,
            text=True,
            capture_output=True,
            check=False,
        )
        # Mirror record_runner_result's EXEC pattern (shiki_runtime): an
        # EXEC-*.json record holds the raw command + captured stdout/stderr; the
        # type:check ledger names skill tdd and references that EXEC file, so the
        # run is durable, branch-syncable evidence. The EXEC `command` stays the
        # exact command run — the "tdd" naming lives in the ledger summary.
        record_id = next_control_id(target, "EXEC")
        record_file = shiki_path(target, "runner", f"{record_id}.json")
        write_json(
            record_file,
            {
                "id": record_id,
                "task_id": task["id"],
                "goal_id": task["goal_id"],
                "command": command,
                "returncode": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
                "created_at": utc_now(),
            },
        )
        exec_rel = str(record_file.relative_to(target))
        ledger_id = append_ledger(
            target,
            goal_id=task["goal_id"],
            task_id=task["id"],
            ledger_type="check",
            summary=(
                f"Loop-observed TDD gate (skill: tdd) exited {process.returncode} "
                f"for {task['id']}: {command}"
            ),
            evidence=[exec_rel],
        )
        task = load_task(target, task_id)
        task.setdefault("ledger_evidence", []).append(ledger_id)
        _save_task(target, task)
        ok = process.returncode == 0
        summary = (
            f"loop-observed TDD gate green ({command})"
            if ok
            else f"loop-observed TDD gate RED (exit {process.returncode}: {command})"
        )
        return ok, ledger_id, exec_rel, summary
    except Exception as error:  # never raise into the loop
        return False, None, None, f"TDD gate could not run: {error}"


def _closeout_pr_body(task: dict[str, Any], goal_id: str, *, completes_goal: bool) -> str:
    """PR body for an autonomous closeout PR (ADR 0012). Must contain the literal
    Scope/Acceptance/Evidence/MergeGate headings the MergeGate metadata check
    requires plus the task and goal ids."""
    goal_line = "goal `complete` (scorecard)" if completes_goal else "goal stays active (not the last task)"
    accept_goal = ", goal `complete` with scorecard" if completes_goal else ""
    return (
        f"## Shiki\n"
        f"- Task: `{task['id']}`\n"
        f"- Goal: `{goal_id}`\n"
        f"- Risk: `{task.get('risk_level', 'low')}`\n\n"
        f"## Scope\n"
        f"Autonomous loop closeout (ADR 0012): the implementation PR for this task "
        f"already merged, but the loop's `mark_done` / `goal_complete` write only the "
        f"local mirror. This PR pushes that completion to main — task `done`, lock "
        f"`released`, and {goal_line} — so completion is durable on `main`, not "
        f"local-only.\n\n"
        f"## Non-goals\n- No code change (the implementation already merged).\n\n"
        f"## Acceptance\n- Task `done`, lock `released`{accept_goal}; `validate_shiki` passes "
        f"(the goal-completion coupling is satisfied on this HEAD).\n\n"
        f"## Pre-PR code review\n- No code changes in this closeout PR; the "
        f"implementation was reviewed in the task's impl PR (the loop's pre-PR "
        f"code-review gate, ADR 0011) before that PR merged. This PR carries only "
        f"`.shiki` completion bookkeeping.\n\n"
        f"## Evidence\n- Opened autonomously by the goal loop after the impl PR merged; "
        f"the self-reference ledger records `/pull/<this PR>`.\n\n"
        f"## MergeGate\n- Normal-mode closeout (no special label); risk inherits the task "
        f"(low/medium auto-merges). The loop-task `path:.shiki/**` lock covers every staged "
        f"`.shiki` file.\n\n"
        f"\U0001f916 Generated by the Shiki goal loop (ADR 0012)\n"
    )


def _create_closeout_pr(target: Path, goal_id: str, task_id: str) -> dict[str, Any]:
    """Open a normal-mode closeout PR pushing task=done + lock=released +
    (goal=complete iff this task completes the goal) to main — Gap B / ADR 0012.

    The loop's `mark_done`/`goal_complete` otherwise mutate only the coordinator
    mirror, so completion never reaches GitHub (the source of truth). This builds
    the terminal state in a FRESH worktree cut from ``origin/main`` (the impl
    worktree's branch already merged), opens the PR, and in the coordinator records
    ``task.closeout_pr`` and repoints ``expected_pr`` so the existing snapshot/merge
    machinery drives the closeout PR to auto-merge. Fails closed to ``stop_blocked``
    (never raises into the loop)."""
    import contextlib
    import io
    import tempfile

    branch = f"shiki/{task_id.lower()}-closeout"
    worktree = None
    try:
        # Re-entrancy: this is reached only when closeout_pr is unset, so a PR for
        # this deterministic branch means a PRIOR run was interrupted mid-effector
        # (before recording closeout_pr) and its HEAD may be incomplete (missing the
        # /pull ledger or the repointed expected_pr). Don't silently adopt a possibly
        # broken PR (it would block MergeGate forever with no repair path); stop for
        # a recorded operator reconcile instead.
        listing = _gh(target, ["pr", "list", "--head", branch, "--state", "open",
                               "--json", "number", "--limit", "1"], check=False)
        if listing.returncode == 0 and listing.stdout.strip():
            try:
                rows = json.loads(listing.stdout)
            except (json.JSONDecodeError, ValueError):
                rows = []
            if rows:
                num = int(rows[0]["number"])
                return {"action": "stop_blocked", "task_id": task_id,
                        "reason": (f"a closeout PR #{num} already exists for {branch} from an interrupted run; "
                                   f"verify it carries expected_pr={num} + a /pull/{num} ledger and set task.closeout_pr={num}, "
                                   "or close it and re-run")}

        run(["git", "fetch", "origin", "main"], cwd=target, check=False)
        worktree = Path(tempfile.mkdtemp(prefix="shiki-closeout-"))
        add = run(["git", "worktree", "add", "--force", "-B", branch, str(worktree), "origin/main"], cwd=target, check=False)
        if add.returncode != 0:
            return {"action": "stop_blocked", "task_id": task_id, "reason": f"closeout worktree add failed: {(add.stderr or '').strip()[-200:]}"}
        # Build the terminal state in the worktree (cut from main: task=review).
        wt_task = load_task(worktree, task_id)
        wt_task["status"] = "done"
        wt_task["expected_branch"] = branch
        _save_task(worktree, wt_task)
        _release_lock(worktree, task_id)

        # completes_goal must reflect the WHOLE goal, judged against the closeout
        # worktree (cut from origin/main) — not just whatever rode onto main so far.
        # The old `all(status == done for tasks_for_goal(worktree))` is vacuously
        # true for a multi-task goal whose first task merged but whose siblings'
        # files are not yet on main (the worktree carries only that one task), so it
        # wrongly completed the goal on main after a single task. Require EVERY task
        # id the COORDINATOR knows for this goal to be present in the worktree AND
        # done there: a single-task goal (its one task set done above) stays true; a
        # multi-task goal completes only once its last task's closeout finds all
        # siblings already done on main.
        coordinator_task_ids = [str(t.get("id")) for t in tasks_for_goal(target, goal_id)]
        worktree_status = {
            str(t.get("id")): str(t.get("status")) for t in tasks_for_goal(worktree, goal_id)
        }
        completes_goal = bool(coordinator_task_ids) and all(
            worktree_status.get(tid) == "done" for tid in coordinator_task_ids
        )
        if completes_goal:
            # Complete the goal IN THE WORKTREE so the scorecard report + goal=complete
            # land on the HEAD (validate_shiki's coupling requires it there). Suppress
            # cmd_goal_complete's stdout so it never pollutes the loop's JSON result.
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_goal_complete(argparse.Namespace(
                    target=str(worktree), goal_id=goal_id,
                    summary="Autonomous loop closeout: push goal completion to main (ADR 0012)."))
            # cmd_goal_complete records the completion ledger on the GOAL only; the
            # task PR's MergeGate requires every PR-changed ledger to be in the
            # TASK's ledger_evidence, so mirror the completion ledger across.
            wt_goal = load_goal(worktree, goal_id) or {}
            wt_task = load_task(worktree, task_id)
            for lid in wt_goal.get("ledger_evidence", []):
                lpath = worktree / ".shiki" / "ledger" / f"{lid}.json"
                if not lpath.is_file():
                    continue
                try:
                    led = read_json(lpath)
                except Exception:
                    continue
                if led.get("type") == "completion" and lid not in (wt_task.get("ledger_evidence") or []):
                    wt_task.setdefault("ledger_evidence", []).append(lid)
            _save_task(worktree, wt_task)

        run(["git", "add", "-A"], cwd=worktree, check=False)
        commit = run(["git", "commit", "-m", f"shiki: closeout {task_id} — push completion to main (goal loop, ADR 0012)"], cwd=worktree, check=False)
        if commit.returncode != 0:
            return {"action": "stop_blocked", "task_id": task_id, "reason": "closeout produced no diff (already reconciled on main?)"}
        push = run(["git", "push", "-u", "origin", branch], cwd=worktree, check=False)
        if push.returncode != 0:
            return {"action": "stop_blocked", "task_id": task_id, "reason": f"closeout branch push failed: {(push.stderr or '').strip()[-200:]}"}

        task = load_task(worktree, task_id)
        create = _gh(
            target,
            ["pr", "create", "--base", "main", "--head", branch,
             "--title", f"Closeout {task_id}: push goal completion to main (ADR 0012)",
             "--body", _closeout_pr_body(task, goal_id, completes_goal=completes_goal)],
            check=False,
        )
        url = (create.stdout or "").strip().splitlines()[-1] if create.stdout.strip() else ""
        try:
            num = parse_github_number(url, "pull")
        except Exception:
            num = None
        if not num:
            return {"action": "stop_blocked", "task_id": task_id, "reason": f"closeout PR create failed: {(create.stderr or create.stdout or '').strip()[-200:]}"}

        # Self-reference ledger (/pull/N) — MergeGate requires the ledger evidence to
        # name this PR. Append it on the branch and push (a second commit).
        pull_ledger = append_ledger(
            worktree, goal_id=goal_id, task_id=task_id, ledger_type="lock",
            summary=(f"Autonomous closeout PR #{num} (/pull/{num}): task done + lock released"
                     + (" + goal complete (scorecard)" if completes_goal else "")
                     + " pushed to main by the goal loop (ADR 0012)."),
            evidence=[f".shiki/tasks/{task_id}.json", f".shiki/locks/{task_id}.json"],
            links=[url])
        wt_task = load_task(worktree, task_id)
        # CRITICAL: MergeGate matches the branch HEAD's task.expected_pr to the PR
        # number (mergegate_check.py ~1334). The branch was cut from main where
        # expected_pr is the IMPL PR; repoint it to the closeout PR here, or the
        # metadata check fails and the closeout never merges.
        wt_task["expected_pr"] = num
        if pull_ledger not in wt_task.get("ledger_evidence", []):
            wt_task.setdefault("ledger_evidence", []).append(pull_ledger)
        _save_task(worktree, wt_task)
        run(["git", "add", "-A"], cwd=worktree, check=False)
        commit2 = run(["git", "commit", "-m", f"shiki: link closeout PR #{num} (goal loop)"], cwd=worktree, check=False)
        if commit2.returncode != 0:
            return {"action": "stop_blocked", "task_id": task_id, "reason": f"closeout PR #{num} opened but its /pull-ledger commit produced no diff; reconcile the branch manually"}
        push2 = run(["git", "push"], cwd=worktree, check=False)
        if push2.returncode != 0:
            return {"action": "stop_blocked", "task_id": task_id, "reason": f"closeout PR #{num} opened but pushing its /pull ledger + expected_pr failed: {(push2.stderr or '').strip()[-160:]}; re-run to reconcile"}

        # Coordinator: record the closeout PR and repoint expected_pr so the loop's
        # snapshot/merge machinery drives the closeout PR (the impl PR is done).
        # Set closeout_pr LAST: it is the re-entrancy anchor, so it must only be
        # recorded once the closeout PR HEAD is complete (ledger + expected_pr).
        task = load_task(target, task_id)
        task["closeout_pr"] = num
        task["expected_pr"] = num
        task["expected_branch"] = branch
        # The closeout PR is a FRESH PR head with its own CCA rerun budget: reset the
        # impl PR's rerun tally so the closeout's CCA same-head race can still rerun.
        # A task that exhausted its 2 reruns on the impl PR would otherwise fail
        # closed to stop_blocked on the closeout PR's first CCA race (decide_task_
        # action reads cca_rerun_count from this coordinator task file).
        task["cca_rerun_count"] = 0
        _save_task(target, task)
        return {"action": "create_closeout_pr", "task_id": task_id, "closeout_pr": num, "completes_goal": completes_goal, "url": url}
    except Exception as error:  # noqa: BLE001 — the effector must NEVER raise into the loop
        return {"action": "stop_blocked", "task_id": task_id, "reason": f"closeout effector error: {str(error)[:180]}"}
    finally:
        if worktree is not None:
            run(["git", "worktree", "remove", "--force", str(worktree)], cwd=target, check=False)


def _loop_own_ledger_ids(target: Path, task: dict[str, Any]) -> list[str]:
    """This task's loop-authored merge + mark_done ledger ids.

    The ``merge`` effector appends a ``mergegate`` ledger and ``_mark_done`` appends
    a ``check`` ledger whose summary starts ``Goal loop marked``; both are written to
    the coordinator task only and are never pushed to main. The ``goal_complete``
    origin/main sync therefore reverts them off the task file, so they are captured
    here (before the sync) to be re-appended after it. Order preserved; unreadable or
    absent ledgers are skipped.
    """
    own: list[str] = []
    for ledger_id in task.get("ledger_evidence") or []:
        path = target / ".shiki" / "ledger" / f"{ledger_id}.json"
        if not path.is_file():
            continue
        try:
            entry = read_json(path)
        except Exception:
            continue
        ledger_type = entry.get("type")
        summary = str(entry.get("summary") or "")
        if ledger_type == "mergegate" or (ledger_type == "check" and summary.startswith("Goal loop marked")):
            own.append(ledger_id)
    return own


def _sync_goal_complete_mirror(target: Path, goal_id: str) -> dict[str, Any]:
    """Sync ONLY the completing goal's ``.shiki`` paths from ``origin/main``.

    After the closeout PR merged, main is authoritative for this goal (goal=complete
    + scorecard + task=done + lock=released) and the coordinator mirror must catch
    up — but NOT with a whole-tree ``git checkout origin/main -- .shiki``. That
    reverts every unrelated in-flight goal's files to main's stale version and drops
    the loop's own merge/mark_done ledger ids (coordinator-only, never on main) off
    the completing task. This effector instead:

      * captures the loop's own merge/mark_done ledger ids per task (pre-sync);
      * checks out only this goal's own id-named files (goal, dag, tasks, locks,
        worktrees) and the ledgers/reports those main-side files reference —
        restricted to paths that actually exist on ``origin/main`` so the checkout
        cannot error on a not-yet-pushed candidate;
      * re-appends the captured merge/mark_done ids the checkout reverted;
      * unstages every synced path, so the coordinator carries them exactly as the
        loop normally leaves ``.shiki`` mutations — in the working tree, unstaged.

    Fail-open: on any sync failure the goal is still reflected ``complete`` locally
    so the run reports the durable truth. Never raises into the loop.
    """
    run(["git", "fetch", "origin", "main"], cwd=target, check=False)

    coordinator_tasks = tasks_for_goal(target, goal_id)
    task_ids = [str(t.get("id")) for t in coordinator_tasks]
    own_ledgers = {str(t.get("id")): _loop_own_ledger_ids(target, t) for t in coordinator_tasks}

    # One listing of every .shiki path on origin/main: `git checkout <ref> -- <spec>`
    # errors if ANY pathspec is absent from the ref, so candidates are filtered
    # against this before every checkout.
    ls = run(["git", "ls-tree", "-r", "--name-only", "origin/main", "--", ".shiki"], cwd=target, check=False)
    present = set((ls.stdout or "").splitlines()) if ls.returncode == 0 else set()

    synced_paths: list[str] = []
    checkout_ok = True

    def _sync(paths: list[str]) -> None:
        nonlocal checkout_ok
        pending = [p for p in paths if p in present and p not in synced_paths]
        if not pending:
            return
        res = run(["git", "checkout", "origin/main", "--", *pending], cwd=target, check=False)
        if res.returncode == 0:
            synced_paths.extend(pending)
        else:
            checkout_ok = False

    # Pass 1: this goal's id-named files (deterministic filenames).
    id_named = [f".shiki/goals/{goal_id}.json", f".shiki/dag/{goal_id}.json"]
    for task_id in task_ids:
        id_named += [
            f".shiki/tasks/{task_id}.json",
            f".shiki/locks/{task_id}.json",
            f".shiki/worktrees/{task_id}.json",
        ]
    _sync(id_named)

    # Pass 2: ledgers referenced by the now-synced (main-side) goal + task files.
    # main's new completion/pull ledgers are absent from the coordinator's pre-sync
    # references, so read them from the FRESHLY checked-out files.
    ledger_refs: list[str] = []

    def _collect_ledgers(entity: dict[str, Any] | None) -> None:
        if not isinstance(entity, dict):
            return
        for ledger_id in entity.get("ledger_evidence") or []:
            rel = f".shiki/ledger/{ledger_id}.json"
            if rel not in ledger_refs:
                ledger_refs.append(rel)

    try:
        _collect_ledgers(load_goal(target, goal_id))
    except ShikiError:
        pass
    for task_id in task_ids:
        try:
            _collect_ledgers(load_task(target, task_id))
        except ShikiError:
            continue
    _sync(ledger_refs)

    # Pass 3: reports (and any other .shiki evidence) those ledgers reference, read
    # from the now-local ledger files.
    report_refs: list[str] = []
    for rel in ledger_refs:
        path = target / rel
        if not path.is_file():
            continue
        try:
            entry = read_json(path)
        except Exception:
            continue
        for ref in entry.get("evidence") or []:
            ref = str(ref)
            if ref.startswith(".shiki/") and ref not in ledger_refs and ref not in report_refs:
                report_refs.append(ref)
    _sync(report_refs)

    # Re-append the loop's own merge/mark_done ledger ids the Pass-1 checkout
    # reverted off the task files (they are not on main; the ledger files stay on
    # disk locally, so the references are never dangling).
    for task_id, ids in own_ledgers.items():
        if not ids:
            continue
        try:
            task_obj = load_task(target, task_id)
        except ShikiError:
            continue
        evidence = task_obj.setdefault("ledger_evidence", [])
        added = [ledger_id for ledger_id in ids if ledger_id not in evidence]
        if added:
            evidence.extend(added)
            write_json(shiki_path(target, "tasks", f"{task_id}.json"), task_obj)

    # Leave no reverted path staged: the coordinator carries .shiki mutations
    # unstaged (the loop writes JSON directly), so unstage what the checkout staged.
    if synced_paths:
        run(["git", "reset", "-q", "--", *synced_paths], cwd=target, check=False)

    mirror_synced = checkout_ok and bool(synced_paths)
    if not mirror_synced:
        # Fail open: reflect completion locally so the run reports the durable truth
        # (the closeout merged; main is authoritative).
        try:
            goal = load_goal(target, goal_id)
        except ShikiError:
            goal = None
        if goal and goal.get("status") != "complete":
            goal["status"] = "complete"
            write_json(shiki_path(target, "goals", f"{goal_id}.json"), goal)
    return {"mirror_synced": mirror_synced}


def execute_action(target: Path, goal_id: str, decision: dict[str, Any], *, repair_limit: int) -> dict[str, Any]:
    action = decision["action"]
    task_id = decision.get("task_id")
    result: dict[str, Any] = {"action": action, "task_id": task_id, "reason": decision.get("reason")}

    if action in WAIT_ACTIONS or action in STOP_ACTIONS or action == "goal_complete":
        if action == "goal_complete":
            # The completing task's closeout PR already pushed goal=complete (with
            # the scorecard report + completion ledger) to main (ADR 0012). Sync the
            # coordinator mirror to main's authoritative state for THIS goal only —
            # never a whole-tree checkout, which would revert unrelated in-flight
            # goals' files and drop the loop's own merge/mark_done ledger ids off the
            # completing task. Do NOT re-run cmd_goal_complete (duplicate scorecard).
            result.update(_sync_goal_complete_mirror(target, goal_id))
            result["goal_status"] = "complete"
        return result

    task = load_task(target, task_id)
    if action == "dispatch":
        result["returncode"] = _dispatch(target, task)
    elif action == "redispatch":
        # A `running` task whose session lease is gone (free/absent): the session
        # died mid-work and left the task stranded. Reset it to `ready`, record
        # the attempt on the task (a monotonically increasing `dispatch_attempts`
        # the decision reads to enforce the bound), and dispatch a fresh session.
        task["status"] = "ready"
        task["dispatch_attempts"] = int(task.get("dispatch_attempts") or 0) + 1
        _save_task(target, task)
        result["dispatch_attempts"] = task["dispatch_attempts"]
        result["returncode"] = _dispatch(target, load_task(target, task_id))
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
        # Loop-owned TDD gate FIRST (ADR 0011): the loop — an independent
        # verifier, not the implementer — runs the task's tests in the worktree
        # and records a type:check ledger naming skill tdd (EXEC evidence ref)
        # BEFORE any PR exists. Fail-closed: a RED run does NOT open the PR. We
        # stop_blocked rather than dispatch_repair because repair packets require
        # an existing PR (dispatch_repair is PR-gated) — there is none yet.
        tdd_ok, tdd_ledger_id, tdd_exec, tdd_summary = _run_task_tests_in_worktree(target, task_id)
        result["tdd_observed"] = tdd_summary
        result["tdd_ledger_id"] = tdd_ledger_id
        result["tdd_exec"] = tdd_exec
        if not tdd_ok:
            result["action"] = "stop_blocked"
            result["reason"] = f"loop-observed TDD gate did not pass ({tdd_summary}); no PR opened"
            return result
        # Persist the implementer runtime's work to the branch before opening the
        # PR — the runner implements in the worktree but does not commit/push
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
        # DEFECT B — carry the rerun ledger (and the transitive evidence it and the
        # rest of ledger_evidence cite) onto the task branch. The PR already exists,
        # so the append above lands only in the COORDINATOR mirror; without this the
        # branch cites a ledger it does not carry and MergeGate's metadata check
        # fails closed with "Task ledger evidence L-… is missing".
        result["state_sync"] = _sync_state_to_branch(target, task_id, ledger_id)
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
        result.update(
            _dispatch_repair(
                target,
                task,
                decision.get("failed_checks", []),
                attempt,
                cca_verdict=decision.get("cca_verdict"),
            )
        )
        # Deliver the repair to the PR head, exactly as the create_pr branch
        # delivers the initial implementation. The repair runner writes its fix
        # into the worktree but — like the impl runner — does not commit/push;
        # without this the repair finishes green in the worktree and the PR never
        # moves (the manual `git commit && git push` this task removes). Commit and
        # push the fix, then require BOTH a reported push AND an advanced PR head:
        # a delivery failure (or a repair that produced no change, so `git push`
        # reports "Everything up-to-date" and the head never moves) stops the loop
        # with a NAMED reason instead of silently spinning — and burning — further
        # repair attempts against an unchanged head that can never turn its checks
        # green. Snapshot the pushed head before and after so the guarantee is that
        # the head actually MOVED, not merely that the push command returned 0.
        head_before = _pr_branch_head(target, task_id)
        impl = _commit_and_push_implementation(target, task_id)
        result["impl_commit"] = impl
        head_after = _pr_branch_head(target, task_id)
        if "pushed to the task branch" not in impl:
            result["action"] = "stop_blocked"
            result["reason"] = (
                f"repair fix was not delivered to the task branch ({impl}); the PR "
                "head did not move, so its required checks cannot re-run — diagnose "
                "or re-dispatch"
            )
            return result
        if head_after is None or head_after == head_before:
            result["action"] = "stop_blocked"
            result["reason"] = (
                "repair produced no change: the PR head did not advance past "
                f"{head_before or '(unpushed)'} ({impl}), so its required checks "
                "cannot re-run and the repair did not fix the failure — diagnose "
                "or re-dispatch"
            )
            return result
        # DEFECT B — the repair effector appended a repair ledger (and the runner may
        # append more) to the COORDINATOR mirror; carry those, plus the transitive
        # evidence ledger_evidence cites, onto the task branch so MergeGate does not
        # block the re-run with "Task ledger evidence L-… is missing". Sync AFTER the
        # head-movement guard so this evidence commit never masks a no-op repair.
        result["state_sync"] = _sync_state_to_branch(target, task_id, None)
    elif action == "create_closeout_pr":
        # ADR 0012: the impl PR merged; open a closeout PR that pushes the terminal
        # state (task=done + lock=released + goal=complete) to main. The effector
        # repoints expected_pr to the closeout PR, so the snapshot/merge path drives
        # it next. Fails closed to stop_blocked inside the effector.
        result.update(_create_closeout_pr(target, goal_id, task_id))
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
        # ADR 0012: done-marking is DEFERRED. The loop never records `done` locally
        # until it is durable on main. After the IMPL PR merges the task stays
        # `review` and the next decision routes to create_closeout_pr; after the
        # CLOSEOUT PR merges, the `mark_done` action (below) records done + unblocks.
    elif action == "mark_done":
        result.update(_mark_done(target, task_id, "PR already merged"))
        result["unblocked"], _ = _unblock_ready_tasks(target, goal_id)
    elif action == "unblock":
        unblocked, lock_blocked = _unblock_ready_tasks(target, goal_id)
        result["unblocked"] = unblocked
        if not unblocked and lock_blocked:
            # Serialization behind another task's active lock — name the owner and
            # overlapping locks so the loop output is not mistaken for a deadlock.
            result.update(_lock_blocked_stop(lock_blocked))
        elif not unblocked:
            result["action"] = "stop_blocked"
            result["reason"] = "dependency-blocked tasks could not be unblocked (incomplete dependencies)"
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
        lease_state = None
        cca_verdict = None
        if task.get("status") == "review":
            pr_state, checks = snapshot_pr(target, task)
            # DEFECT A: when the `CCA verdict` required check is red on an impl PR,
            # resolve the verdict VALUE read-only from durable CI evidence so the
            # pure engine can gate on it (a needs_guardian / blocked / complete /
            # unresolvable verdict is never handed to a repair). The bookkeeping
            # closeout path has its own CCA handling and never dispatches a repair,
            # so it does not need the value.
            if checks.get(CCA_VERDICT_CHECK) == "fail" and not task.get("closeout_pr"):
                cca_verdict = _resolve_cca_verdict(target, task, pr_state)
        elif task.get("status") == "running":
            # Probe the OS lease so a stranded `running` task (session died) is
            # distinguished from a live one instead of waiting on it forever.
            lease_state = session_lease_state(target, str(task.get("id")))
        decisions.append(
            decide_task_action(
                task,
                checks=checks,
                pr_state=pr_state,
                repair_attempts=repair_attempts_for(target, str(task.get("id"))),
                repair_limit=repair_limit,
                required_checks=list(required_checks),
                cca_reruns=int(task.get("cca_rerun_count") or 0),
                lease_state=lease_state,
                cca_verdict=cca_verdict,
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
