#!/usr/bin/env python3
"""External AI Guardian Review adapter contract (ADR 0010 / ADR 0014).

Deterministic Shiki-side surfaces consumed by the Codex App External AI Guardian
UI Adapter:

- ``build_review_packet`` assembles a review-INPUT packet from a task contract
  and Codex-gathered PR evidence;
- ``classify_review_focus`` injects PR-type-specific review focus areas;
- ``build_review_prompt`` renders the deterministic GPT Pro reviewer prompt;
- ``extract_review_response`` / ``verify_review_response`` parse the reviewer
  output and validate any approval artifact against the same approval contract
  the PR-comment path enforces (``shiki_guardian.validate_ai_review_artifact``).

Role boundary (ADR 0010 / ADR 0014): Claude Code implements these DETERMINISTIC
contracts only. Codex App is the UI adapter that drives ChatGPT Pro; GPT Pro is
the external Guardian Authority; GitHub carries the live ``external-ai-guardian-
review`` artifact; MergeGate verifies. Nothing in this module drives a ChatGPT
UI, and nothing here lets a Claude Code implementer self-approve its own PR: the
packet is review *input* (never approval evidence), and approval is only the
validated fenced artifact emitted by the external reviewer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from shiki_contracts import CANONICAL_EXTERNAL_AI_GUARDIAN_REVIEW_PACKET_SCHEMA_PATH
from shiki_guardian import (
    GUARDIAN_POLICY_PATH,
    GuardianPolicy,
    _extract_ai_review_artifacts,
    load_guardian_policy_file,
    validate_ai_review_artifact,
)
from shiki_jsonschema import JsonSchemaError, UnsupportedJsonSchemaError, validate_json_schema
from shiki_process import ShikiError, print_json, read_json, shiki_path, target_path, utc_now
from shiki_tasks import load_task, require_github_first_target

PACKET_KIND = "external_ai_guardian_review_packet"
APPROVAL_ARTIFACT_KIND = "external_ai_guardian_review"
REVIEW_VERDICTS = ("approve", "request_changes", "insufficient_evidence")
# Where each parsed reviewer verdict routes in Shiki. ``approve`` only routes to
# autonomous merge when its artifact also validates; non-approval verdicts route
# to bounded repair / evidence work, never to direct implementation changes.
VERDICT_ROUTES = {
    "approve": "autonomous_merge",
    "request_changes": "repair_packet",
    "insufficient_evidence": "evidence_packet",
}
REJECTED_ROUTE = "rejected"


# --------------------------------------------------------------------------- #
# PR-type review focus classifier
# --------------------------------------------------------------------------- #

# Ordered (category_key, path_markers, scope_markers, focus_areas). Conservative:
# matching is substring-based and a PR can match several categories at once.
_FOCUS_CATEGORIES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "guardian_authority",
        ("shiki_guardian", "guardian_approval", "guardian-policy", "mergegate", "cca", "guardian_review"),
        ("guardian", "mergegate", "merge gate", "cca", "approval", "authority", "verdict"),
        (
            "Guardian/MergeGate/CCA authority: confirm no approval path is weakened, no self-approval or identity spoofing is introduced, and head-SHA / repo / PR binding stays intact.",
            "Confirm MergeGate remains the authoritative state-transition gate and is not bypassed.",
        ),
    ),
    (
        "github_permissions",
        ("shiki_github", "actions/permissions", ".github/workflows", "branch_protection", "workflow_permission"),
        ("permission", "can_approve", "workflow permission", "branch protection", "token", "secret", "scope"),
        (
            "GitHub API / repository or workflow permission changes: confirm least-privilege, no secret/token exposure, and no unintended grant of approve/merge permission.",
        ),
    ),
    (
        "runtime_adapter",
        ("shiki_runtime", "runner", "_adapter", "runtime_registry", "shiki_guardian_review"),
        ("runner", "adapter", "claude code", "codex", "runtime", "dispatch", "handoff"),
        (
            "Runner / runtime adapter changes: confirm the implementer/approver role boundary holds and no runtime can self-route Guardian approval for its own work.",
        ),
    ),
    (
        "memory_loop",
        ("shiki_memory", ".shiki/memories", "memory"),
        ("memory loop", "distilled", "memory"),
        (
            "Memory Loop changes: confirm captured/distilled rules cannot silently alter gate behavior and promotion/revocation stays auditable.",
        ),
    ),
    (
        "shiki_state",
        (".shiki/schemas", ".shiki/ledger", "shiki_state", "state_class", ".shiki/"),
        ("schema", "ledger", "state class", "mirror", "contract"),
        (
            ".shiki schema / ledger / state-class changes: confirm contracts stay backward-compatible, ledger evidence stays append-only, and no trusted state is shaped by the PR under review.",
        ),
    ),
    (
        "workflow_ci",
        (".github/workflows", "ci.yml", ".nvmrc", "package.json", "package-lock.json", "validate_shiki", "test_shiki_"),
        ("workflow", "ci", "node", "runtime version", "pipeline"),
        (
            "Workflow / CI / Node runtime changes: confirm required checks still run and gate, and CI changes do not relax verification.",
        ),
    ),
)

_DOC_SUFFIXES = (".md", ".rst", ".txt")
_DOC_PREFIXES = ("docs/", "docs/adr/")

_CONSERVATIVE_FOCUS = (
    "Authority & evidence (conservative default): verify every acceptance check maps to durable evidence, the role boundary is preserved, and approval requires the validated external-ai-guardian-review artifact bound to this exact repo / PR / head SHA.",
)


def _is_docs_only(changed_files: list[str]) -> bool:
    files = [f for f in changed_files if f]
    if not files:
        return False
    for path in files:
        lowered = path.lower()
        if lowered.endswith(_DOC_SUFFIXES) or lowered.startswith(_DOC_PREFIXES):
            continue
        return False
    return True


def classify_review_focus(
    changed_files: list[str],
    scope: str,
    risk_level: str,
) -> list[str]:
    """Deterministically derive PR-type review focus areas.

    Conservative by design: every matching category contributes focus areas, and
    an unknown / mixed / high-or-critical-risk change always adds broad
    authority/evidence checks so an unclassified PR is never under-reviewed.
    """
    haystack_paths = " ".join(f.lower() for f in changed_files if f)
    scope_text = (scope or "").lower()
    focus: list[str] = []
    matched = 0
    for _key, path_markers, scope_markers, areas in _FOCUS_CATEGORIES:
        if any(marker in haystack_paths for marker in path_markers) or any(
            marker in scope_text for marker in scope_markers
        ):
            matched += 1
            for area in areas:
                if area not in focus:
                    focus.append(area)

    docs_only = _is_docs_only(changed_files)
    if docs_only and matched == 0:
        focus.append(
            "Docs/ADR-only change: confirm the change is genuinely documentation/decision-only, introduces no behavior or contract drift, and stays consistent with the constitution and existing ADRs."
        )

    # Conservative augmentation: unknown (no category and not docs-only), mixed
    # (more than one category), or high/critical risk always gets broad checks.
    risk = (risk_level or "").strip().lower()
    if matched == 0 and not docs_only:
        focus.append(
            "Unclassified change: no known category matched; apply broad authority, evidence, scope, and regression review."
        )
    if matched > 1 or (matched == 0 and not docs_only) or risk in {"high", "critical"}:
        for area in _CONSERVATIVE_FOCUS:
            if area not in focus:
                focus.append(area)
    return focus


# --------------------------------------------------------------------------- #
# Review packet builder
# --------------------------------------------------------------------------- #

def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float)) and str(item).strip()]


def _check_results(value: Any) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    if not isinstance(value, list):
        return results
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        status = str(item.get("status") or item.get("conclusion") or "").strip()
        if name and status:
            results.append({"name": name, "status": status})
    return results


def _derive_missing_evidence(
    *,
    check_results: list[dict[str, str]],
    diff_summary: str,
    implementer_report: dict[str, str],
    acceptance_checks: list[str],
) -> list[str]:
    """Deterministically flag absent baseline evidence for the reviewer."""
    missing: list[str] = []
    if not check_results:
        missing.append("No check results were supplied in the packet.")
    if not diff_summary.strip():
        missing.append("No diff/patch summary was supplied in the packet.")
    if not implementer_report.get("summary", "").strip():
        missing.append("No implementer report summary was supplied in the packet.")
    if not acceptance_checks:
        missing.append("The task contract declares no acceptance checks.")
    return missing


def build_review_packet(
    *,
    task: dict[str, Any],
    pr_data: dict[str, Any],
    implementer_report: dict[str, Any] | None = None,
    relevant_docs: list[str] | None = None,
    generated_at: str,
    source_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble an External AI Guardian Review Packet (review INPUT only).

    ``task`` is a loaded Shiki task contract; ``pr_data`` is the Codex App-
    gathered PR evidence (repository, pr_number, base_sha, head_sha, pr_summary,
    changed_files, diff_summary, check_results). The packet is NOT approval
    evidence and must not be committed by the PR under review.
    """
    repository = str(pr_data.get("repository") or "").strip()
    pr_number = pr_data.get("pr_number")
    if not isinstance(pr_number, int):
        raise ShikiError("pr_data.pr_number must be an integer")
    scope = str(task.get("scope") or "")
    risk_level = str(task.get("risk_level") or "").strip()
    changed_files = _string_list(pr_data.get("changed_files"))
    diff_summary = str(pr_data.get("diff_summary") or "")
    check_results = _check_results(pr_data.get("check_results"))
    acceptance_checks = _string_list(task.get("acceptance_checks"))

    report = implementer_report or {}
    report_obj = {
        "source": str(report.get("source") or "unspecified"),
        "summary": str(report.get("summary") or ""),
    }
    if report.get("ref"):
        report_obj["ref"] = str(report.get("ref"))

    packet = {
        "kind": PACKET_KIND,
        "not_approval_evidence": True,
        "repository": repository,
        "pr_number": pr_number,
        "base_sha": str(pr_data.get("base_sha") or ""),
        "head_sha": str(pr_data.get("head_sha") or ""),
        "goal_id": str(task.get("goal_id") or ""),
        "task_id": str(task.get("id") or ""),
        "risk_level": risk_level,
        "scope": scope,
        "non_goals": _string_list(task.get("non_goals")),
        "acceptance_checks": acceptance_checks,
        "locks": _string_list(task.get("locks")),
        "pr_summary": str(pr_data.get("pr_summary") or ""),
        "changed_files": changed_files,
        "diff_summary": diff_summary,
        "check_results": check_results,
        "implementer_report": report_obj,
        "relevant_docs": list(relevant_docs or []),
        "review_focus_areas": classify_review_focus(changed_files, scope, risk_level),
        "known_missing_evidence": _derive_missing_evidence(
            check_results=check_results,
            diff_summary=diff_summary,
            implementer_report=report_obj,
            acceptance_checks=acceptance_checks,
        ),
        "packet_generated_at": generated_at,
        "packet_source_refs": list(source_refs or []),
    }
    return packet


def packet_schema_path(target: Path) -> Path:
    return target / CANONICAL_EXTERNAL_AI_GUARDIAN_REVIEW_PACKET_SCHEMA_PATH


def validate_packet(packet: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate a packet against the packet schema; raise ShikiError on failure."""
    try:
        validate_json_schema(packet, schema)
    except (JsonSchemaError, UnsupportedJsonSchemaError) as error:
        raise ShikiError(
            f"External AI Guardian Review Packet failed schema validation: {error}"
        ) from error


# --------------------------------------------------------------------------- #
# GPT Pro reviewer prompt builder
# --------------------------------------------------------------------------- #

def _bullets(items: list[str], *, empty: str = "(none)") -> str:
    items = [i for i in items if str(i).strip()]
    if not items:
        return f"  {empty}"
    return "\n".join(f"  - {item}" for item in items)


def _approval_artifact_template(packet: dict[str, Any], *, reviewer_model: str, reviewer_role: str) -> str:
    artifact = {
        "kind": APPROVAL_ARTIFACT_KIND,
        "reviewer": {"type": "ai_model", "model": reviewer_model, "role": reviewer_role},
        "repo": packet.get("repository", ""),
        "pr": packet.get("pr_number"),
        "head_sha": packet.get("head_sha", ""),
        "verdict": "approve",
        "merge_permission": "autonomous_merge_permitted",
        "not_operator_approval": True,
    }
    return json.dumps(artifact, indent=2)


def build_review_prompt(
    packet: dict[str, Any],
    *,
    reviewer_model: str,
    reviewer_role: str,
    fence: str = "external-ai-guardian-review",
) -> str:
    """Render the deterministic GPT Pro external Guardian review prompt.

    The prompt fixes the reviewer identity/role, mandates the three review stages
    and the three-way verdict, and instructs the reviewer to emit the fenced
    approval artifact ONLY when approving. The packet is the primary evidence;
    the GitHub connector is optional corroboration.
    """
    checks = [f"{c.get('name')}: {c.get('status')}" for c in packet.get("check_results", [])]
    lines = [
        f"You are {reviewer_model} acting as {reviewer_role}.",
        "Review this PR as an AI-implemented Shiki change.",
        "Do not treat yourself as the implementer.",
        "Do not record your verdict as human/operator approval.",
        "Your verdict, if approving, must be expressed as external_ai_guardian_review.",
        "",
        "Primary evidence is the attached External AI Guardian Review Packet below.",
        "Use the GitHub connector only to verify or challenge the packet, not as the sole source of context.",
        "If required evidence is missing, return insufficient_evidence or request_changes.",
        "The packet is review INPUT only; it is not approval evidence and was not committed as trusted state.",
        "",
        "Return exactly these sections:",
        "1. Evidence Review",
        "2. Adversarial Review",
        "3. Authority Verdict: approve | request_changes | insufficient_evidence",
        f"4. If and only if approve: a fenced {fence} JSON artifact",
        "",
        "=== External AI Guardian Review Packet ===",
        f"repository: {packet.get('repository', '')}",
        f"pr_number: {packet.get('pr_number')}",
        f"base_sha: {packet.get('base_sha', '')}",
        f"head_sha: {packet.get('head_sha', '')}",
        f"goal_id: {packet.get('goal_id', '')}",
        f"task_id: {packet.get('task_id', '')}",
        f"risk_level: {packet.get('risk_level', '')}",
        "",
        "Task scope:",
        f"  {packet.get('scope', '') or '(none)'}",
        "Non-goals:",
        _bullets(packet.get("non_goals", [])),
        "Acceptance checks:",
        _bullets(packet.get("acceptance_checks", [])),
        "Declared locks:",
        _bullets(packet.get("locks", [])),
        "",
        "PR summary:",
        f"  {packet.get('pr_summary', '') or '(none)'}",
        "Changed files:",
        _bullets(packet.get("changed_files", [])),
        "Diff/patch summary:",
        f"  {packet.get('diff_summary', '') or '(none)'}",
        "Check results:",
        _bullets(checks),
        "Implementer report:",
        f"  source: {packet.get('implementer_report', {}).get('source', '')}",
        f"  summary: {packet.get('implementer_report', {}).get('summary', '') or '(none)'}",
        "Relevant docs:",
        _bullets(packet.get("relevant_docs", [])),
        "Known missing evidence:",
        _bullets(packet.get("known_missing_evidence", [])),
        "",
        "=== PR-specific review focus areas ===",
        _bullets(packet.get("review_focus_areas", [])),
        "",
        "=== Approval artifact (emit ONLY if your verdict is approve) ===",
        "Return it in a fenced block exactly like this, bound to this PR:",
        f"```{fence}",
        _approval_artifact_template(packet, reviewer_model=reviewer_model, reviewer_role=reviewer_role),
        "```",
        "Do not emit the artifact for request_changes or insufficient_evidence.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Reviewer response / verdict extraction + verification
# --------------------------------------------------------------------------- #

def _extract_verdict(text: str) -> str | None:
    """Find the reviewer's Authority Verdict deterministically.

    Prefers an explicit ``Authority Verdict: <verdict>`` line; otherwise the last
    standalone verdict keyword in the text. Returns None when ambiguous.
    """
    lowered = text.lower()
    marker = "authority verdict"
    found: str | None = None
    idx = lowered.find(marker)
    if idx != -1:
        # Examine the remainder of the marker line + a little after.
        segment = lowered[idx: idx + 200]
        for verdict in REVIEW_VERDICTS:
            if verdict in segment:
                # No verdict keyword is a substring of another, so compare the
                # positions of any keywords present and keep whichever appears
                # first on/after the "Authority Verdict" marker line.
                pos = segment.find(verdict)
                if found is None or pos < segment.find(found):
                    found = verdict
        if found:
            return found
    # Fallback: last explicit occurrence anywhere.
    positions = {v: lowered.rfind(v) for v in REVIEW_VERDICTS}
    positions = {v: p for v, p in positions.items() if p != -1}
    if not positions:
        return None
    return max(positions, key=positions.get)


def _section_items(text: str, headings: tuple[str, ...]) -> list[str]:
    """Collect bullet lines that appear under any of the given headings."""
    items: list[str] = []
    lines = text.splitlines()
    capturing = False
    for line in lines:
        stripped = line.strip()
        low = stripped.lower()
        if any(low.startswith(h) for h in headings):
            capturing = True
            continue
        if capturing:
            if not stripped:
                continue
            if stripped.startswith(("-", "*", "•")):
                item = stripped.lstrip("-*• ").strip()
                if item:
                    items.append(item)
            elif low[:2].rstrip(".").isdigit() or low.endswith(":"):
                # A new numbered/section heading ends the current capture.
                capturing = False
    return items


def extract_review_response(text: str, *, fence: str = "external-ai-guardian-review") -> dict[str, Any]:
    """Parse a GPT Pro reviewer response into a structured, deterministic result.

    Returns ``{verdict, artifact, blocking_issues, missing_evidence,
    artifact_present}``. ``verdict`` is one of REVIEW_VERDICTS or None.
    """
    text = text or ""
    artifacts = [a for a in _extract_ai_review_artifacts(text, fence) if a.get("kind") == APPROVAL_ARTIFACT_KIND]
    return {
        "verdict": _extract_verdict(text),
        "artifact": artifacts[0] if artifacts else None,
        "artifact_present": bool(artifacts),
        "blocking_issues": _section_items(text, ("blocking", "request_changes", "changes requested", "issues")),
        "missing_evidence": _section_items(text, ("missing evidence", "insufficient", "missing:")),
    }


def verify_review_response(
    packet: dict[str, Any],
    response_text: str,
    *,
    policy: GuardianPolicy,
) -> dict[str, Any]:
    """Validate a reviewer response against the packet + approval policy.

    Approval is accepted ONLY when the verdict is ``approve`` AND a fenced
    external-ai-guardian-review artifact validates against this packet's repo /
    PR / head SHA and the policy's allowed reviewer model / role (reusing
    ``shiki_guardian.validate_ai_review_artifact``). ``request_changes`` and
    ``insufficient_evidence`` are surfaced distinctly and routed to bounded
    repair / evidence work; they never approve.

    Offline verification treats a *soft* violation (e.g. a stale/missing head
    SHA) on an ``approve`` verdict as a rejection: unlike the PR-comment path,
    there is no other approval source to promote against, so any binding gap
    fails closed (``route: rejected``).
    """
    extracted = extract_review_response(response_text, fence=policy.ai_review_fence)
    verdict = extracted["verdict"]
    result: dict[str, Any] = {
        "approved": False,
        "verdict": verdict,
        "route": REJECTED_ROUTE,
        "reasons": [],
        "reviewer_model": None,
        "artifact_present": extracted["artifact_present"],
        "blocking_issues": extracted["blocking_issues"],
        "missing_evidence": extracted["missing_evidence"],
        "bound_to": {
            "repository": packet.get("repository", ""),
            "pr_number": packet.get("pr_number"),
            "head_sha": packet.get("head_sha", ""),
        },
    }

    if verdict is None:
        result["reasons"].append("Could not determine an Authority Verdict from the reviewer response.")
        return result

    if verdict != "approve":
        # request_changes -> repair_packet, insufficient_evidence -> evidence_packet
        result["route"] = VERDICT_ROUTES.get(verdict, REJECTED_ROUTE)
        return result

    artifact = extracted["artifact"]
    if not artifact:
        result["reasons"].append(
            "Verdict is approve but no fenced external-ai-guardian-review artifact was present; approval rejected."
        )
        return result

    violations, soft = validate_ai_review_artifact(
        artifact,
        policy=policy,
        expected_repo=packet.get("repository", ""),
        pr_number=packet.get("pr_number"),
        head_sha=packet.get("head_sha", ""),
    )
    reasons = list(violations) + list(soft)
    if reasons:
        result["reasons"].extend(reasons)
        return result

    reviewer = artifact.get("reviewer") if isinstance(artifact.get("reviewer"), dict) else {}
    result["approved"] = True
    result["route"] = VERDICT_ROUTES["approve"]
    result["reviewer_model"] = str(reviewer.get("model") or "").strip()
    result["artifact"] = artifact
    return result


# --------------------------------------------------------------------------- #
# CLI surface (consumed by the Codex App adapter; never drives a ChatGPT UI)
# --------------------------------------------------------------------------- #

def _load_policy(target: Path) -> GuardianPolicy:
    return load_guardian_policy_file(target / GUARDIAN_POLICY_PATH)


def _reviewer_identity(policy: GuardianPolicy, args: argparse.Namespace) -> tuple[str, str]:
    model = getattr(args, "reviewer_model", None) or (policy.ai_review_allowed_models[0] if policy.ai_review_allowed_models else "")
    role = getattr(args, "reviewer_role", None) or (policy.ai_review_allowed_roles[0] if policy.ai_review_allowed_roles else "")
    if not model:
        raise ShikiError("no reviewer model configured (guardian policy ai_review_allowed_models is empty); pass --reviewer-model")
    if not role:
        raise ShikiError("no reviewer role configured (guardian policy ai_review_allowed_roles is empty); pass --reviewer-role")
    return model, role


def _emit_text(text: str, output: str | None) -> None:
    if output:
        Path(output).expanduser().write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    else:
        print(text)


def cmd_guardian_packet(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    task = load_task(target, args.task_id)
    pr_data = read_json(Path(args.pr_data).expanduser())
    if not isinstance(pr_data.get("pr_number"), int):
        pr_data["pr_number"] = args.pr
    if pr_data.get("pr_number") != args.pr:
        raise ShikiError(f"--pr {args.pr} does not match pr-data pr_number {pr_data.get('pr_number')}")
    implementer_report = read_json(Path(args.implementer_report).expanduser()) if args.implementer_report else None
    packet = build_review_packet(
        task=task,
        pr_data=pr_data,
        implementer_report=implementer_report,
        relevant_docs=args.relevant_doc,
        generated_at=utc_now(),
        source_refs=args.source_ref,
    )
    schema = read_json(packet_schema_path(target))
    validate_packet(packet, schema)
    if args.output:
        Path(args.output).expanduser().write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
        print_json({"packet_written": str(Path(args.output).expanduser()), "pr_number": packet["pr_number"], "head_sha": packet["head_sha"]})
    else:
        print_json(packet)
    return 0


def cmd_guardian_prompt(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    policy = _load_policy(target)
    packet = read_json(Path(args.packet).expanduser())
    schema = read_json(packet_schema_path(target))
    validate_packet(packet, schema)
    model, role = _reviewer_identity(policy, args)
    prompt = build_review_prompt(packet, reviewer_model=model, reviewer_role=role, fence=policy.ai_review_fence)
    _emit_text(prompt, args.output)
    return 0


def cmd_guardian_verify_response(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    policy = _load_policy(target)
    packet = read_json(Path(args.packet).expanduser())
    response_text = Path(args.response).expanduser().read_text(encoding="utf-8")
    result = verify_review_response(packet, response_text, policy=policy)
    if args.output:
        Path(args.output).expanduser().write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print_json(result)
    # Non-zero exit only on a hard error; a non-approval verdict is a valid,
    # routable outcome (the adapter inspects "approved"/"route").
    return 0
