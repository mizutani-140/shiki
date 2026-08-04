"""Adversarial binding tests for Guardian approval evidence handling.

These attack the approval surface directly — the durable lesson from
T-...02de0043 is that the approval path had no adversarial test of its OWN
evidence handling. Each of the four defects fixed there gets at least one case,
and EVERY rejecting case also asserts the Guardian requirement still resolves as
REQUIRED (the risk gate is on and approval is NOT satisfied), so a rejection is
never mistaken for "Guardian not required":

1. PAGINATION — every Guardian-evidence ``gh api`` call in BOTH workflows pages.
2. EMPTY HEAD SHA — an empty head SHA under ``require_head_sha`` is MISSING
   evidence (the old ``head_sha not in body`` guard was dead for ``''``).
3. POSITIONAL HEAD BINDING — the head SHA must stand on its own line in the
   ``build_approval_body`` shape; a SHA quoted in prose, an abbreviated SHA, two
   tokens, or a comment from a non-Guardian never approve. The actor check runs
   BEFORE SHA extraction (asserted by ordering, not only outcome).
4. PATCH TARGET — the guardian-status comment is PATCHed only when authored by
   ``github-actions[bot]``.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_guardian import (
    GuardianApprovalResult,
    _comment_source,
    evaluate_guardian_approval,
    load_guardian_policy,
    risk_requires_guardian,
)
from shiki_guardian_status import build_approval_body

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
POLICY = load_guardian_policy(REPO_ROOT)
MARKER = POLICY.comment_marker
HEAD = "a" * 40
OTHER = "b" * 40
GUARDIAN = "mizutani-140"
NON_GUARDIAN = "mallory"
RISK_LABELS = ["risk:critical"]


def _pr(*, author: str = GUARDIAN, labels=("guardian:approved", "risk:critical")) -> dict:
    return {
        "number": 55,
        "headRefOid": HEAD,
        "author": {"login": author},
        "labels": [{"name": name} for name in labels],
    }


def _labeled(actor: str = GUARDIAN) -> dict:
    return {"event": "labeled", "label": {"name": "guardian:approved"}, "actor": {"login": actor}}


def _comment(body: str, author: str = GUARDIAN) -> dict:
    return {"author": {"login": author}, "body": body}


def _evaluate(*, comments=(), head_sha: str = HEAD, pr=None, events=None) -> GuardianApprovalResult:
    return evaluate_guardian_approval(
        policy=POLICY,
        pr=pr if pr is not None else _pr(),
        reviews=[],
        comments=list(comments),
        label_events=[_labeled()] if events is None else list(events),
        head_sha=head_sha,
        expected_repo="mizutani-140/shiki",
    )


class _RequirementMixin(unittest.TestCase):
    def assertStillRequiredAndBlocked(self, result: GuardianApprovalResult) -> None:
        # The requirement RESOLVES as required (the risk gate is on) and is NOT
        # met — not merely that "something blocked".
        self.assertTrue(risk_requires_guardian(RISK_LABELS, POLICY), "critical risk must require a Guardian")
        self.assertFalse(result.approved, result)
        self.assertTrue(result.blockers, f"a rejecting case must record a blocker: {result}")


class Defect2EmptyHeadTests(_RequirementMixin):
    def test_empty_head_with_marker_comment_is_not_approved(self) -> None:
        # A well-formed canonical body but an empty PR head SHA: MISSING evidence,
        # the SAME soft blocker a stale comment produces — never a vacuous pass.
        result = _evaluate(comments=[_comment(build_approval_body(MARKER, HEAD))], head_sha="")
        self.assertStillRequiredAndBlocked(result)
        self.assertTrue(any("current head SHA" in b for b in result.blockers), result.blockers)

    def test_empty_head_with_no_comment_is_not_approved(self) -> None:
        result = _evaluate(comments=[], head_sha="")
        self.assertStillRequiredAndBlocked(result)

    def test_empty_head_comment_source_soft_blocks(self) -> None:
        sources, _approvers, _blockers, soft, _warnings = _comment_source(
            policy=POLICY,
            comments=[_comment(build_approval_body(MARKER, HEAD))],
            head_sha="",
            pr_author=GUARDIAN,
        )
        self.assertEqual(sources, [])
        self.assertTrue(any("current head SHA" in s for s in soft), soft)


class Defect3PositionalBindingTests(_RequirementMixin):
    def test_canonical_body_is_approved(self) -> None:
        result = _evaluate(comments=[_comment(build_approval_body(MARKER, HEAD))])
        self.assertTrue(result.approved, result)
        self.assertIn("guardian_comment", result.sources)

    def test_head_sha_quoted_in_prose_is_not_approved(self) -> None:
        # The bot's own status comment renders the head SHA in prose; a Guardian
        # quoting it while explaining a blocker must never satisfy the binding.
        body = f"{MARKER}\n\nApproving — the current head is {HEAD} as the bot shows above."
        result = _evaluate(comments=[_comment(body)])
        self.assertStillRequiredAndBlocked(result)

    def test_two_hex_tokens_is_not_approved(self) -> None:
        body = f"{MARKER}\n\n{HEAD}\n{HEAD}"
        result = _evaluate(comments=[_comment(body)])
        self.assertStillRequiredAndBlocked(result)
        self.assertTrue(any("more than one" in b for b in result.blockers), result.blockers)

    def test_abbreviated_sha_is_not_approved(self) -> None:
        body = build_approval_body(MARKER, HEAD[:12])
        result = _evaluate(comments=[_comment(body)])
        self.assertStillRequiredAndBlocked(result)
        self.assertTrue(any("abbreviated" in b for b in result.blockers), result.blockers)

    def test_mismatched_full_sha_is_not_approved(self) -> None:
        result = _evaluate(comments=[_comment(build_approval_body(MARKER, OTHER))])
        self.assertStillRequiredAndBlocked(result)

    def test_non_guardian_actor_rejected_before_sha_extraction(self) -> None:
        # A comment from a non-configured actor is rejected at the actor check and
        # is NEVER parsed for a SHA. The body carries TWO SHA tokens, which WOULD
        # yield the "more than one" binding blocker if extraction ran; asserting
        # that blocker's ABSENCE (alongside the actor blocker) proves the ORDER.
        body = f"{MARKER}\n\n{HEAD}\n{HEAD}"
        sources, _approvers, _blockers, soft, _warnings = _comment_source(
            policy=POLICY,
            comments=[_comment(body, author=NON_GUARDIAN)],
            head_sha=HEAD,
            pr_author=GUARDIAN,
        )
        self.assertEqual(sources, [])
        self.assertTrue(any("is not configured" in s for s in soft), soft)
        self.assertFalse(
            any("more than one" in s for s in soft),
            "a non-Guardian comment must never be parsed for a SHA",
        )

    def test_negation_comment_still_blocks(self) -> None:
        body = f"No {MARKER} for {HEAD}. This critical change is NOT authorized to merge."
        result = _evaluate(comments=[_comment(body)])
        self.assertStillRequiredAndBlocked(result)


class Defect1PaginationWorkflowTests(unittest.TestCase):
    """Every Guardian-evidence gh api call in BOTH workflows pages (--paginate)."""

    ENDPOINTS = ("comments", "events", "timeline")

    def _guardian_evidence_calls(self, text: str) -> list[str]:
        calls: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("gh api"):
                continue
            if any(f"/{endpoint}\"" in stripped for endpoint in self.ENDPOINTS):
                calls.append(stripped)
        return calls

    def test_both_workflows_paginate_guardian_evidence(self) -> None:
        for name in ("shiki-mergegate.yml", "shiki-cca-completion.yml"):
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            calls = self._guardian_evidence_calls(text)
            found = {endpoint for endpoint in self.ENDPOINTS
                     for call in calls if f"/{endpoint}\"" in call}
            self.assertEqual(found, set(self.ENDPOINTS), f"{name}: missing a Guardian-evidence call: {calls}")
            for call in calls:
                self.assertIn("--paginate", call, f"{name}: Guardian-evidence gh api call missing --paginate: {call}")


class Defect4PatchTargetWorkflowTests(unittest.TestCase):
    """The guardian-status comment is PATCHed only when authored by the bot."""

    def test_patch_selects_only_github_actions_bot(self) -> None:
        text = (WORKFLOWS / "shiki-cca-completion.yml").read_text(encoding="utf-8")
        picker = [
            line for line in text.splitlines()
            if "<!-- shiki:guardian-status -->" in line and "existing_id" in line
        ]
        self.assertTrue(picker, "guardian-status PATCH picker line not found")
        for line in picker:
            self.assertIn(".user.login", line, line)
            self.assertIn("github-actions[bot]", line, line)
        # It still posts a fresh comment when no bot comment exists.
        self.assertIn("gh pr comment", text)


if __name__ == "__main__":
    unittest.main()
