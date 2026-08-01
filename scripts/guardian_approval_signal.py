#!/usr/bin/env python3
"""Emit a deterministic Guardian-approval signal for the CCA judge.

The CCA completion judge must not decide Guardian approval by interpreting raw
PR comments with an LLM — "LLM outputs may vary. State transitions must not
vary." This helper runs the SAME authoritative ``evaluate_guardian_approval``
used by the MergeGate policy check against the live PR comments/events, and
writes a small JSON signal the CCA reads for CCA-08.

It never approves on its own: it reports whether a recorded authority (human
label/review/comment OR an external AI guardian review artifact, ADR 0010)
approved the exact current head. The MergeGate policy check remains the
authoritative gate; this only lets the CCA see the same result deterministically.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from shiki_guardian import (
    GuardianPolicyError,
    evaluate_guardian_approval,
    load_guardian_policy_file,
    risk_requires_guardian,
    validate_guardian_policy,
)

# The bookkeeping-closeout exemption (ADR 0017) must resolve risk IDENTICALLY on
# this signal path and on the MergeGate policy check. Importing and calling the
# SAME classifier — never a second copy of its six conditions — is what keeps the
# two Guardian decision points from diverging. ``parse_changed_files_status``
# comes from the same module so the diff is parsed exactly as MergeGate parses it.
from mergegate_check import is_bookkeeping_closeout, parse_changed_files_status

# The canonical Shiki task-id pattern, identical to the MergeGate policy check's
# TASK_ID. The signal MUST resolve risk the same way MergeGate does so the two
# gates never diverge.
_ID_SUFFIX = r"(?:[0-9]{4,}|[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8})"
_TASK_ID_RE = re.compile(rf"\bT-{_ID_SUFFIX}\b")


def _load_json(path: str) -> Any:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _as_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _label_names(pr: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for label in pr.get("labels") or []:
        if isinstance(label, dict):
            label = label.get("name")
        if label:
            names.append(str(label).strip().lower())
    return names


def _builtin_high_or_critical(labels: list[str]) -> bool:
    normalized = {label.removeprefix("risk:") for label in labels}
    return bool(normalized.intersection({"high", "critical"}))


def _resolve_task_risk_level(shiki_root: str, pr_body: str) -> str | None:
    """Resolve the task risk the SAME way the MergeGate policy check does: by the
    first Shiki task id in the PR body (not by an expected_pr glob, which fails
    open on a null/stale/string expected_pr or a corrupt task file).

    Returns the lowercase ``risk_level`` of the resolved task, or ``None`` when it
    cannot be determined — in which case the caller MUST fail closed (require
    Guardian approval) rather than letting an undetermined risk collapse to
    "not required".
    """
    match = _TASK_ID_RE.search(pr_body or "")
    if not match:
        return None
    task = _load_json(str(Path(shiki_root) / ".shiki" / "tasks" / f"{match.group(0)}.json"))
    if not isinstance(task, dict):
        return None
    risk = str(task.get("risk_level") or "").strip().lower()
    return risk or None


def _parse_merged_prs(value: str) -> set[int]:
    """Parse the comma-separated ``--merged-prs`` list exactly as MergeGate does."""
    return {int(token.strip()) for token in str(value or "").split(",") if token.strip().isdigit()}


def _load_changed_files_status(path: str) -> list | None:
    """Parse the git ``--name-status`` changed-files file into ChangedFile entries.

    Returns ``None`` when the path is empty or the file is absent, so the caller
    FAILS CLOSED (no exemption) rather than classifying against an assumed diff. A
    real-but-empty status list is intentionally NOT ``None``: the classifier will
    simply find no terminal transitions and reject it.
    """
    if not path:
        return None
    status_path = Path(path)
    if not status_path.exists():
        return None
    return parse_changed_files_status(status_path, [])


def _bookkeeping_closeout_exemption(
    *,
    shiki_root: str,
    pr_body: str,
    base_shiki: str,
    changed_files_status: str,
    merged_prs: str,
) -> bool:
    """Whether this PR is a proven ADR 0017 bookkeeping closeout.

    The entire decision is delegated to the SAME ``is_bookkeeping_closeout``
    classifier the MergeGate policy check calls, over the SAME inputs (the base
    ``.shiki`` snapshot, the changed-files status, and the merged-PR proof). Reusing
    the one classifier — never a second copy of its six conditions — is what keeps
    this signal and MergeGate from diverging.

    FAILS CLOSED (returns ``False``, i.e. no exemption) on every axis: no task id
    in the PR body, an unreadable/missing head task file, or a missing changed-files
    status file returns ``False`` here; a missing/unreadable base snapshot,
    unresolvable implementation PR, or any disqualifying diff returns ``False`` from
    the classifier itself. The classifier never reads the PR, a label, or a marker,
    so only durable evidence can grant the exemption.
    """
    match = _TASK_ID_RE.search(pr_body or "")
    if not match:
        return False
    task_id = match.group(0)
    target = Path(shiki_root)
    task = _load_json(str(target / ".shiki" / "tasks" / f"{task_id}.json"))
    if not isinstance(task, dict):
        return False
    files_status = _load_changed_files_status(changed_files_status)
    if files_status is None:
        return False
    return is_bookkeeping_closeout(
        target=target,
        task=task,
        task_id=task_id,
        goal_id=str(task.get("goal_id") or ""),
        base_shiki=Path(base_shiki) if base_shiki else None,
        changed_files_status=files_status,
        merged_pr_numbers=_parse_merged_prs(merged_prs),
    )


def _contract_approval_result(
    *,
    shiki_root: str,
    pr_body: str,
    base_shiki: str,
    changed_files_status: str,
    contract_approval: str,
) -> Any:
    """Evaluate ADR 0015 Contract Approval for the CCA Guardian signal.

    The SAME OR that MergeGate applies must reach this deterministic signal: ADR
    0015 is explicit that without it, a contract-approved PR still returns
    ``needs_guardian``, the CCA job fails, and the MergeGate policy check never
    runs — the gate this change moves would never be reached. This resolves the
    task the SAME way MergeGate does (the first Shiki task id in the PR body),
    loads the head/base task snapshots and the registration proof, and calls the
    SAME ``evaluate_contract_approval`` MergeGate calls.

    Returns a ``ContractApprovalResult`` or None. None (does not apply) on every
    axis: no ``--contract-approval`` proof, an unreadable proof, no task id in the
    body, a missing/unreadable head task, or the evaluator module being absent
    (ImportError) — so a signal invocation without the flag behaves byte-for-byte
    as before. The evaluator is imported LAZILY so running/importing this script
    never hard-requires the ``shiki_contract_approval`` module.
    """
    registration = _load_json(contract_approval) if contract_approval else None
    if not isinstance(registration, dict):
        return None
    match = _TASK_ID_RE.search(pr_body or "")
    if not match:
        return None
    task_id = match.group(0)
    head_task = _load_json(str(Path(shiki_root) / ".shiki" / "tasks" / f"{task_id}.json"))
    if not isinstance(head_task, dict):
        return None
    files_status = _load_changed_files_status(changed_files_status) or []
    base_task = _load_json(str(Path(base_shiki) / "tasks" / f"{task_id}.json")) if base_shiki else None
    try:
        from shiki_contract_approval import evaluate_contract_approval
    except ImportError:
        return None
    return evaluate_contract_approval(
        task_id=task_id,
        base_task=base_task if isinstance(base_task, dict) else None,
        head_task=head_task,
        changed_files_status=files_status,
        registration=registration,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-json", required=True)
    parser.add_argument("--guardian-policy", required=True)
    parser.add_argument("--guardian-comments", default="")
    parser.add_argument("--guardian-events", default="")
    parser.add_argument("--guardian-timeline", default="")
    parser.add_argument("--expected-repository", default="")
    parser.add_argument("--shiki-root", default=".")
    # ADR 0017 bookkeeping-closeout exemption inputs. All default to values that
    # grant NO exemption, so an invocation that omits them behaves exactly as
    # before (require Guardian approval from the task's real risk).
    parser.add_argument(
        "--base-shiki",
        default="",
        help="Path to the base branch's .shiki snapshot; required for the ADR 0017 closeout exemption.",
    )
    parser.add_argument(
        "--changed-files-status",
        default="",
        help="git --name-status changed-files file; required for the ADR 0017 closeout exemption.",
    )
    parser.add_argument(
        "--merged-prs",
        default="",
        help="Comma-separated PR numbers proven merged; the implementation PR must be here for the exemption.",
    )
    # ADR 0015 Contract Approval input. Defaults to "" (no proof), so an
    # invocation that omits it reports exactly as before (require a live Guardian
    # approval from the task's real risk).
    parser.add_argument(
        "--contract-approval",
        default="",
        help=(
            "Path to the ADR 0015 Contract Approval registration proof (JSON). When present and valid, a "
            "normal task PR whose contract was registered and Guardian-approved before dispatch reports the "
            "Guardian requirement satisfied with a contract_approval source. Absent/missing/unreadable => no effect."
        ),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    pr = _load_json(args.pr_json)
    if not isinstance(pr, dict):
        signal = {
            "required": True,
            "approved": False,
            "error": "pr.json missing or invalid",
            "bookkeeping_closeout_exemption": False,
        }
        Path(args.output).write_text(json.dumps(signal, indent=2) + "\n", encoding="utf-8")
        return 0

    # Authoritative risk comes from the task resolved by the PR-body task id —
    # exactly how the MergeGate policy check resolves it. When the risk cannot be
    # determined (no task id in the body, missing/corrupt task file, or no
    # risk_level), FAIL CLOSED: require Guardian approval rather than let an
    # undetermined risk collapse to "not required" (which would let the CCA treat
    # CCA-08 as not applicable for an unapproved high/critical PR).
    head_sha = str(pr.get("headRefOid") or "")
    pr_body = str(pr.get("body") or "")
    task_risk = _resolve_task_risk_level(args.shiki_root, pr_body)

    # ADR 0017: a PROVEN bookkeeping closeout carries no implementation and
    # inherits no new risk, so at this single Guardian decision point the task's
    # risk is evaluated as if low — IDENTICALLY to the MergeGate policy check,
    # which calls the same is_bookkeeping_closeout classifier over the same inputs.
    # It fails closed to "no exemption" whenever any input is absent or unbuildable,
    # so a missing base snapshot / changed-files status / merge proof requires
    # Guardian approval exactly as before.
    exemption = _bookkeeping_closeout_exemption(
        shiki_root=args.shiki_root,
        pr_body=pr_body,
        base_shiki=args.base_shiki,
        changed_files_status=args.changed_files_status,
        merged_prs=args.merged_prs,
    )
    # The risk that actually gates Guardian approval: "low" for a proven closeout,
    # otherwise the real task risk. task_risk is still reported as risk_level for
    # audit. PR labels may only ESCALATE the effective risk (a maintainer can force
    # the gate even on an exempted PR) and are ignored when the effective risk is
    # undetermined (fail closed wins) — exactly as in MergeGate's
    # _guardian_risk_labels.
    effective_risk = "low" if exemption else task_risk
    risk_unknown = effective_risk is None
    labels = _label_names(pr) + ([effective_risk, f"risk:{effective_risk}"] if effective_risk else [])

    try:
        policy = load_guardian_policy_file(Path(args.guardian_policy))
    except GuardianPolicyError as error:
        # Fail closed: an undetermined risk or an unreadable policy on a
        # high/critical PR must never let the CCA see approval.
        required = risk_unknown or _builtin_high_or_critical(labels)
        signal = {
            "required": required,
            "approved": not required,
            "error": f"guardian policy unreadable: {error}",
            "head_sha": head_sha,
            "bookkeeping_closeout_exemption": exemption,
        }
        Path(args.output).write_text(json.dumps(signal, indent=2) + "\n", encoding="utf-8")
        return 0

    policy_errors = validate_guardian_policy(policy)
    required = risk_unknown or _builtin_high_or_critical(labels) or risk_requires_guardian(labels, policy)
    if policy_errors and required:
        signal = {
            "required": True,
            "approved": False,
            "error": "; ".join(policy_errors),
            "head_sha": head_sha,
            "bookkeeping_closeout_exemption": exemption,
        }
        Path(args.output).write_text(json.dumps(signal, indent=2) + "\n", encoding="utf-8")
        return 0

    if not required:
        # Risk is KNOWN and below the Guardian threshold (low/medium, or a proven
        # bookkeeping closeout evaluated as if low).
        note = f"Guardian approval not required for risk level {task_risk!r}"
        if exemption:
            note += " (ADR 0017 bookkeeping-closeout exemption applied)"
        signal = {
            "required": False,
            "approved": True,
            "sources": [],
            "ai_reviewers": [],
            "approvers": [],
            "head_sha": head_sha,
            "bookkeeping_closeout_exemption": exemption,
            "note": note,
        }
        Path(args.output).write_text(json.dumps(signal, indent=2) + "\n", encoding="utf-8")
        return 0

    # ADR 0015 Contract Approval OR-branch. The Guardian requirement is satisfied
    # by EITHER a live PR approval (below) OR a proven Contract Approval — the SAME
    # OR the MergeGate policy check applies, so the two Guardian decision points
    # never diverge. When it applies, report the requirement satisfied with the
    # ``contract_approval`` source the CCA prompt reads, and DO NOT consult the
    # live-approval evidence (the approval was recorded on the merged Contract PR).
    # Absent/invalid proof => None => the live-approval path below runs unchanged.
    contract = _contract_approval_result(
        shiki_root=args.shiki_root,
        pr_body=pr_body,
        base_shiki=args.base_shiki,
        changed_files_status=args.changed_files_status,
        contract_approval=args.contract_approval,
    )
    if contract is not None and contract.applies:
        signal = {
            "required": True,
            "approved": True,
            "sources": list(contract.sources),
            "ai_reviewers": [],
            "approvers": [],
            "head_sha": head_sha,
            "expected_repository": args.expected_repository,
            "risk_level": task_risk,
            "risk_determined": not risk_unknown,
            "bookkeeping_closeout_exemption": exemption,
            "contract_approval": True,
            "note": (
                "Guardian approval satisfied by Contract Approval (ADR 0015): the task contract was "
                "registered and Guardian-approved before dispatch"
            ),
        }
        Path(args.output).write_text(json.dumps(signal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0

    comments = _as_list(_load_json(args.guardian_comments))
    events = _as_list(_load_json(args.guardian_events))
    timeline = _as_list(_load_json(args.guardian_timeline))
    reviews = [r for r in pr.get("reviews") or [] if isinstance(r, dict)]

    result = evaluate_guardian_approval(
        policy=policy,
        pr=pr,
        reviews=reviews,
        comments=comments,
        label_events=events + timeline,
        head_sha=head_sha,
        expected_repo=args.expected_repository,
    )

    signal = {
        "required": True,
        "approved": bool(result.approved),
        "sources": list(result.sources),
        "ai_reviewers": list(result.ai_reviewers),
        "approvers": list(result.approvers),
        "blockers": list(result.blockers),
        "warnings": list(result.warnings),
        "head_sha": head_sha,
        "expected_repository": args.expected_repository,
        "risk_level": task_risk,
        "risk_determined": not risk_unknown,
        "bookkeeping_closeout_exemption": exemption,
    }
    Path(args.output).write_text(json.dumps(signal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


# Test-friendly alias: call with an explicit argv list.
main_with_argv = main


if __name__ == "__main__":
    raise SystemExit(main())
