#!/usr/bin/env python3
"""Machine-readable Guardian approval policy and evidence evaluation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any

GUARDIAN_POLICY_PATH = ".shiki/guardian-policy.json"
KNOWN_RISK_LEVELS = {"low", "medium", "high", "critical"}
BOT_LOGINS = {"github-actions", "github-actions[bot]"}
CLAUDE_LOGIN_MARKERS = ("claude", "anthropic")
TEAM_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class GuardianPolicyError(Exception):
    """Raised when Guardian policy loading fails."""


@dataclass(frozen=True)
class GuardianPolicy:
    version: int
    applies_to_risk: tuple[str, ...]
    users: tuple[str, ...]
    teams: tuple[str, ...]
    github_review_enabled: bool
    github_review_require_approved_state: bool
    guardian_label_enabled: bool
    label: str
    require_label_actor: bool
    guardian_comment_enabled: bool
    comment_marker: str
    require_head_sha: bool
    solo_maintainer_enabled: bool
    allow_pr_author_as_guardian: bool
    solo_maintainer_rationale: str
    github_actions_review_bridge_counts_as_guardian: bool
    advisory_claude_review_counts_as_guardian: bool


@dataclass(frozen=True)
class GuardianApprovalResult:
    approved: bool
    sources: tuple[str, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    approvers: tuple[str, ...]


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if isinstance(item, str) and item.strip())


def _bool(value: Any, *, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _policy_from_data(data: dict[str, Any]) -> GuardianPolicy:
    sources = data.get("approval_sources") if isinstance(data.get("approval_sources"), dict) else {}
    review = sources.get("github_review") if isinstance(sources.get("github_review"), dict) else {}
    label = sources.get("guardian_label") if isinstance(sources.get("guardian_label"), dict) else {}
    comment = sources.get("guardian_comment") if isinstance(sources.get("guardian_comment"), dict) else {}
    approvers = data.get("approvers") if isinstance(data.get("approvers"), dict) else {}
    solo = data.get("solo_maintainer") if isinstance(data.get("solo_maintainer"), dict) else {}
    exclusions = data.get("exclusions") if isinstance(data.get("exclusions"), dict) else {}
    return GuardianPolicy(
        version=data.get("version") if isinstance(data.get("version"), int) else -1,
        applies_to_risk=tuple(risk.lower() for risk in _strings(data.get("applies_to_risk"))),
        users=tuple(user.lower() for user in _strings(approvers.get("users"))),
        teams=tuple(team.lower() for team in _strings(approvers.get("teams"))),
        github_review_enabled=_bool(review.get("enabled")),
        github_review_require_approved_state=_bool(review.get("require_approved_state"), default=True),
        guardian_label_enabled=_bool(label.get("enabled")),
        label=str(label.get("label") or "").strip(),
        require_label_actor=_bool(label.get("require_label_actor"), default=True),
        guardian_comment_enabled=_bool(comment.get("enabled")),
        comment_marker=str(comment.get("marker") or "").strip(),
        require_head_sha=_bool(comment.get("require_head_sha"), default=True),
        solo_maintainer_enabled=_bool(solo.get("enabled")),
        allow_pr_author_as_guardian=_bool(solo.get("allow_pr_author_as_guardian")),
        solo_maintainer_rationale=str(solo.get("rationale") or "").strip(),
        github_actions_review_bridge_counts_as_guardian=_bool(exclusions.get("github_actions_review_bridge_counts_as_guardian")),
        advisory_claude_review_counts_as_guardian=_bool(exclusions.get("advisory_claude_review_counts_as_guardian")),
    )


def load_guardian_policy_file(path: Path) -> GuardianPolicy:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise GuardianPolicyError(f"{GUARDIAN_POLICY_PATH}: Guardian policy file is missing") from error
    except json.JSONDecodeError as error:
        raise GuardianPolicyError(f"{GUARDIAN_POLICY_PATH}: invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise GuardianPolicyError(f"{GUARDIAN_POLICY_PATH}: policy must be a JSON object")
    return _policy_from_data(data)


def load_guardian_policy(root: Path) -> GuardianPolicy:
    return load_guardian_policy_file(root / GUARDIAN_POLICY_PATH)


def validate_guardian_policy(policy: GuardianPolicy) -> list[str]:
    errors: list[str] = []
    if policy.version != 1:
        errors.append("guardian policy version must be 1")
    if not policy.applies_to_risk:
        errors.append("applies_to_risk must not be empty")
    for risk in policy.applies_to_risk:
        if risk not in KNOWN_RISK_LEVELS:
            errors.append(f"unsupported risk level: {risk}")
    if not policy.users and not policy.teams:
        errors.append("at least one Guardian approver user or team must be configured")
    for user in policy.users:
        if not user or any(ch.isspace() for ch in user):
            errors.append(f"invalid Guardian user login: {user!r}")
    for team in policy.teams:
        if not TEAM_SLUG_RE.match(team):
            errors.append(f"invalid Guardian team slug: {team!r}")
    if policy.guardian_label_enabled and not policy.label:
        errors.append("guardian label must be non-empty when label approval source is enabled")
    if policy.guardian_comment_enabled and not policy.comment_marker:
        errors.append("guardian comment marker must be non-empty when comment approval source is enabled")
    if policy.solo_maintainer_enabled:
        if not policy.solo_maintainer_rationale:
            errors.append("solo maintainer mode requires a non-empty rationale")
        if policy.allow_pr_author_as_guardian is not True:
            errors.append("solo maintainer mode must explicitly set allow_pr_author_as_guardian=true")
    if policy.github_actions_review_bridge_counts_as_guardian:
        errors.append("CCA Review Bridge must not count as Guardian approval by default")
    if policy.advisory_claude_review_counts_as_guardian:
        errors.append("advisory Claude review must not count as Guardian approval by default")
    return errors


def risk_requires_guardian(risk_labels: list[str], policy: GuardianPolicy) -> bool:
    normalized = {label.strip().lower().removeprefix("risk:") for label in risk_labels if label.strip()}
    return bool(normalized.intersection(set(policy.applies_to_risk)))


def _actor_login(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("login") or value.get("name") or value.get("slug")
    return str(value or "").strip().lower()


def _is_review_bridge(login: str) -> bool:
    return login.lower() in BOT_LOGINS


def _is_claude_actor(login: str) -> bool:
    lowered = login.lower()
    return any(marker in lowered for marker in CLAUDE_LOGIN_MARKERS)


def _pr_author(pr: dict[str, Any]) -> str:
    for key in ("author", "user"):
        author = pr.get(key)
        login = _actor_login(author)
        if login:
            return login
    return ""


def _pr_label_names(pr: dict[str, Any]) -> set[str]:
    labels: set[str] = set()
    for label in pr.get("labels") or []:
        if isinstance(label, dict):
            label = label.get("name")
        if label:
            labels.add(str(label).strip().lower())
    return labels


def _configured_guardian(login: str, policy: GuardianPolicy) -> bool:
    return login in set(policy.users)


def _team_allowed(team: str, policy: GuardianPolicy) -> bool:
    return bool(team and team in set(policy.teams))


def _author_allowed(login: str, pr_author: str, policy: GuardianPolicy) -> bool:
    if login != pr_author:
        return True
    return policy.solo_maintainer_enabled and policy.allow_pr_author_as_guardian and bool(policy.solo_maintainer_rationale)


def _valid_label_actor(policy: GuardianPolicy, label_events: list[dict[str, Any]], pr_author: str) -> tuple[bool, str | None]:
    if not policy.require_label_actor:
        return True, None
    expected = policy.label.lower()
    for event in label_events:
        if not isinstance(event, dict):
            continue
        event_name = str(event.get("event") or "").lower()
        label = event.get("label")
        label_name = str(label.get("name") if isinstance(label, dict) else label or "").strip().lower()
        if event_name not in {"labeled", "label_added"} or label_name != expected:
            continue
        actor = _actor_login(event.get("actor"))
        if _configured_guardian(actor, policy) and _author_allowed(actor, pr_author, policy):
            return True, actor
    return False, None


def _review_source(
    *,
    policy: GuardianPolicy,
    reviews: list[dict[str, Any]],
    pr_author: str,
) -> tuple[list[str], list[str], list[str], list[str]]:
    sources: list[str] = []
    approvers: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []
    if not policy.github_review_enabled:
        return sources, approvers, blockers, warnings
    for review in reviews:
        if not isinstance(review, dict):
            continue
        state = str(review.get("state") or "").upper()
        if policy.github_review_require_approved_state and state != "APPROVED":
            continue
        actor = _actor_login(review.get("author") or review.get("user"))
        if not actor:
            continue
        if _is_review_bridge(actor) and not policy.github_actions_review_bridge_counts_as_guardian:
            warnings.append("github-actions Review Bridge approval is not Guardian approval")
            continue
        if _is_claude_actor(actor) and not policy.advisory_claude_review_counts_as_guardian:
            warnings.append("advisory Claude review is not Guardian approval")
            continue
        if not _configured_guardian(actor, policy):
            team = _actor_login(review.get("team"))
            if _team_allowed(team, policy):
                blockers.append("Guardian team review could not be verified from PR review payload")
            continue
        if not _author_allowed(actor, pr_author, policy):
            blockers.append(f"PR author {actor} cannot satisfy Guardian review without solo maintainer policy")
            continue
        sources.append("github_review")
        approvers.append(actor)
    return sources, approvers, blockers, warnings


def _comment_source(
    *,
    policy: GuardianPolicy,
    comments: list[dict[str, Any]],
    head_sha: str,
    pr_author: str,
) -> tuple[list[str], list[str], list[str], list[str]]:
    sources: list[str] = []
    approvers: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []
    if not policy.guardian_comment_enabled:
        return sources, approvers, blockers, warnings
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "")
        if policy.comment_marker not in body:
            if "guardian approval" in body.lower() and "no guardian approval" in body.lower():
                warnings.append("negative Guardian approval text was ignored")
            continue
        actor = _actor_login(comment.get("author") or comment.get("user"))
        if not _configured_guardian(actor, policy):
            blockers.append(f"Guardian approval comment actor {actor or '<missing>'} is not configured")
            continue
        if not _author_allowed(actor, pr_author, policy):
            blockers.append(f"PR author {actor} cannot satisfy Guardian comment without solo maintainer policy")
            continue
        if policy.require_head_sha and head_sha not in body:
            blockers.append("Guardian approval comment does not reference current head SHA")
            continue
        sources.append("guardian_comment")
        approvers.append(actor)
    return sources, approvers, blockers, warnings


def evaluate_guardian_approval(
    *,
    policy: GuardianPolicy,
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    label_events: list[dict[str, Any]],
    head_sha: str,
) -> GuardianApprovalResult:
    blockers: list[str] = []
    warnings: list[str] = []
    sources: list[str] = []
    approvers: list[str] = []
    policy_errors = validate_guardian_policy(policy)
    if policy_errors:
        return GuardianApprovalResult(False, (), tuple(policy_errors), (), ())

    labels = _pr_label_names(pr)
    label_present = policy.guardian_label_enabled and policy.label.lower() in labels
    pr_author = _pr_author(pr)
    if not label_present:
        blockers.append(f"Guardian label {policy.label!r} is missing")
    else:
        label_actor_ok, label_actor = _valid_label_actor(policy, label_events, pr_author)
        if label_actor_ok:
            sources.append("guardian_label")
            if label_actor:
                approvers.append(label_actor)
        else:
            blockers.append(f"Guardian label {policy.label!r} was not applied by a configured Guardian")

    review_sources, review_approvers, review_blockers, review_warnings = _review_source(
        policy=policy,
        reviews=reviews,
        pr_author=pr_author,
    )
    sources.extend(review_sources)
    approvers.extend(review_approvers)
    blockers.extend(review_blockers)
    warnings.extend(review_warnings)

    comment_sources, comment_approvers, comment_blockers, comment_warnings = _comment_source(
        policy=policy,
        comments=comments,
        head_sha=head_sha,
        pr_author=pr_author,
    )
    sources.extend(comment_sources)
    approvers.extend(comment_approvers)
    blockers.extend(comment_blockers)
    warnings.extend(comment_warnings)

    has_secondary = bool(set(sources).intersection({"github_review", "guardian_comment"}))
    if not has_secondary:
        blockers.append("Guardian approval requires a configured Guardian review or current-head Guardian comment")

    approved = label_present and has_secondary and not any("must not" in blocker for blocker in blockers)
    if blockers:
        approved = False
    return GuardianApprovalResult(
        approved=approved,
        sources=tuple(dict.fromkeys(sources)),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
        approvers=tuple(dict.fromkeys(approvers)),
    )
