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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-json", required=True)
    parser.add_argument("--guardian-policy", required=True)
    parser.add_argument("--guardian-comments", default="")
    parser.add_argument("--guardian-events", default="")
    parser.add_argument("--guardian-timeline", default="")
    parser.add_argument("--expected-repository", default="")
    parser.add_argument("--shiki-root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    pr = _load_json(args.pr_json)
    if not isinstance(pr, dict):
        signal = {"required": True, "approved": False, "error": "pr.json missing or invalid"}
        Path(args.output).write_text(json.dumps(signal, indent=2) + "\n", encoding="utf-8")
        return 0

    # Authoritative risk comes from the task resolved by the PR-body task id —
    # exactly how the MergeGate policy check resolves it. When the risk cannot be
    # determined (no task id in the body, missing/corrupt task file, or no
    # risk_level), FAIL CLOSED: require Guardian approval rather than let an
    # undetermined risk collapse to "not required" (which would let the CCA treat
    # CCA-08 as not applicable for an unapproved high/critical PR).
    head_sha = str(pr.get("headRefOid") or "")
    task_risk = _resolve_task_risk_level(args.shiki_root, str(pr.get("body") or ""))
    risk_unknown = task_risk is None
    # PR labels may only ESCALATE risk; they can never downgrade the task's risk
    # and are ignored when the task risk is undetermined (fail closed wins).
    labels = _label_names(pr) + ([task_risk, f"risk:{task_risk}"] if task_risk else [])

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
        }
        Path(args.output).write_text(json.dumps(signal, indent=2) + "\n", encoding="utf-8")
        return 0

    if not required:
        # Risk is KNOWN and below the Guardian threshold (low/medium).
        signal = {
            "required": False,
            "approved": True,
            "sources": [],
            "ai_reviewers": [],
            "approvers": [],
            "head_sha": head_sha,
            "note": f"Guardian approval not required for risk level {task_risk!r}",
        }
        Path(args.output).write_text(json.dumps(signal, indent=2) + "\n", encoding="utf-8")
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
    }
    Path(args.output).write_text(json.dumps(signal, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


# Test-friendly alias: call with an explicit argv list.
main_with_argv = main


if __name__ == "__main__":
    raise SystemExit(main())
