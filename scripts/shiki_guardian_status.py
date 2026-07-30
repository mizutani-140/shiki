#!/usr/bin/env python3
"""Show the Guardian what the gate is waiting for.

``shiki guardian status --pr N`` runs the SAME authoritative
``evaluate_guardian_approval`` the MergeGate policy check and the CCA signal use,
over LIVE PR evidence, and reports:

  (a) which approval sources are satisfied and by whom;
  (b) which requirements are still missing (the exact blockers the gate computed);
  (c) a paste-ready Guardian-approval comment body carrying the policy marker and
      the current 40-character head SHA, read from live PR state in full — never
      abbreviated, transcribed, or reconstructed from a prefix;
  (d) whether any unsynced ledger evidence or unpushed commit means the head SHA
      is about to change, in which case the approval body is WITHHELD so approval
      is never requested against a doomed SHA.

"LLM outputs may vary. State transitions must not vary." This command is a
deterministic, READ-ONLY advisory surface: it never applies a label and never
posts an approval. It only tells the operator (locally) or the approver (as a CCA
PR comment) the exact string the gate is waiting for.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any

from shiki_guardian import (
    GUARDIAN_POLICY_PATH,
    GuardianPolicy,
    evaluate_guardian_approval,
    load_guardian_policy_file,
)
from shiki_process import ShikiError, load_default_config, require_tool, run, target_path

# A full git object name: exactly 40 lowercase hex characters. The paste-ready
# approval body MUST bind to a full head SHA; a short/abbreviated/reconstructed
# SHA is refused (PR #179 failure mode: a head SHA fabricated from a 12-char
# prefix). Matched case-insensitively but the body carries the value verbatim.
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Hidden HTML marker on the CCA-posted comment so the workflow can UPDATE the
# same comment across re-runs instead of spamming a new one each synchronize.
COMMENT_MARKER = "<!-- shiki:guardian-status -->"

_COMMENT_FMT = "comment"
_TEXT_FMT = "text"


def is_full_head_sha(sha: str) -> bool:
    """True only for an exact 40-hex-character git object name."""
    return bool(_FULL_SHA_RE.match(sha or ""))


def build_approval_body(marker: str, head_sha: str) -> str:
    """Render the paste-ready Guardian-approval comment body.

    The marker is line-initial (so ``_marker_is_line_initial`` accepts it) and
    the FULL head SHA appears on its own line (so ``require_head_sha`` is
    satisfied). The body carries no negation/revocation cue, so a configured
    Guardian can paste it verbatim to satisfy the guardian_comment source.
    """
    return f"{marker}\n\n{head_sha}"


def detect_sha_change(
    *,
    local_head: str,
    live_head: str,
    porcelain_paths: list[str],
) -> tuple[bool, list[str]]:
    """Decide whether the PR head SHA is about to change.

    Pure over gathered git state so it is unit-testable without a repo:

    - a local HEAD that differs from the live PR head means an unpushed/diverged
      commit — a push will move the PR head SHA;
    - any uncommitted working-tree change will change the head SHA once committed
      and pushed; unsynced ``.shiki/ledger/`` evidence is called out by name
      because it is the common case this command exists to catch.

    Returns ``(will_change, reasons)``. When ``will_change`` is true the caller
    WITHHOLDS the approval body so approval is never requested against a SHA that
    is about to be replaced.
    """
    will_change = False
    reasons: list[str] = []
    if local_head and live_head and local_head != live_head:
        will_change = True
        reasons.append(
            f"Local HEAD {local_head} differs from the live PR head {live_head}; "
            "the PR head SHA is about to change — push first, then re-run."
        )
    ledger = [p for p in porcelain_paths if p.startswith(".shiki/ledger/")]
    other = sorted(p for p in porcelain_paths if not p.startswith(".shiki/ledger/"))
    if ledger:
        will_change = True
        reasons.append(
            "Unsynced ledger evidence is not committed/pushed yet: "
            + ", ".join(sorted(ledger))
            + "; committing it will change the head SHA."
        )
    if other:
        shown = ", ".join(other[:10]) + (" …" if len(other) > 10 else "")
        will_change = True
        reasons.append(
            "Uncommitted working-tree changes will change the head SHA once "
            f"committed and pushed: {shown}."
        )
    return will_change, reasons


@dataclass(frozen=True)
class GuardianStatusReport:
    pr_number: int
    repository: str
    head_sha: str
    approved: bool
    satisfied: tuple[tuple[str, str], ...]  # (source, by-whom)
    missing: tuple[str, ...]
    warnings: tuple[str, ...]
    sha_will_change: bool
    sha_change_reasons: tuple[str, ...]
    marker: str
    label: str
    approval_body: str | None = None
    approval_body_withheld_reason: str | None = None


def build_status_report(
    *,
    policy: GuardianPolicy,
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    label_events: list[dict[str, Any]],
    head_sha: str,
    expected_repo: str,
    repository: str = "",
    pr_number: int | None = None,
    sha_will_change: bool = False,
    sha_change_reasons: tuple[str, ...] | list[str] = (),
) -> GuardianStatusReport:
    """Assemble the Guardian status report from live evidence.

    Delegates the approval decision entirely to ``evaluate_guardian_approval`` so
    this surface can never diverge from MergeGate; it only presents that result
    and derives the paste-ready body.
    """
    result = evaluate_guardian_approval(
        policy=policy,
        pr=pr,
        reviews=reviews,
        comments=comments,
        label_events=label_events,
        head_sha=head_sha,
        expected_repo=expected_repo,
    )

    human_by = ", ".join(result.approvers) if result.approvers else "<unknown>"
    ai_by = ", ".join(result.ai_reviewers) if result.ai_reviewers else "<external AI reviewer>"
    satisfied: list[tuple[str, str]] = []
    for source in result.sources:
        if source == "external_ai_guardian_review":
            satisfied.append((source, f"reviewer_type=external_ai_model ({ai_by})"))
        else:
            satisfied.append((source, human_by))

    # Derive the paste-ready approval body, or record precisely why it is withheld.
    marker = policy.comment_marker
    approval_body: str | None = None
    withheld: str | None = None
    if not policy.guardian_comment_enabled or not marker:
        withheld = "guardian policy has no comment approval source (marker) configured"
    elif not is_full_head_sha(head_sha):
        withheld = (
            "head SHA is not a full 40-character SHA read from live PR state; "
            "refusing to abbreviate or reconstruct one"
        )
    elif sha_will_change:
        withheld = (
            "the PR head SHA is about to change (see head-SHA stability warnings); "
            "push first, then re-run so approval binds to the final SHA"
        )
    else:
        approval_body = build_approval_body(marker, head_sha)

    resolved_number = pr_number if pr_number is not None else pr.get("number")
    try:
        resolved_number = int(resolved_number)
    except (TypeError, ValueError):
        resolved_number = 0

    return GuardianStatusReport(
        pr_number=resolved_number,
        repository=repository or expected_repo,
        head_sha=head_sha,
        approved=bool(result.approved),
        satisfied=tuple(satisfied),
        missing=result.blockers,
        warnings=result.warnings,
        sha_will_change=bool(sha_will_change),
        sha_change_reasons=tuple(sha_change_reasons),
        marker=marker,
        label=policy.label,
        approval_body=approval_body,
        approval_body_withheld_reason=withheld,
    )


def _bullets(items: list[str] | tuple[str, ...], *, empty: str = "(none)") -> list[str]:
    items = [i for i in items if str(i).strip()]
    if not items:
        return [f"  {empty}"]
    return [f"  - {item}" for item in items]


def render_report(report: GuardianStatusReport, *, fmt: str = _TEXT_FMT) -> str:
    """Render the report as operator text or as a paste-ready PR comment."""
    if fmt == _COMMENT_FMT:
        return _render_comment(report)
    return _render_text(report)


def _render_text(report: GuardianStatusReport) -> str:
    lines: list[str] = []
    verdict = "APPROVED" if report.approved else "NOT APPROVED"
    lines.append(f"Guardian approval status for PR #{report.pr_number} ({report.repository})")
    lines.append(f"Head SHA: {report.head_sha or '<unavailable>'}")
    lines.append(f"Result: {verdict}")
    lines.append("")
    lines.append("Satisfied approval sources:")
    if report.satisfied:
        lines.extend(f"  - {source} (by {by})" for source, by in report.satisfied)
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Missing / what the gate is waiting for:")
    lines.extend(_bullets(list(report.missing)))
    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(_bullets(list(report.warnings)))
    lines.append("")
    lines.append("Head SHA stability:")
    if report.sha_will_change:
        lines.extend(_bullets(list(report.sha_change_reasons)))
    else:
        lines.append("  - clean: safe to request approval against this head SHA")
    lines.append("")
    if report.approval_body is not None:
        lines.append("Paste-ready Guardian approval comment (a configured Guardian pastes this verbatim):")
        lines.append("----------8<----------")
        lines.append(report.approval_body)
        lines.append("---------->8----------")
    else:
        lines.append(f"Paste-ready Guardian approval comment: WITHHELD — {report.approval_body_withheld_reason}")
    return "\n".join(lines)


def _render_comment(report: GuardianStatusReport) -> str:
    lines: list[str] = [COMMENT_MARKER]
    if report.approved:
        lines.append("### ✅ Guardian approval satisfied")
        lines.append("")
        lines.append(f"**PR:** #{report.pr_number} · **Head SHA:** `{report.head_sha}`")
        if report.satisfied:
            lines.append("")
            lines.append("**Satisfied by:**")
            lines.extend(f"- {source} (by {by})" for source, by in report.satisfied)
        return "\n".join(lines)

    lines.append("### 🔒 Guardian approval required — this PR is blocked pending Guardian sign-off")
    lines.append("")
    lines.append(f"**PR:** #{report.pr_number} · **Head SHA:** `{report.head_sha or 'unavailable'}`")
    lines.append("")
    lines.append("**What the gate is waiting for:**")
    if report.missing:
        lines.extend(f"- {item}" for item in report.missing)
    else:
        lines.append("- (no blockers recorded)")
    if report.satisfied:
        lines.append("")
        lines.append("**Already satisfied:**")
        lines.extend(f"- {source} (by {by})" for source, by in report.satisfied)
    lines.append("")
    if report.approval_body is not None:
        lines.append("**To approve, a configured Guardian must post a comment containing exactly:**")
        lines.append("")
        lines.append("```")
        lines.append(report.approval_body)
        lines.append("```")
        lines.append("")
        lines.append(
            "The head SHA above is the live PR head, in full. It must match when the "
            "approval is posted; a later push invalidates it."
        )
    else:
        lines.append(f"**Approval body withheld:** {report.approval_body_withheld_reason}")
    if report.sha_will_change:
        lines.append("")
        lines.append("**⚠️ Head SHA is about to change:**")
        lines.extend(f"- {reason}" for reason in report.sha_change_reasons)
    if report.warnings:
        lines.append("")
        lines.append("**Warnings:**")
        lines.extend(f"- {warning}" for warning in report.warnings)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Evidence gathering (live gh + git) and CLI
# --------------------------------------------------------------------------- #

_PR_VIEW_FIELDS = "number,title,body,author,headRefName,baseRefName,headRefOid,labels,reviews,reviewDecision"


def _load_json_file(path: str | None) -> Any:
    if not path:
        return None
    p = Path(path).expanduser()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _gh_json(args: list[str], target: Path) -> Any:
    result = run(["gh", *args], cwd=target, check=False)
    if result.returncode != 0:
        raise ShikiError("gh " + " ".join(args) + f" failed\n{result.stderr.strip()}")
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as error:
        raise ShikiError(f"gh {' '.join(args)} returned invalid JSON: {error}") from error


def _resolve_repo(args: argparse.Namespace, target: Path) -> str:
    if getattr(args, "repo", None):
        return str(args.repo)
    config_repo = load_default_config().get("repo")
    if config_repo:
        return str(config_repo)
    view = run(["gh", "repo", "view", "--json", "nameWithOwner"], cwd=target, check=False)
    if view.returncode == 0:
        try:
            name = json.loads(view.stdout or "{}").get("nameWithOwner")
            if name:
                return str(name)
        except json.JSONDecodeError:
            pass
    raise ShikiError("could not resolve repository; pass --repo OWNER/NAME")


def _git_sha_change(target: Path, live_head: str) -> tuple[bool, list[str]]:
    head = run(["git", "-C", str(target), "rev-parse", "HEAD"], check=False)
    if head.returncode != 0:
        return True, [
            "could not read local git HEAD to verify head-SHA stability; "
            "withholding the approval body to be safe"
        ]
    local_head = head.stdout.strip()
    status = run(["git", "-C", str(target), "status", "--porcelain"], check=False)
    porcelain_paths: list[str] = []
    if status.returncode == 0:
        for line in status.stdout.splitlines():
            if len(line) > 3:
                entry = line[3:].strip()
                # A rename shows "old -> new"; the new path is what will be committed.
                if " -> " in entry:
                    entry = entry.split(" -> ", 1)[1]
                if entry:
                    porcelain_paths.append(entry)
    return detect_sha_change(local_head=local_head, live_head=live_head, porcelain_paths=porcelain_paths)


def _gather_evidence(args: argparse.Namespace, target: Path) -> dict[str, Any]:
    """Gather PR evidence from live gh (default) or from provided files (offline).

    Offline mode is selected by ``--pr-json`` and is what the CCA workflow and the
    unit tests use: it consumes evidence already fetched from live PR state (or a
    fixture) without hitting the network. Live mode is the operator's one command.
    """
    offline = bool(getattr(args, "pr_json", None))
    if offline:
        pr = _load_json_file(args.pr_json)
        if not isinstance(pr, dict):
            raise ShikiError(f"--pr-json {args.pr_json} is missing or not a JSON object")
        comments = _as_dict_list(_load_json_file(getattr(args, "comments", None)))
        events = _as_dict_list(_load_json_file(getattr(args, "events", None)))
        timeline = _as_dict_list(_load_json_file(getattr(args, "timeline", None)))
        repository = str(getattr(args, "repo", None) or "")
    else:
        require_tool("gh")
        repository = _resolve_repo(args, target)
        pr = _gh_json(["pr", "view", str(args.pr), "--repo", repository, "--json", _PR_VIEW_FIELDS], target)
        if not isinstance(pr, dict):
            raise ShikiError(f"gh pr view {args.pr} did not return a PR object")
        comments = _as_dict_list(_gh_json(["api", "--paginate", f"repos/{repository}/issues/{args.pr}/comments"], target))
        events = _as_dict_list(_gh_json(["api", "--paginate", f"repos/{repository}/issues/{args.pr}/events"], target))
        timeline = _as_dict_list(
            _gh_json(
                ["api", "--paginate", f"repos/{repository}/issues/{args.pr}/timeline", "-H", "Accept: application/vnd.github+json"],
                target,
            )
        )

    number = pr.get("number")
    if isinstance(number, int) and number != args.pr:
        raise ShikiError(f"--pr {args.pr} does not match PR evidence number {number}")

    head_sha = str(getattr(args, "head_sha", None) or pr.get("headRefOid") or "")
    reviews = [r for r in pr.get("reviews") or [] if isinstance(r, dict)]

    sha_will_change = False
    sha_change_reasons: list[str] = []
    if not getattr(args, "no_git", False):
        sha_will_change, sha_change_reasons = _git_sha_change(target, head_sha)

    return {
        "pr": pr,
        "comments": comments,
        "label_events": events + timeline,
        "reviews": reviews,
        "head_sha": head_sha,
        "repository": repository,
        "sha_will_change": sha_will_change,
        "sha_change_reasons": sha_change_reasons,
    }


def _policy_path(args: argparse.Namespace, target: Path) -> Path:
    override = getattr(args, "guardian_policy", None)
    if override:
        return Path(override).expanduser()
    return target / GUARDIAN_POLICY_PATH


def cmd_guardian_status(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    policy = load_guardian_policy_file(_policy_path(args, target))
    evidence = _gather_evidence(args, target)
    report = build_status_report(
        policy=policy,
        pr=evidence["pr"],
        reviews=evidence["reviews"],
        comments=evidence["comments"],
        label_events=evidence["label_events"],
        head_sha=evidence["head_sha"],
        expected_repo=evidence["repository"],
        repository=evidence["repository"],
        pr_number=args.pr,
        sha_will_change=evidence["sha_will_change"],
        sha_change_reasons=tuple(evidence["sha_change_reasons"]),
    )
    rendered = render_report(report, fmt=args.format)
    if getattr(args, "output", None):
        out = Path(args.output).expanduser()
        out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


# Test-friendly alias mirroring guardian_approval_signal.main_with_argv.
def main_with_argv(argv: list[str]) -> int:
    from shiki_cli import build_parser  # local import to avoid a cycle at module load

    parser = build_parser()
    args = parser.parse_args(["guardian", "status", *argv])
    return args.func(args)
