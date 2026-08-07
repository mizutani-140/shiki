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
from shiki_runtime import dispatch_runner_task, resolve_default_branch_ref, session_lease_state
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
# GitHub's `mergeStateStatus` value meaning "the base moved while the PR was open
# and up-to-date branch protection requires a base merge before merging". It is the
# ONLY mergeStateStatus the loop remediates: every other non-mergeable state
# (DIRTY = real conflict, BLOCKED = missing review/checks, ...) falls through to the
# merge effector, whose existing failure message still covers those cases.
MERGE_STATE_BEHIND = "BEHIND"
# The Guardian/policy gate. It enforces guardian-policy.json (human
# review/label/comment OR an external AI guardian review, ADR 0010) and is the
# ONLY required check that must never become an auto-repair target: an
# autonomous runner must never be instructed to "make the Guardian gate pass".
POLICY_GATE = "MergeGate policy check"
CCA_VERDICT_CHECK = "CCA verdict"
MAX_CCA_RERUNS = 2
# Bound on how many times the loop base-syncs a BEHIND branch whose red required
# check is caused by base movement (the base moved after the branch forked, so a
# base file the branch never had reads as "deleted"). Mirrors MAX_CCA_RERUNS: a
# durable per-task `base_sync_attempts` counter is checked against this, and once
# it is reached the decision reverts to today's dispatch_repair -> repair limit ->
# stop_guardian path so the repair limit never becomes dead code and a permanently
# stale branch cannot livelock on syncs.
MAX_BASE_SYNCS = 2

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
# Sentinel for a verdict the enforcer REFUSED for a TRANSIENT cause: a blocking
# item or acceptance criterion the judge left insufficient_evidence because its
# evidence was not yet available at judge time (the CCA is itself one of the
# concurrently-running required checks; observed on PR #291/#292). It is NOT a
# real verdict either — the refused verdict's own value must never be resolved —
# but unlike the unresolvable case it is re-runnable: the gate drives the bounded
# rerun_cca path on it rather than a terminal stop. A NON-transient refusal
# (missing evidence, a durable fail, a structural fault) resolves to
# CCA_VERDICT_UNRESOLVED and keeps today's terminal stop.
CCA_VERDICT_TRANSIENT_REFUSAL = "transient_refusal"
# The CCA workflow uploads `.shiki/gha` as the `shiki-cca-evidence` artifact; the
# verdict lands at this basename inside it.
CCA_VERDICT_BASENAME = "cca-verdict.json"
# A REFUSED verdict is not written to cca-verdict.json (that path is the operative
# authority every consumer reads); the enforcer writes a refusal record on this
# sibling basename instead. Kept byte-identical to
# ``enforce_cca_verdict.refusal_record_path(cca-verdict.json).name`` (a cross-module
# agreement test pins them together) so the resolver reads exactly what the
# enforcer wrote, without importing the enforcer into the loop.
CCA_REFUSAL_BASENAME = "cca-verdict.refusal.json"

# The MergeGate metadata check's own blocking reasons. ``mergegate_check.py`` writes
# them to ``.shiki/gha/mergegate-result.json`` (the ``blocking_reasons`` array),
# BEFORE it exits non-zero, so the file exists on the FAILING run too. The
# ``shiki-mergegate.yml`` workflow uploads that file as this artifact so the loop can
# read WHY the check blocked and name those reasons in the repair packet + handoff,
# instead of every packet saying only "required checks failed: MergeGate metadata
# check". These are literals (not imported from ``shiki_evidence``) because that
# module is out of this change's locks; the workflow YAML references the same
# strings. Purely informative — the reasons never change a loop decision.
MERGEGATE_WORKFLOW = "shiki-mergegate.yml"
MERGEGATE_RESULT_ARTIFACT_NAME = "shiki-mergegate-result"
MERGEGATE_RESULT_BASENAME = "mergegate-result.json"

# Bound on how many times the loop re-dispatches a `running` task whose session
# lease proves the session is gone (free/absent). Once the recorded
# `dispatch_attempts` reaches this bound the loop stops for the operator instead
# of spinning: a session that keeps dying is a real failure, not a strand to
# silently retry forever.
MAX_DISPATCH_ATTEMPTS = 2

# Bound on how many times the loop re-runs the pre-PR reviewer when it returns a
# BLOCKING verdict that names ZERO findings — a degenerate verdict that produced
# nothing usable (Measured 2026-08-05/06: the reviewer blocked with 0 findings on
# work that met every contract requirement; a plain re-run then passed). The
# retry budget is a durable per-task counter (`pre_pr_review_empty_retries`) so it
# bounds across the loop's fresh invocations, and it is RESET on every
# (re-)dispatch so it bounds retries of the SAME reviewed work and never wedges
# freshly re-dispatched work.
MAX_PRE_PR_REVIEW_EMPTY_RETRIES = 1

# Engine action names, in execution priority order for a goal pass.
ACTION_PRIORITY = (
    "mark_done",
    "create_closeout_pr",
    "merge",
    "sync_branch",
    "rerun_cca",
    "dispatch_repair",
    "create_pr",
    "redispatch",
    "dispatch",
    "unblock",
)
STOP_ACTIONS = {"stop_guardian", "stop_blocked", "stop_lock_blocked"}
# ``wait_review`` is an execute-time override the create_pr effector emits when the
# pre-PR reviewer returned a degenerate (blocking, zero-findings) verdict and the
# retry budget still allows one more attempt: the loop waits and re-runs create_pr
# next pass (which re-runs the reviewer), rather than stopping or opening a PR on a
# verdict that named nothing. The decision engine never emits it.
WAIT_ACTIONS = {"wait_checks", "wait_runner", "wait_dependencies", "wait_review", "none"}


def _merge_or_sync(task_id: str, pr_state: dict[str, Any] | None, merge_reason: str) -> dict[str, Any]:
    """The final merge-gate choice: ``merge``, or ``sync_branch`` when BEHIND.

    Reached only once a PR is otherwise ready to merge — every required check
    green, the Guardian gate resolved, the merge decision already made. When the
    branch has simply fallen behind its base (``mergeStateStatus == BEHIND``), a
    merge would fail under up-to-date branch protection with nothing actually
    wrong, so the loop takes a bounded ``sync_branch`` action first and re-verifies
    the new head. Every other state (CLEAN, DIRTY, BLOCKED, a missing status)
    proceeds to ``merge`` unchanged: a real conflict or protection failure is left
    for the merge effector's existing failure message.
    """
    if (pr_state or {}).get("merge_state_status") == MERGE_STATE_BEHIND:
        return {
            "action": "sync_branch",
            "task_id": task_id,
            "reason": (
                "PR is ready to merge but its branch is BEHIND the base; sync the base "
                "into the branch and re-verify the checks before merging"
            ),
        }
    return {"action": "merge", "task_id": task_id, "reason": merge_reason}


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

    ``pr_state`` is consulted for a ``ready`` task too, not only ``review``: a
    re-opened task carrying a MERGED ``expected_pr`` from a previous cycle is
    dispatched with ``clear_expected_pr`` set so the effector drops the stale pointer
    before cutting a fresh implementation (a pure-engine caller that leaves it
    ``None`` for a ready task keeps the plain dispatch behaviour).
    """
    task_id = str(task.get("id"))
    status = str(task.get("status", ""))

    if status == "done":
        return {"action": "none", "task_id": task_id, "reason": "task is done"}
    if status in {"planned", "blocked"}:
        # A `planned` task carrying a stale merged pointer (below) is harmless until it
        # is dispatched: it reaches `review` — where the pointer would mislead — only
        # by first becoming `ready`, and the ready branch clears the pointer as it cuts
        # the fresh implementation. So keep waiting for dependencies; a non-terminal
        # task is simply never read as done here (it returns before the merge check).
        return {"action": "wait_dependencies", "task_id": task_id, "reason": f"task is {status}"}
    if status == "ready":
        # A `ready` task normally has no PR yet. One that carries a MERGED expected_pr
        # is a re-opened task holding a stale pointer from a previous cycle: the loop
        # repoints expected_pr at a task's closeout PR, and a Spec Amendment can then
        # re-open the task (status -> ready) while that now-merged pointer survives,
        # frozen to base by MergeGate. A ready (non-terminal) status and a merged
        # expected_pr cannot both describe the CURRENT cycle — no PR has been cut for
        # this cycle's work — so that merged PR is a previous cycle's, never this
        # cycle's completed implementation. Clear the pointer and cut a fresh
        # implementation; reading it as done drives a closeout whose diff carries no
        # source change (the amended contract silently unimplemented, the CCA
        # confirming `complete`). Detected from the task record alone — the status plus
        # the pointer's merge state — never an amendment marker, ledger scan or PR
        # timestamp. The failure is asymmetric: clearing a still-live pointer at worst
        # re-opens a PR-less cycle the loop already handles, while reading a stale one
        # silently ships nothing. The `review` path (below) is the normal route INTO
        # the closeout and is deliberately excluded.
        if pr_state and pr_state.get("merged"):
            return {
                "action": "dispatch",
                "task_id": task_id,
                "clear_expected_pr": True,
                "reason": (
                    "task is ready but its expected_pr names an already-merged PR from a "
                    "previous cycle (a re-opened task's stale pointer); clearing it and "
                    "cutting a fresh implementation"
                ),
            }
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
            return _merge_or_sync(task_id, pr_state, "closeout PR checks green; merge to push completion to main")
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
            if cca_verdict == CCA_VERDICT_TRANSIENT_REFUSAL:
                # The verdict was REFUSED for a transient cause (a blocking item or
                # acceptance criterion left insufficient_evidence because its evidence
                # was not yet available at judge time — the CCA is itself one of the
                # concurrently-running required checks). This is NOT a judgment, so it
                # must never reach _cca_verdict_stop as if it were one: drive the
                # bounded rerun_cca path (the same MAX_CCA_RERUNS budget the same-head
                # completion race uses) so the CCA re-judges after the concurrent
                # checks settle. Once that budget is exhausted the refusal is no longer
                # re-runnable, so it degrades to today's terminal fail-closed stop
                # (the unresolvable reason) — never a repair on a non-judgment.
                if cca_reruns < MAX_CCA_RERUNS:
                    return {
                        "action": "rerun_cca",
                        "task_id": task_id,
                        "reason": (
                            "the CCA verdict was refused for a transient cause (a blocking item "
                            "was left insufficient_evidence because its evidence was not yet "
                            "available at judge time); rerun after the concurrent checks settle"
                        ),
                    }
                cca_verdict = CCA_VERDICT_UNRESOLVED
            verdict_stop = _cca_verdict_stop(task_id, cca_verdict)
            if verdict_stop is not None:
                return verdict_stop
            repairable_failed = [name for name in repairable_failed if name != CCA_VERDICT_CHECK]
        failure_desc = ", ".join(repairable_failed) if repairable_failed else f"CCA verdict {cca_verdict}"
        # A red required check on a branch that is BEHIND its base is often CAUSED by
        # the base moving after the branch forked — e.g. main gained an
        # append-only-evidence ledger the branch never had, so the MergeGate metadata
        # check reports "PR must not delete base ledger file …". The task is fine; the
        # branch is merely stale. A bounded base sync remediates it — three identical
        # no-op repairs do not (PR #288). This arm is reached ONLY after the verdict
        # gate above returned None, so a needs_guardian / blocked / complete /
        # unresolvable CCA verdict has already produced its terminal stop and is never
        # laundered into a head-moving sync (ADR 0010). It also never fires while the
        # policy (Guardian) gate is red — for ANY reason, not only when it is the sole
        # red check — so a head-bound Guardian approval, or an ADR 0015
        # contract-approval carry that base movement defeated (the policy gate compares
        # base and head task governance directly), is never silently restored by
        # adopting base wholesale. It is bounded by a durable per-task counter so the
        # repair limit stays reachable and a permanently stale branch cannot livelock.
        # A sync creates no repair packet and consumes no repair attempt.
        behind = (pr_state or {}).get("merge_state_status") == MERGE_STATE_BEHIND
        base_syncs = int(task.get("base_sync_attempts") or 0)
        if behind and not policy_failed and base_syncs < MAX_BASE_SYNCS:
            return {
                "action": "sync_branch",
                "task_id": task_id,
                "reason": (
                    f"branch is BEHIND its base with a red required check ({failure_desc}); "
                    "the base moved after the branch forked, so sync the base into the "
                    "branch and re-verify before repairing (bounded base sync "
                    f"{base_syncs + 1}/{MAX_BASE_SYNCS}, no repair attempt consumed)"
                ),
            }
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
        return _merge_or_sync(task_id, pr_state, f"all required checks green and risk {risk} permits auto-merge")
    # High/critical risk requires Guardian approval, but the "MergeGate policy
    # check" required check IS the Guardian gate: it enforces guardian-policy.json
    # (human review/label/comment OR an external AI guardian review, ADR 0010).
    # When it is green, Guardian approval — by whatever authority — was recorded,
    # so the loop may merge autonomously.
    if "MergeGate policy check" in required_checks:
        return _merge_or_sync(
            task_id,
            pr_state,
            f"all required checks green incl. the MergeGate policy Guardian gate; risk {risk} approved by recorded authority",
        )
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
    view = _gh(target, ["pr", "view", str(pr), "--json", "state,mergedAt,headRefOid,mergeStateStatus"], check=False)
    if view.returncode != 0:
        # Transient gh/network/auth failure must not be read as "no PR".
        return {"number": pr, "error": True}, {}
    state = json.loads(view.stdout)
    pr_state = {
        "number": pr,
        "state": state.get("state"),
        "merged": bool(state.get("mergedAt")),
        "head_sha": state.get("headRefOid"),
        # GitHub's computed mergeability: BEHIND means the base moved and up-to-date
        # branch protection would reject `gh pr merge` even with every check green.
        # The decision engine syncs the base in first instead of failing the merge.
        "merge_state_status": state.get("mergeStateStatus"),
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
            if verdict_path.is_file():
                data = json.loads(verdict_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return CCA_VERDICT_UNRESOLVED
                verdict = data.get("verdict")
                if isinstance(verdict, str) and verdict in CCA_VERDICT_VALUES:
                    return verdict
                return CCA_VERDICT_UNRESOLVED
            # No cca-verdict.json means the enforcer REFUSED the verdict (it writes
            # cca-verdict.json only for a verdict that passed validation) and left a
            # refusal record on the sibling path instead. Read that record's
            # transient classification ONLY — never the refused verdict's own value,
            # which must never be resolved as if it were a judgment. A transient
            # refusal is re-runnable; anything else (non-transient, or no record at
            # all) fails closed to the unresolvable case and keeps today's terminal
            # stop.
            refusal_path = tmp / CCA_REFUSAL_BASENAME
            if refusal_path.is_file():
                refusal = json.loads(refusal_path.read_text(encoding="utf-8"))
                if isinstance(refusal, dict) and refusal.get("transient") is True:
                    return CCA_VERDICT_TRANSIENT_REFUSAL
                return CCA_VERDICT_UNRESOLVED
            return CCA_VERDICT_UNRESOLVED
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        # Read-only resolution never crashes the loop: any error IS the
        # unresolvable case (fail closed), never a raised exception.
        return CCA_VERDICT_UNRESOLVED


def _resolve_blocking_reasons(
    target: Path, task: dict[str, Any], pr_state: dict[str, Any] | None
) -> list[str]:
    """Read the MergeGate gate's own blocking reasons from durable CI evidence.

    A red ``MergeGate metadata check`` writes WHY it blocked to
    ``.shiki/gha/mergegate-result.json`` (the ``blocking_reasons`` array) BEFORE it
    exits non-zero, and ``shiki-mergegate.yml`` uploads that file as the
    ``shiki-mergegate-result`` artifact. Find the MergeGate run for the PR head,
    download the artifact read-only, and return its ``blocking_reasons`` so the
    repair packet + handoff can name the actual blockers instead of only the failing
    check.

    Purely INFORMATIVE and read-only: this never mutates GitHub, never raises into
    the loop, and never changes a decision. ANY inability to read the reasons — no
    run, no artifact, ``gh``/network failure, an unreadable or malformed file, or a
    result that names none — degrades to ``[]`` ("no reasons available"). The reasons
    are UNTRUSTED text (derived from PR content) and are returned verbatim for the
    caller to render as data, never as instructions.
    """
    import shutil
    import tempfile

    try:
        head_sha = (pr_state or {}).get("head_sha")
        runs = _gh(
            target,
            ["run", "list", "--workflow", MERGEGATE_WORKFLOW, "--limit", "20",
             "--json", "databaseId,conclusion,headSha,status"],
            check=False,
        )
        if runs.returncode != 0 or not (runs.stdout or "").strip():
            return []
        entries = json.loads(runs.stdout)
        if not isinstance(entries, list):
            return []
        run_id = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Bind to the PR head when it is known so a stale run for a superseded
            # head cannot surface reasons that no longer describe the current gate.
            if head_sha and entry.get("headSha") != head_sha:
                continue
            run_id = entry.get("databaseId")
            if run_id is not None:
                break
        if run_id is None:
            return []
        tmp = Path(tempfile.mkdtemp(prefix="shiki-mergegate-reasons-"))
        try:
            download = _gh(
                target,
                ["run", "download", str(run_id), "--name", MERGEGATE_RESULT_ARTIFACT_NAME,
                 "--dir", str(tmp)],
                check=False,
            )
            if download.returncode != 0:
                return []
            result_path = tmp / MERGEGATE_RESULT_BASENAME
            if not result_path.is_file():
                return []
            data = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return []
            reasons = data.get("blocking_reasons")
            if not isinstance(reasons, list):
                return []
            # Keep only non-empty strings: a malformed entry must not crash rendering,
            # and an empty result reads as "no reasons available".
            return [reason for reason in reasons if isinstance(reason, str) and reason.strip()]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        # Read-only resolution never crashes the loop: any error degrades to no
        # reasons, exactly like a missing artifact — and changes no decision.
        return []


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


def _reset_pre_pr_review_retries(target: Path, task_id: str) -> None:
    """Clear the pre-PR degenerate-review retry budget on a (re-)dispatch.

    A (re-)dispatch produces fresh reviewed work, so the durable
    ``pre_pr_review_empty_retries`` counter — which bounds re-runs of the reviewer
    against the SAME work across the loop's fresh invocations — must not carry over.
    Without this reset a durable counter would wedge freshly re-dispatched work at
    the bound; with it, the counter bounds retries of one reviewed change and never
    blocks new work."""
    task = load_task(target, task_id)
    if task.get("pre_pr_review_empty_retries"):
        task["pre_pr_review_empty_retries"] = 0
        _save_task(target, task)


def _clear_stale_expected_pr(target: Path, task_id: str) -> None:
    """Drop a re-opened task's stale ``expected_pr`` before it is re-dispatched.

    The loop repoints ``expected_pr`` at a task's closeout PR; a Spec Amendment can
    then re-open the task (status -> ready) while that now-merged pointer survives,
    frozen to base by MergeGate. ``decide_task_action`` flags such a decision with
    ``clear_expected_pr``. Clearing the pointer here means the fresh implementation's
    ``create_pr`` opens a NEW PR rather than the loop reading the merged pointer as
    this cycle's completed work and driving a source-free closeout (see
    ``decide_task_action``). Idempotent: a task with no ``expected_pr`` is untouched."""
    task = load_task(target, task_id)
    if task.get("expected_pr") is None:
        return
    task["expected_pr"] = None
    _save_task(target, task)


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
    mergegate_blocking_reasons: list[str] | None = None,
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
        # The MergeGate gate's OWN blocking reasons (resolved read-only from durable
        # CI evidence; [] when unavailable). They are untrusted PR-derived text — the
        # packet stores them verbatim and the handoff renders them as data.
        mergegate_blocking_reasons=mergegate_blocking_reasons,
    )
    cmd_handoff_repair(argparse.Namespace(target=str(target), repair_id=repair_id))
    returncode = _dispatch(target, load_task(target, task["id"]), repair_id=repair_id)
    return {"repair_id": repair_id, "returncode": returncode}


def _default_base_ref(target: Path) -> str:
    """Resolve the default-branch commit-ish to compare a task branch against.

    Reuses ``shiki_runtime.resolve_default_branch_ref``, which runs a best-effort
    ``git fetch origin <name>`` and then prefers the remote-tracking ref
    ``origin/<name>`` over the bare local ``<name>``. Nothing keeps a
    coordinator's local default branch current — the loop fetches ``origin`` for
    its own merges but never fast-forwards the local ref — so the local branch
    silently lags the remote after other goals merge. Counting or diffing against
    that stale local ref hands the ahead-count (and the pre-PR reviewer) the whole
    already-merged history instead of the task's own change; the refreshed
    remote-tracking ref is the authority.

    ``resolve_default_branch_ref`` raises ``ShikiError`` only when NEITHER the
    remote-tracking ref nor the local branch can be resolved — a fetch failure
    alone degrades to the ref already present. Fall back to the bare default name
    on any failure (both callers contract "never raises into the loop", matching
    the T1 fail-closed effector pattern) so resolution reproduces exactly the
    historical bare-``main`` behaviour of these call sites instead of crashing.
    """
    try:
        return resolve_default_branch_ref(target)
    except Exception:
        return "main"


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
    base = _default_base_ref(target)
    ahead = run(["git", "rev-list", "--count", f"{base}..HEAD"], cwd=worktree_path, check=False)
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


def _loop_authored_repair_paths(target: Path, task_id: str) -> set[str]:
    """The ``.shiki`` paths the loop ITSELF (re)writes into the task worktree during
    a repair cycle — loop-authored bookkeeping, never implementer work.

    ``dispatch_runner_task`` calls ``sync_contract_into_worktree`` before the
    session, copying the coordinator's CURRENT task, lock and goal records over the
    worktree's copies; and the task's own mirror record additionally carries the
    loop's ``status`` / ``cca_rerun_count`` / ``ledger_evidence`` bookkeeping. When
    ``_commit_and_push_implementation`` then ``git add -A``s the worktree, a change
    to any of these commits and advances the PR head even though the repairer fixed
    nothing — the PR #310 repro (a lone ``.shiki/tasks/<id>.json`` commit, zero
    source).

    Deliberately NARROW: EXACTLY the per-task / per-goal contract-mirror records the
    loop syncs, NEVER a blanket ``.shiki`` exclusion. A repair may legitimately need
    to change other mirror content (a sibling record, a fixture, a schema), and such
    a change MUST still count as delivered.
    """
    paths = {
        f".shiki/tasks/{task_id}.json",
        f".shiki/locks/{task_id}.json",
    }
    goal_id = str(load_task(target, task_id).get("goal_id") or "")
    if goal_id:
        paths.add(f".shiki/goals/{goal_id}.json")
    return paths


def _repair_implementer_changes(
    target: Path, task_id: str, head_before: str | None, head_after: str | None
) -> list[str] | None:
    """The files a repair push added that the IMPLEMENTER owns.

    ``_commit_and_push_implementation`` ``git add -A``s the whole task worktree, so
    a push can advance the PR head carrying NOTHING the repairer changed: the runner
    dispatch's ``sync_contract_into_worktree`` rewrites the task's own mirror record
    (and its lock/goal) in the worktree, which commits and moves the head on a no-op
    repair. Judge delivery by the pushed commit range (``head_before..head_after``)
    with that loop-authored bookkeeping removed — a repair DELIVERS only when the
    range still holds something. Head movement alone is NOT proof of delivery (the
    loop's own bookkeeping always trips it).

    Returns the implementer-owned changed paths (``[]`` means the range held ONLY
    bookkeeping → a no-op repair the caller stops), or ``None`` when the per-push
    delta cannot be scoped: no prior pushed head (a first push carries the whole
    branch — impl + fix — which is real delivery, never bookkeeping-only), no
    worktree, or a git error. The caller treats ``None`` as "cannot prove a no-op"
    and proceeds; a real repair always has a pushed head, so ``None`` is the
    degenerate first-push case, not the failure this guard is built to catch.
    """
    if not head_before or not head_after:
        return None
    record = worktree_record(target, task_id)
    if not record:
        return None
    worktree_path = Path(record["path"]).expanduser().resolve()
    if not worktree_path.exists():
        return None
    diff = run(
        ["git", "diff", "--name-only", f"{head_before}..{head_after}"],
        cwd=worktree_path,
        check=False,
    )
    if diff.returncode != 0:
        return None
    bookkeeping = _loop_authored_repair_paths(target, task_id)
    changed = [line.strip() for line in (diff.stdout or "").splitlines() if line.strip()]
    return [rel for rel in changed if rel not in bookkeeping]


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
    copied: list[str] = []
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
            copied.append(relative)
    # Stage the coordinator's OWN copies of the evidence just delivered to the
    # branch. The loop writes .shiki JSON straight into the coordinator working
    # tree and never stages it; once the same byte-identical file lands on the
    # default branch (this task's PR merges), a later `git merge origin/main` in
    # the coordinator aborts with "untracked working tree files would be
    # overwritten by merge" and has to be resolved by hand. Staging — NOT
    # committing — makes each path tracked, so the merge absorbs the returning
    # file cleanly. The coordinator's local branch is never advanced and nothing
    # is carried onto a foreign branch: these are exactly the paths copied above,
    # this task's own delivered evidence.
    if copied:
        run(["git", "add", "--", *copied], cwd=target, check=False)
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
    "wrap the JSON in prose.\n\n## Task diff (git diff of the task branch against the default branch)\n"
)


def _record_reviewer_raw_output(target: Path, task_id: str, raw_stdout: str) -> str:
    """Record the reviewer's raw output as durable, branch-syncable evidence.

    A degenerate (blocking, zero-findings) verdict names nothing usable, so its
    only diagnosable artifact is what the reviewer actually emitted. Mirror the
    TDD gate's EXEC pattern — an ``EXEC-*.json`` holding the raw stdout, referenced
    by a ``type:"check"`` ledger — so a future diagnosis has something concrete to
    read. Returns the ledger id (the EXEC id is embedded in its evidence ref)."""
    from shiki_process import shiki_path as _shiki_path
    from shiki_process import utc_now, write_json
    from shiki_tasks import next_control_id

    task = load_task(target, task_id)
    record_id = next_control_id(target, "EXEC")
    record_file = _shiki_path(target, "runner", f"{record_id}.json")
    write_json(
        record_file,
        {
            "id": record_id,
            "task_id": task["id"],
            "goal_id": task["goal_id"],
            "command": "independent pre-PR code review (claude -p, read-only) — ADR 0011",
            "returncode": 0,
            "stdout": raw_stdout,
            "stderr": "",
            "created_at": utc_now(),
        },
    )
    exec_rel = str(record_file.relative_to(target))
    return append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task_id,
        ledger_type="check",
        summary=(
            f"Pre-PR code-review verdict BLOCKING with zero findings for {task_id} "
            "(degenerate — the reviewer produced no usable judgment); raw reviewer "
            "output recorded for diagnosis"
        ),
        evidence=[exec_rel],
    )


def _run_pre_pr_code_review(target: Path, task_id: str) -> dict[str, Any]:
    """Run the independent read-only code-review verifier over the task diff.

    Loop-owned quality-gate step (ADR 0011): the reviewer is the same model as the
    implementer but in a separate context, confined to read tools (no edit tools),
    bound to a structured verdict. The loop parses that verdict deterministically.

    Returns a dict with ``status`` in {clean, blocking, retry, fail}:

    * ``clean``    — verdict parsed as clean; a type:"check" "code-review" ledger
      is recorded and ``pre_pr_code_review`` is written onto the task so the
      PR-12 ``## Pre-PR code review`` body section renders from it.
    * ``blocking`` — verdict parsed as blocking WITH at least one named finding, or
      a degenerate blocking verdict whose retry budget is exhausted. Fail-closed: a
      blocking pre-PR review CANNOT anchor a repair packet (no PR exists yet at
      create_pr time by construction), so the caller stops the loop for diagnosis.
      NOT a repair.
    * ``retry``    — verdict parsed as blocking but naming ZERO findings (degenerate:
      the reviewer produced nothing usable) while the durable retry budget still
      allows one more attempt. NEVER treated as clean: the caller re-runs the
      reviewer next pass rather than opening a PR or stopping. The reviewer's raw
      output is recorded durably first.
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
    # is complete, then diff the index against the default branch resolved from
    # origin. This shows the FULL task change set the reviewer must judge,
    # committed or not, tracked or new — and never the already-merged history a
    # stale local default branch would drag in.
    run(["git", "add", "-A"], cwd=worktree_path, check=False)
    base = _default_base_ref(target)
    diff = run(["git", "diff", "--cached", base], cwd=worktree_path, check=False)
    if diff.returncode != 0:
        # Fall back to the committed-only diff (e.g. the default branch is
        # unrelated/missing so the two-dot diff cannot be computed).
        diff = run(["git", "diff", f"{base}...HEAD"], cwd=worktree_path, check=False)
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
        findings = verdict.get("findings") or []
        if not findings:
            # A blocking verdict that names NOTHING is not a usable judgment — it
            # blocked the PR while pointing at no concrete issue (Measured
            # 2026-08-05/06 on work that met every contract requirement; a plain
            # re-run then passed). Record the reviewer's raw output durably so a
            # future diagnosis has something to read, then RETRY once (bounded by a
            # durable per-task counter) before stopping. A degenerate verdict is
            # NEVER treated as clean: the retry re-runs the reviewer, and once the
            # budget is exhausted the loop stops for diagnosis with the degenerate
            # verdict named — it never becomes a silent pass.
            ledger_id = _record_reviewer_raw_output(target, task_id, exec_result.stdout)
            task = load_task(target, task_id)
            task.setdefault("ledger_evidence", []).append(ledger_id)
            retries = int(task.get("pre_pr_review_empty_retries") or 0)
            if retries < MAX_PRE_PR_REVIEW_EMPTY_RETRIES:
                task["pre_pr_review_empty_retries"] = retries + 1
                task["pre_pr_code_review"] = {
                    "verdict": "blocking",
                    "findings": [],
                    "degenerate": True,
                    "empty_retries": retries + 1,
                    "ledger_id": ledger_id,
                }
                _save_task(target, task)
                return {
                    "status": "retry",
                    "reason": (
                        "independent pre-PR code review returned a blocking verdict naming zero "
                        f"findings (attempt {retries + 1}/{MAX_PRE_PR_REVIEW_EMPTY_RETRIES + 1}); "
                        "the reviewer produced nothing usable — re-running it on the same work"
                    ),
                    "ledger_id": ledger_id,
                    "empty_retries": retries + 1,
                }
            task["pre_pr_code_review"] = {
                "verdict": "blocking",
                "findings": [],
                "degenerate": True,
                "empty_retries": retries,
                "ledger_id": ledger_id,
            }
            _save_task(target, task)
            return {
                "status": "blocking",
                "reason": (
                    "independent pre-PR code review returned a blocking verdict naming zero "
                    f"findings after {retries} retr{'y' if retries == 1 else 'ies'}; the reviewer "
                    "produced nothing usable — diagnose (its raw output is recorded)"
                ),
                "ledger_id": ledger_id,
                "degenerate": True,
            }
        # Record the blocking verdict as a check ledger for the audit trail, then
        # fail closed. No PR exists yet, so this cannot become a repair packet.
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


def _sync_branch(target: Path, task: dict[str, Any], *, base: str = "main") -> dict[str, Any]:
    """Merge the base branch into a PR branch that fell BEHIND, then push it.

    Reached only when the PR is otherwise ready to merge (all required checks
    green, Guardian resolved) but GitHub reports the branch is BEHIND its base:
    the base moved while the PR was open and up-to-date branch protection would
    reject ``gh pr merge`` with nothing actually wrong. This does a plain
    ``git merge origin/<base>`` in a THROWAWAY detached checkout of the PR branch
    and a plain ``git push`` — it never rewrites history. Working in a detached
    checkout of ``origin/<branch>`` (rather than the registered worktree) makes one
    code path serve both the impl PR and the closeout PR, whose branch has no
    persistent worktree.

    A base merge that CONFLICTS is aborted so the branch is left byte-identical,
    and the loop stops with a reason that names the branch — distinct from the
    generic merge failure, so an operator can tell a real conflict from mechanical
    staleness (an automatic conflict resolution would silently pick one side of a
    governance file or lock declaration, so it must never be attempted). A push
    failure is a separate stop reason. On success the loop returns ``sync_branch``
    and the next pass waits for the required checks to re-run against the new head
    before merging. Fails closed to ``stop_blocked`` and never raises into the loop.
    """
    import shutil
    import tempfile

    task_id = str(task.get("id"))
    branch = str(task.get("expected_branch") or "")
    if not branch:
        return {
            "action": "stop_blocked",
            "task_id": task_id,
            "reason": f"task {task_id} has no expected_branch; cannot sync it with origin/{base}",
        }
    tmp_parent = None
    try:
        # Refresh the tracking refs the checkout/merge below read. In production this
        # reaches GitHub; if the fetch fails the operation still proceeds against the
        # already-known origin/* refs (the merge/push report the real error).
        run(["git", "fetch", "origin", base, branch], cwd=target, check=False)
        tmp_parent = Path(tempfile.mkdtemp(prefix="shiki-sync-"))
        worktree = tmp_parent / "wt"
        add = run(
            ["git", "worktree", "add", "--detach", str(worktree), f"origin/{branch}"],
            cwd=target,
            check=False,
        )
        if add.returncode != 0:
            return {
                "action": "stop_blocked",
                "task_id": task_id,
                "reason": (
                    f"could not check out {branch} to sync it with origin/{base}: "
                    f"{(add.stderr or '').strip()[-160:]}"
                ),
            }
        merge = run(["git", "merge", f"origin/{base}"], cwd=worktree, check=False)
        if merge.returncode != 0:
            # Abort so the branch is left exactly as it was — never resolve
            # automatically (see the docstring). This is a genuine conflict.
            run(["git", "merge", "--abort"], cwd=worktree, check=False)
            return {
                "action": "stop_blocked",
                "task_id": task_id,
                "reason": (
                    f"sync of {branch} with origin/{base} CONFLICTED and was aborted; the "
                    "branch is unchanged — this is a real conflict, not mechanical "
                    "staleness, so resolve it manually"
                ),
            }
        push = run(["git", "push", "origin", f"HEAD:{branch}"], cwd=worktree, check=False)
        if push.returncode != 0:
            return {
                "action": "stop_blocked",
                "task_id": task_id,
                "reason": (
                    f"sync of {branch} merged origin/{base} cleanly but the push failed: "
                    f"{(push.stderr or '').strip()[-160:]} — push the synced branch manually"
                ),
            }
        # A base sync now remediates a RED required check on the same footing as a
        # dispatch_repair, so it must leave a durable record (a sync followed by a
        # merge did not need one). Append a `mergegate` ledger — an EXISTING type the
        # merge effector already uses for merge-gate actions — with the concrete git
        # evidence. Guarded so a ledger-write failure can never turn an already
        # completed push into a stop.
        sync_ledger = None
        try:
            sync_ledger = append_ledger(
                target,
                goal_id=str(task.get("goal_id") or ""),
                task_id=task_id,
                ledger_type="mergegate",
                summary=(
                    f"Goal loop synced {branch} with origin/{base} (base moved after the "
                    "branch forked); the required checks re-run against the new head"
                ),
                evidence=[f"git merge origin/{base}", f"git push origin HEAD:{branch}"],
            )
        except Exception:  # noqa: BLE001 — a ledger failure must not undo a done push
            sync_ledger = None
        return {
            "action": "sync_branch",
            "task_id": task_id,
            "ledger_id": sync_ledger,
            "reason": (
                f"synced {branch} with origin/{base}; waiting for the required checks to "
                "re-run against the new head before merging"
            ),
        }
    except Exception as error:  # noqa: BLE001 — the effector must NEVER raise into the loop
        return {
            "action": "stop_blocked",
            "task_id": task_id,
            "reason": f"sync effector error for {branch}: {str(error)[:160]}",
        }
    finally:
        if tmp_parent is not None:
            run(["git", "worktree", "remove", "--force", str(tmp_parent / "wt")], cwd=target, check=False)
            shutil.rmtree(tmp_parent, ignore_errors=True)


def _reconcile_registered_worktree_to_origin(target: Path, task_id: str) -> str:
    """Fast-forward the task's REGISTERED worktree to the pushed (synced) head.

    ``_sync_branch`` pushes the base merge from a THROWAWAY detached checkout, so the
    task's registered worktree — the checkout a subsequent ``dispatch_repair`` commits
    into — still has its local branch at the pre-sync head. Without reconciliation the
    repair's ``git push -u origin <branch>`` is a non-fast-forward and fails with a
    misleading "push failed" message. Fast-forward ONLY — never a reset — so any
    uncommitted work in the worktree is preserved and an (impossible after a base
    merge) non-fast-forward fails loudly instead of being silently discarded. Reads
    the registered branch from the worktree record and never raises into the loop.
    """
    try:
        record = worktree_record(target, task_id)
        if not record:
            return "no registered worktree to reconcile"
        worktree_path = Path(record["path"]).expanduser().resolve()
        if not worktree_path.exists() or worktree_path == target.resolve():
            return "registered worktree unavailable; not reconciled"
        branch = str(record.get("branch") or "")
        if not branch:
            return "registered worktree has no branch; not reconciled"
        run(["git", "fetch", "origin", branch], cwd=worktree_path, check=False)
        forward = run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=worktree_path, check=False)
        if forward.returncode != 0:
            return "registered worktree could not fast-forward to the synced head; reconcile manually"
        return "registered worktree fast-forwarded to the synced head"
    except Exception as error:  # noqa: BLE001 — reconciliation must never raise into the loop
        return f"registered worktree reconciliation error: {str(error)[:120]}"


def _sync_goal_complete_mirror(target: Path, goal_id: str) -> dict[str, Any]:
    """Sync ONLY the completing goal's ``.shiki`` paths from ``origin/main``.

    After the closeout PR merged, main is authoritative for this goal (goal=complete
    + scorecard + task=done + lock=released) and the coordinator mirror must catch
    up — but NOT with a whole-tree ``git checkout origin/main -- .shiki``, which would
    revert every unrelated in-flight goal's files to main's stale version. This
    effector instead checks out only this goal's own id-named files (goal, dag,
    tasks, locks, worktrees) and the ledgers/reports those main-side files
    reference — restricted to paths that actually exist on ``origin/main`` so the
    checkout cannot error on a not-yet-pushed candidate.

    The completing goal's task files are left BYTE-IDENTICAL to main (the source of
    truth for a completed goal). Earlier this effector re-appended the loop's own
    coordinator-only merge/mark_done ledger ids the checkout reverted, but that
    diverged the task file from main and made a later ``git merge origin/main`` abort
    with "Your local changes ... would be overwritten" — and the merge would drop
    those ids anyway, so the re-append bought nothing durable. The loop's own
    merge/mark_done ledger FILES stay on disk (each names its task and goal), so the
    events remain durable evidence even though the completed task now mirrors main's
    reference set rather than carrying coordinator-only extras.

    Fail-open: on any sync failure the goal is still reflected ``complete`` locally
    so the run reports the durable truth. Never raises into the loop.
    """
    run(["git", "fetch", "origin", "main"], cwd=target, check=False)

    coordinator_tasks = tasks_for_goal(target, goal_id)
    task_ids = [str(t.get("id")) for t in coordinator_tasks]

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

    # The synced paths (byte-identical to main) are left in the working tree;
    # execute_action's single covering point (`_commit_coordinator_mirror`) commits
    # the coordinator's whole .shiki surface — including these — so a returning file
    # never aborts a later `git merge origin/main`. That merge is non-fast-forward
    # (the coordinator carries its own commits), but because the completing goal's
    # files now match main exactly it resolves cleanly with no manual step.

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


def _commit_coordinator_mirror(target: Path) -> None:
    """Commit the coordinator's whole ``.shiki`` working surface — a real commit,
    not a bare stage.

    The loop writes ``.shiki`` JSON straight into the coordinator working tree and
    never commits it there. The moment the same file lands on the default branch (a
    task or closeout PR merges), a later ``git merge origin/main`` collides on that
    returning path. MEASURED, with the colliding path byte-identical to the incoming
    version:

        staged   + fast-forward merge      -> succeeds
        staged   + non-fast-forward merge  -> "Your local changes to the following
                                              files would be overwritten by merge"
        committed + non-fast-forward merge -> clean ("Merge made by the ort strategy")

    The coordinator always carries its OWN commits, so its merge is ALWAYS the
    non-fast-forward case; ``git add`` only renamed the abort (untracked ->
    "local changes would be overwritten"). Committing absorbs it. The coordinator
    branch is a disposable execution surface that is never merged anywhere, so
    advancing HEAD costs nothing (the previous contract's unmeasured "the local
    branch must not advance" non-goal is removed — it was exactly what made the
    staged design unable to satisfy its own criterion).

    Only ``.shiki`` is staged, and this is a PLAIN index commit (not
    ``git commit -- .shiki``, which would re-read the working tree and record a
    removal — breaking the append-only mirror). A plain index commit records the
    WHOLE index, so "nothing outside ``.shiki`` is committed" rests on the coordinator
    checkout being loop-owned: the loop is its only writer and only ever stages
    ``.shiki`` (the ``_sync_state_to_branch`` and ``_sync_goal_complete_mirror`` adds
    are ``.shiki``-scoped; every implementer/worktree commit runs in a SEPARATE
    worktree index). Strict pathspec-scoping and no-deletion cannot both come from a
    single ``git commit``, so append-only is chosen and the scope rests on that
    invariant. ``--ignore-removal`` additionally guarantees this covering point never
    STAGES a deletion, so the append-only mirror cannot be emptied by this path.

    What remains, documented rather than suppressed: when the coordinator's copy and
    the incoming version genuinely DIFFER, the later ``git merge origin/main``
    produces a normal ``CONFLICT (add/add)`` instead of a hard refusal. That is
    correct — the two sides really disagree and main is the source of truth — so the
    resolution is left to the operator; no merge driver, strategy option, or
    automatic resolution is added. Measured, 27 of 28 and 20 of 20 real collisions
    were byte-identical, so a conflict is the exception, not the rule. Never raises
    into the loop (it runs from a ``finally``).
    """
    try:
        run(["git", "add", "--ignore-removal", "--", ".shiki"], cwd=target, check=False)
        # Commit only when the covering add actually staged mirror writes; otherwise
        # there is nothing to absorb and the commit would no-op. ``--no-verify`` skips
        # the coordinator's commit hooks: this is disposable bookkeeping on a branch
        # that is never pushed or merged, not a contribution that should trip lint /
        # test hooks (and a failing hook must never wedge this ``finally``).
        staged = run(["git", "diff", "--cached", "--name-only"], cwd=target, check=False)
        if (staged.stdout or "").strip():
            run(
                ["git", "commit", "--no-verify", "-m", "shiki: sync coordinator mirror (goal loop)"],
                cwd=target,
                check=False,
            )
    except Exception:  # noqa: BLE001 — committing must never break the loop
        pass


def execute_action(target: Path, goal_id: str, decision: dict[str, Any], *, repair_limit: int) -> dict[str, Any]:
    """Run one effector, then commit the coordinator mirror at the SINGLE point
    every effector passes through.

    ``_execute_action_body`` has eleven effector branches; several write ``.shiki``
    records straight into the coordinator (a merge ledger, a ``done`` task, a
    ``goal_complete`` sync, a closeout's repointed task). Placing the commit inside
    each branch would leave the next new effector uncovered, so it happens here
    instead — in a ``finally`` so a normal return, an early return (e.g.
    ``goal_complete``, ``wait_review``, a ``stop_blocked`` guard) or a future effector
    is covered by construction. It COMMITS the coordinator's ``.shiki`` surface (not
    merely stages it): the coordinator always carries its own commits, so a later
    ``git merge origin/main`` is always non-fast-forward, and a merely-staged
    returning file aborts that merge — committing absorbs it. HEAD may advance (the
    coordinator branch is disposable, never merged anywhere) and only ``.shiki`` is
    committed.
    """
    try:
        return _execute_action_body(target, goal_id, decision, repair_limit=repair_limit)
    finally:
        _commit_coordinator_mirror(target)


def _execute_action_body(target: Path, goal_id: str, decision: dict[str, Any], *, repair_limit: int) -> dict[str, Any]:
    action = decision["action"]
    task_id = decision.get("task_id")
    result: dict[str, Any] = {"action": action, "task_id": task_id, "reason": decision.get("reason")}

    # A re-opened task's decision may carry ``clear_expected_pr``: its ``expected_pr``
    # names a previous cycle's already-merged PR (see ``decide_task_action``). Drop the
    # stale pointer BEFORE the effector runs so the fresh implementation's ``create_pr``
    # opens a new PR instead of the loop reading the merged pointer as this cycle's
    # completed work. Handled here (before the wait/stop early return) so the single
    # coordinator-mirror commit in ``execute_action`` persists it.
    if decision.get("clear_expected_pr") and task_id:
        _clear_stale_expected_pr(target, task_id)

    if action in WAIT_ACTIONS or action in STOP_ACTIONS or action == "goal_complete":
        if action == "goal_complete":
            # The completing task's closeout PR already pushed goal=complete (with
            # the scorecard report + completion ledger) to main (ADR 0012). Sync the
            # coordinator mirror to main's authoritative state for THIS goal only —
            # never a whole-tree checkout, which would revert unrelated in-flight
            # goals' files. The completing goal's task files are left byte-identical
            # to main so a later `git merge origin/main` resolves cleanly (a non-fast-
            # forward merge — the coordinator carries its own mirror commits — but a
            # clean one because the goal's files already match main). Do NOT re-run
            # cmd_goal_complete (duplicate scorecard).
            result.update(_sync_goal_complete_mirror(target, goal_id))
            result["goal_status"] = "complete"
        return result

    task = load_task(target, task_id)
    if action == "dispatch":
        # A (re-)dispatch produces fresh reviewed work, so the pre-PR degenerate-
        # review retry budget must not carry over onto it (it bounds retries of the
        # SAME reviewed change, never new work).
        _reset_pre_pr_review_retries(target, task_id)
        result["returncode"] = _dispatch(target, task)
    elif action == "redispatch":
        # A `running` task whose session lease is gone (free/absent): the session
        # died mid-work and left the task stranded. Reset it to `ready`, record
        # the attempt on the task (a monotonically increasing `dispatch_attempts`
        # the decision reads to enforce the bound), and dispatch a fresh session.
        _reset_pre_pr_review_retries(target, task_id)
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
        if review.get("status") == "retry":
            # The reviewer returned a blocking verdict naming zero findings and the
            # retry budget still allows another attempt. Wait and re-run create_pr
            # (hence the reviewer) next pass rather than stopping or opening a PR on
            # a verdict that named nothing — the reviewed work is unchanged, so a
            # re-run may produce a usable or clean verdict. NEVER a clean pass.
            result["action"] = "wait_review"
            result["reason"] = review.get("reason")
            return result
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
                mergegate_blocking_reasons=decision.get("mergegate_blocking_reasons"),
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
        # CHANGE THE DISCRIMINATOR — head movement alone does NOT prove delivery.
        # ``_commit_and_push_implementation`` ``git add -A``s the whole worktree, so
        # the advance above can be pure loop bookkeeping: the runner dispatch's
        # ``sync_contract_into_worktree`` rewrites the task's own mirror record (and
        # its lock/goal) in the worktree, which commits and moves the head even when
        # the repairer changed no source. That is the PR #310 repro — three attempts,
        # three identical ``.shiki/tasks/<id>.json | 5 +++--`` commits, zero source,
        # stopping only at ``stop_guardian`` at the limit. A repair DELIVERS only when
        # it changes something the IMPLEMENTER owns, so judge delivery by the pushed
        # commit's file set with the loop's own bookkeeping removed (NARROW — the
        # task's own mirror record and the per-cycle contract-mirror sync, never all
        # of ``.shiki``). Same stop action / reason shape / position as the guard
        # above; only the discriminator changes. ``None`` means the per-push delta
        # could not be scoped (a first push carrying the whole branch, not a no-op)
        # — proceed; an EMPTY list means the head moved on bookkeeping alone — stop.
        implementer_changes = _repair_implementer_changes(target, task_id, head_before, head_after)
        if implementer_changes is not None and not implementer_changes:
            result["action"] = "stop_blocked"
            result["reason"] = (
                "repair produced no implementer change: the PR head advanced past "
                f"{head_before} ({impl}) only with loop-authored bookkeeping — the "
                "task's own mirror record — not a fix the implementer owns, so its "
                "required checks cannot turn green and the repair did not fix the "
                "failure — diagnose or re-dispatch"
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
    elif action == "sync_branch":
        # BEHIND its base: merge the base in and push (never rebase/force-push). On
        # success the loop returns to waiting for the re-run checks; a conflict or push
        # failure stops with a NAMED reason. Resolve the base the module-standard way
        # (a refreshed origin/<name>) then hand _sync_branch the bare branch NAME: it
        # builds `origin/{base}` itself, so passing `origin/main` would produce
        # `origin/origin/main`. Falls back to the bare name when there is no origin/
        # prefix (the _default_base_ref offline fallback).
        base_ref = _default_base_ref(target)
        base_name = base_ref[len("origin/"):] if base_ref.startswith("origin/") else base_ref
        result.update(_sync_branch(target, task, base=base_name))
        if result.get("action") == "sync_branch":
            # A successful base sync is a bounded remediation. Record the attempt on
            # the task (mirrors cca_rerun_count / MAX_CCA_RERUNS) so the decision
            # engine caps re-syncs and the repair limit stays reachable. No repair
            # packet is created and repair_attempts is untouched — a sync never
            # consumes a repair attempt.
            task = load_task(target, task_id)
            task["base_sync_attempts"] = int(task.get("base_sync_attempts") or 0) + 1
            _save_task(target, task)
            result["base_sync_attempts"] = task["base_sync_attempts"]
            # _sync_branch pushed the new head from a throwaway detached checkout, so
            # the task's REGISTERED worktree still has its local branch at the pre-sync
            # head. Because a sync can now be followed by a REPAIR (not only a merge),
            # fast-forward that worktree so the repair's push is not a non-fast-forward
            # (never a reset: uncommitted work is preserved).
            result["worktree_reconcile"] = _reconcile_registered_worktree_to_origin(target, task_id)
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
    pr_states: dict[str, dict[str, Any] | None] = {}
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
        elif task.get("status") == "ready" and task.get("expected_pr"):
            # A `ready` task normally has no PR yet. One that DOES carry an expected_pr
            # is a re-opened task (a Spec Amendment moved it back to ready) whose
            # pointer may still name the previous cycle's — now merged — PR. Snapshot it
            # so the pure engine can spot a stale merged pointer and clear it before
            # cutting a fresh implementation (reading it as done would drive a closeout
            # with no source change). Only the merge bit is needed; checks are ignored
            # for a ready task, and a fresh ready task (no expected_pr) makes no call.
            pr_state, _ = snapshot_pr(target, task)
        elif task.get("status") == "running":
            # Probe the OS lease so a stranded `running` task (session died) is
            # distinguished from a live one instead of waiting on it forever.
            lease_state = session_lease_state(target, str(task.get("id")))
        pr_states[str(task.get("id"))] = pr_state
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
    # Attach the MergeGate gate's own blocking reasons to a chosen repair dispatch so
    # the repair packet + handoff name the actual blockers, not just the failing
    # check. Resolved read-only from durable CI evidence and INFORMATIVE ONLY: it
    # never raises, degrades to no reasons on any failure, and never alters the
    # decision the pure engine already made (this runs strictly after decide_*).
    if decision.get("action") == "dispatch_repair":
        repair_task_id = str(decision.get("task_id"))
        repair_task = load_task(target, repair_task_id)
        repair_pr_state = pr_states.get(repair_task_id)
        if repair_pr_state is None:
            # A task that entered the pass already `repair-needed` was not snapshotted
            # above; snapshot it now so the reasons bind to the current PR head.
            repair_pr_state, _ = snapshot_pr(target, repair_task)
        decision["mergegate_blocking_reasons"] = _resolve_blocking_reasons(
            target, repair_task, repair_pr_state
        )
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
        # A successful `sync_branch` is a forward action, but the loop must then
        # WAIT: the base merge produced a new head whose required checks GitHub has
        # not re-queued yet. Pause like a wait so the next pass re-verifies the new
        # head (and lets `mergeStateStatus` settle) instead of re-snapshotting the
        # just-pushed head at zero delay and racing GitHub's eventual consistency.
        if result["action"] in WAIT_ACTIONS or result["action"] == "sync_branch":
            time.sleep(args.interval)
    print_json({"goal_id": args.goal_id, "outcome": "max-cycles", "cycles": args.max_cycles, "history": history})
    return 1
