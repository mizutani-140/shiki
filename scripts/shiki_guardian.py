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
    # external_ai_guardian_review: a first-class authority kind (ADR 0010).
    # An external AI reviewer (e.g. GPT-5.5 Pro) can authorize autonomous merge,
    # recorded under its OWN model identity — never as a human operator approval.
    ai_review_enabled: bool = False
    ai_review_fence: str = "external-ai-guardian-review"
    ai_review_require_head_sha: bool = True
    ai_review_allowed_models: tuple[str, ...] = ()
    ai_review_allowed_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardianApprovalResult:
    approved: bool
    sources: tuple[str, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    approvers: tuple[str, ...]
    # AI reviewer identities (model names) when external_ai_guardian_review
    # satisfied approval. Recorded distinctly from human `approvers` so the
    # merge ledger can stamp reviewer_type=external_ai_model.
    ai_reviewers: tuple[str, ...] = ()


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
    ai_review = sources.get("external_ai_guardian_review") if isinstance(sources.get("external_ai_guardian_review"), dict) else {}
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
        ai_review_enabled=_bool(ai_review.get("enabled")),
        ai_review_fence=str(ai_review.get("fence") or "external-ai-guardian-review").strip(),
        ai_review_require_head_sha=_bool(ai_review.get("require_head_sha"), default=True),
        ai_review_allowed_models=_strings(ai_review.get("allowed_models")),
        ai_review_allowed_roles=_strings(ai_review.get("allowed_roles")),
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
    if policy.ai_review_enabled:
        if not policy.ai_review_fence:
            errors.append("external_ai_guardian_review requires a non-empty fence marker when enabled")
        if policy.ai_review_require_head_sha is not True:
            errors.append("external_ai_guardian_review must bind to the head SHA (require_head_sha=true)")
        if not policy.ai_review_allowed_models:
            errors.append("external_ai_guardian_review must list at least one allowed reviewer model when enabled")
        if not policy.ai_review_allowed_roles:
            errors.append("external_ai_guardian_review must list at least one allowed reviewer role when enabled")
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
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    sources: list[str] = []
    approvers: list[str] = []
    blockers: list[str] = []
    soft: list[str] = []
    warnings: list[str] = []
    if not policy.github_review_enabled:
        return sources, approvers, blockers, soft, warnings
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
                # Soft: an unverifiable team review must not poison a gate that
                # another authority already approved; fatal only when it is the
                # sole approval attempt.
                soft.append("Guardian team review could not be verified from PR review payload")
            else:
                # An arbitrary unconfigured reviewer is explicitly fail-closed and
                # auditable (symmetric with the comment path): it never satisfies
                # approval, is fatal when it is the sole attempt, and is demoted to
                # a warning when another authority validly approved.
                soft.append(f"Guardian review actor {actor} is not configured")
            continue
        if not _author_allowed(actor, pr_author, policy):
            # Soft: a PR author's own stray review must not poison a validly
            # approved gate (symmetric with the comment and AI paths); it still
            # blocks when it is the only approval attempt.
            soft.append(f"PR author {actor} cannot satisfy Guardian review without solo maintainer policy")
            continue
        sources.append("github_review")
        approvers.append(actor)
    return sources, approvers, blockers, soft, warnings


def _comment_source(
    *,
    policy: GuardianPolicy,
    comments: list[dict[str, Any]],
    head_sha: str,
    pr_author: str,
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    sources: list[str] = []
    approvers: list[str] = []
    blockers: list[str] = []
    soft: list[str] = []
    warnings: list[str] = []
    if not policy.guardian_comment_enabled:
        return sources, approvers, blockers, soft, warnings
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
            # Soft: a stray marker comment from a non-Guardian must not let any
            # user grief a validly-approved gate; it is a blocker only when it
            # is the sole approval attempt.
            soft.append(f"Guardian approval comment actor {actor or '<missing>'} is not configured")
            continue
        if not _author_allowed(actor, pr_author, policy):
            soft.append(f"PR author {actor} cannot satisfy Guardian comment without solo maintainer policy")
            continue
        if policy.require_head_sha and head_sha not in body:
            # Stale/malformed marker comment lacking the current head SHA. This
            # is a SOFT blocker: it is fatal only when no other valid approval
            # source exists (the poisoning fix is applied in the caller).
            soft.append("Guardian approval comment does not reference current head SHA")
            continue
        sources.append("guardian_comment")
        approvers.append(actor)
    return sources, approvers, blockers, soft, warnings


def _extract_ai_review_artifacts(body: str, fence: str) -> list[dict[str, Any]]:
    """Extract every fenced ```<fence> {json} ``` artifact from a comment body.

    Scans ALL fenced blocks (not just the first) so a malformed leading block
    cannot hide a later valid artifact.
    """
    artifacts: list[dict[str, Any]] = []
    marker = "```" + fence
    cursor = 0
    while True:
        start = body.find(marker, cursor)
        if start == -1:
            break
        rest = body[start + len(marker):]
        end = rest.find("```")
        if end == -1:
            break
        block = rest[:end].strip()
        cursor = start + len(marker) + end + 3
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            artifacts.append(data)
    return artifacts


def validate_ai_review_artifact(
    artifact: dict[str, Any],
    *,
    policy: GuardianPolicy,
    expected_repo: str,
    pr_number: int | None,
    head_sha: str,
) -> tuple[list[str], list[str]]:
    """Field + binding validation for a single external_ai_guardian_review artifact.

    Returns ``(violations, soft_violations)``. Empty in both lists means the
    artifact is a valid autonomous-merge approval bound to the given repo, PR,
    and head SHA. A stale/missing head SHA is a *soft* violation (fatal only when
    it is the sole approval attempt; see the caller's promotion logic); every
    other failure is a hard violation. The checks short-circuit at the first
    failure so a single artifact yields a single message.

    This is the shared approval contract: both the PR-comment evaluation path
    (``_external_ai_review_source``) and the offline Codex App adapter
    verify-response path consume it so neither can drift from ADR 0010 / ADR 0014
    approval semantics. Missing/under-specified fields fail closed.
    """
    if artifact.get("kind") != "external_ai_guardian_review":
        return (["external AI guardian review artifact kind must be external_ai_guardian_review"], [])
    # Identity boundary: the artifact must EXPLICITLY declare itself a
    # non-operator AI-model review. Missing/null/false all fail closed.
    if artifact.get("not_operator_approval") is not True:
        return (["external AI guardian review artifact must explicitly declare not_operator_approval=true"], [])
    reviewer = artifact.get("reviewer") if isinstance(artifact.get("reviewer"), dict) else {}
    if str(reviewer.get("type") or "").strip() != "ai_model":
        return (["external AI guardian review artifact reviewer.type must be ai_model"], [])
    model = str(reviewer.get("model") or "").strip()
    role = str(reviewer.get("role") or "").strip()
    if model not in policy.ai_review_allowed_models:
        return ([f"external AI guardian review model {model!r} is not in the allowed list"], [])
    if role not in policy.ai_review_allowed_roles:
        return ([f"external AI guardian review role {role!r} is not in the allowed list"], [])
    if artifact.get("verdict") != "approve" or artifact.get("merge_permission") != "autonomous_merge_permitted":
        return (["external AI guardian review artifact does not authorize autonomous merge"], [])
    # Repo binding: defeat cross-repo replay. Fail closed if either side is empty.
    artifact_repo = str(artifact.get("repo") or "").strip().lower()
    expected = (expected_repo or "").strip().lower()
    if not expected or artifact_repo != expected:
        return (["external AI guardian review artifact does not bind to this repository"], [])
    # PR binding: exact match; fail closed when the PR number is unknown.
    if pr_number is None or artifact.get("pr") != pr_number:
        return (["external AI guardian review artifact does not bind to this PR"], [])
    # Head-SHA binding: require a non-empty exact match (soft so a stale artifact
    # cannot poison an otherwise valid approval from another authority).
    artifact_head = str(artifact.get("head_sha") or "")
    if policy.ai_review_require_head_sha and (not head_sha or artifact_head != head_sha):
        return ([], ["external AI guardian review artifact does not reference current head SHA"])
    return ([], [])


def _external_ai_review_source(
    *,
    policy: GuardianPolicy,
    comments: list[dict[str, Any]],
    head_sha: str,
    pr: dict[str, Any],
    pr_author: str,
    expected_repo: str,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Evaluate external_ai_guardian_review artifacts carried in PR comments.

    Returns (sources, ai_reviewers, soft_blockers, warnings). The recorded
    authority is the AI reviewer model, never the human comment author; a
    configured Guardian must relay the artifact (integrity) AND satisfy the
    same PR-author guard the human paths enforce (the AI path must be no weaker
    than the human comment path). The artifact is bound to the exact repo, PR,
    and head SHA to defeat cross-repo / cross-PR / stale replay.
    """
    sources: list[str] = []
    ai_reviewers: list[str] = []
    soft: list[str] = []
    warnings: list[str] = []
    if not policy.ai_review_enabled:
        return sources, ai_reviewers, soft, warnings
    pr_number = pr.get("number")
    expected_repo = (expected_repo or "").strip().lower()
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        for artifact in _extract_ai_review_artifacts(str(comment.get("body") or ""), policy.ai_review_fence):
            if artifact.get("kind") != "external_ai_guardian_review":
                continue
            relay = _actor_login(comment.get("author") or comment.get("user"))
            if not _configured_guardian(relay, policy):
                # Soft: a non-Guardian relay must not poison a valid approval,
                # but is a blocker when it is the only attempt.
                soft.append(f"external AI guardian review relayed by non-Guardian actor {relay or '<missing>'}")
                continue
            if not _author_allowed(relay, pr_author, policy):
                # The AI path is no weaker than the human comment path: a PR
                # author relaying their own AI artifact is allowed only under
                # the explicit solo-maintainer escape hatch.
                soft.append(f"PR author {relay} cannot relay external AI guardian review without solo maintainer policy")
                continue
            # Field + binding validation is the shared approval contract
            # (validate_ai_review_artifact), so the offline adapter
            # verify-response path enforces identical semantics.
            violations, soft_violations = validate_ai_review_artifact(
                artifact,
                policy=policy,
                expected_repo=expected_repo,
                pr_number=pr_number,
                head_sha=head_sha,
            )
            if violations:
                warnings.extend(violations)
                continue
            if soft_violations:
                soft.extend(soft_violations)
                continue
            reviewer = artifact.get("reviewer") if isinstance(artifact.get("reviewer"), dict) else {}
            model = str(reviewer.get("model") or "").strip()
            sources.append("external_ai_guardian_review")
            ai_reviewers.append(model)
    return sources, ai_reviewers, soft, warnings


def evaluate_guardian_approval(
    *,
    policy: GuardianPolicy,
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    label_events: list[dict[str, Any]],
    head_sha: str,
    expected_repo: str = "",
) -> GuardianApprovalResult:
    blockers: list[str] = []
    warnings: list[str] = []
    sources: list[str] = []
    approvers: list[str] = []
    # Human-path failures (missing/forged label) are fatal only when no
    # approval path succeeds; the external AI guardian path does not require
    # the human label.
    human_path_blockers: list[str] = []
    policy_errors = validate_guardian_policy(policy)
    if policy_errors:
        return GuardianApprovalResult(False, (), tuple(policy_errors), (), ())

    labels = _pr_label_names(pr)
    label_present = policy.guardian_label_enabled and policy.label.lower() in labels
    label_actor_ok = False
    pr_author = _pr_author(pr)
    if not label_present:
        human_path_blockers.append(f"Guardian label {policy.label!r} is missing")
    else:
        label_actor_ok, label_actor = _valid_label_actor(policy, label_events, pr_author)
        if label_actor_ok:
            sources.append("guardian_label")
            if label_actor:
                approvers.append(label_actor)
        else:
            human_path_blockers.append(f"Guardian label {policy.label!r} was not applied by a configured Guardian")

    soft_blockers: list[str] = []
    review_sources, review_approvers, review_blockers, review_soft, review_warnings = _review_source(
        policy=policy,
        reviews=reviews,
        pr_author=pr_author,
    )
    sources.extend(review_sources)
    approvers.extend(review_approvers)
    blockers.extend(review_blockers)
    soft_blockers.extend(review_soft)
    warnings.extend(review_warnings)
    comment_sources, comment_approvers, comment_blockers, comment_soft, comment_warnings = _comment_source(
        policy=policy,
        comments=comments,
        head_sha=head_sha,
        pr_author=pr_author,
    )
    sources.extend(comment_sources)
    approvers.extend(comment_approvers)
    blockers.extend(comment_blockers)
    soft_blockers.extend(comment_soft)
    warnings.extend(comment_warnings)

    ai_sources, ai_reviewers, ai_soft, ai_warnings = _external_ai_review_source(
        policy=policy,
        comments=comments,
        head_sha=head_sha,
        pr=pr,
        pr_author=pr_author,
        expected_repo=expected_repo,
    )
    sources.extend(ai_sources)
    soft_blockers.extend(ai_soft)
    warnings.extend(ai_warnings)

    # Two approval paths satisfy the gate independently:
    #  - the human path: Guardian label applied by a configured Guardian PLUS a
    #    current-head Guardian review or comment;
    #  - the external AI guardian path: a valid head-bound external AI review
    #    artifact (a distinct authority kind; no human label required).
    human_secondary = bool(set(sources).intersection({"github_review", "guardian_comment"}))
    human_approved = label_present and label_actor_ok and human_secondary
    ai_approved = "external_ai_guardian_review" in sources
    approved_path = human_approved or ai_approved

    if not approved_path:
        # No valid approval exists: explain why. The human-path failures and a
        # stale/malformed approval attempt are the genuine blockers in this case.
        blockers.extend(human_path_blockers)
        if not human_secondary and not ai_approved:
            blockers.append("Guardian approval requires a configured Guardian review or current-head Guardian comment")
        blockers.extend(soft_blockers)
    else:
        # A valid exact-head approval exists: stale/malformed approval-like
        # artifacts and unused human-path conditions must not poison the gate —
        # record them as warnings.
        warnings.extend(soft_blockers)
        if ai_approved and not human_approved:
            warnings.extend(human_path_blockers)
        else:
            blockers.extend(human_path_blockers)

    approved = approved_path and not blockers
    return GuardianApprovalResult(
        approved=approved,
        sources=tuple(dict.fromkeys(sources)),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
        approvers=tuple(dict.fromkeys(approvers)),
        ai_reviewers=tuple(dict.fromkeys(ai_reviewers)),
    )
