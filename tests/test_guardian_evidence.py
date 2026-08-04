"""Guardian approval evidence must be current and revocable.

These tests pin the behavior that lets a recorded Guardian approval reach a
verdict on its own (the CCA re-runs on label changes) and that a negation,
revocation, or superseded label transition can never masquerade as approval:

- the ``guardian_comment`` marker must be *line-initial*, so a comment that only
  mentions the marker inside a negating sentence ("No Guardian approval
  granted…", "NOT approving. Guardian approval granted…") never approves;
- a negated/revoked marker is recorded as a soft blocker (auditable in the gate
  JSON), never a silent pass;
- ``_valid_label_actor`` resolves the LATEST labeled/unlabeled transition, so a
  label removed-then-relabeled by a non-Guardian (or simply revoked) does not
  keep an earlier Guardian labeling alive;
- ``shiki-cca-completion.yml`` re-runs on ``labeled``/``unlabeled`` and pins its
  MergeGate job to read-only permissions, enforced by ``validate_shiki``.
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import validate_shiki
from shiki_guardian import (
    GuardianPolicy,
    _comment_source,
    _valid_label_actor,
    evaluate_guardian_approval,
    load_guardian_policy,
)
from shiki_workflows import workflow_job_permissions

REPO_ROOT = Path(__file__).resolve().parents[1]
HEAD = "a" * 40
GUARDIAN = "mizutani-140"
MALLORY = "mallory"

POLICY: GuardianPolicy = load_guardian_policy(REPO_ROOT)


def _labeled(actor: str, event: str = "labeled") -> dict:
    return {"event": event, "label": {"name": "guardian:approved"}, "actor": {"login": actor}}


def _pr(labels=("guardian:approved",), author: str = GUARDIAN) -> dict:
    return {
        "number": 55,
        "headRefOid": HEAD,
        "author": {"login": author},
        "labels": [{"name": name} for name in labels],
    }


def _evaluate(*, comments=(), reviews=(), events=None, pr=None):
    return evaluate_guardian_approval(
        policy=POLICY,
        pr=pr or _pr(),
        reviews=list(reviews),
        comments=list(comments),
        label_events=[_labeled(GUARDIAN)] if events is None else list(events),
        head_sha=HEAD,
        expected_repo="mizutani-140/shiki",
    )


def _comment(body: str, author: str = GUARDIAN) -> dict:
    return {"author": {"login": author}, "body": body}


class CommentMarkerLineInitialTests(unittest.TestCase):
    def test_loose_prose_marker_does_not_approve(self) -> None:
        # Defect 3: the loose form — the head SHA quoted inside the marker's prose
        # line rather than standing on its own line — is NOT a positional binding
        # and must NOT approve. A Guardian quoting the head SHA while explaining a
        # blocker must never be read as an affirmative approval.
        result = _evaluate(comments=[_comment(f"Guardian approval granted for head {HEAD}")])
        self.assertFalse(result.approved, result)
        self.assertNotIn("guardian_comment", result.sources)

    def test_line_initial_marker_with_blank_line_approves(self) -> None:
        result = _evaluate(comments=[_comment(f"Guardian approval granted\n\n{HEAD}")])
        self.assertTrue(result.approved, result)

    def test_negated_marker_without_prefix_blocks(self) -> None:
        body = f"No Guardian approval granted for {HEAD}. This critical change is NOT authorized to merge."
        result = _evaluate(comments=[_comment(body)])
        self.assertFalse(result.approved, result)

    def test_withheld_marker_blocks(self) -> None:
        body = f"NOT approving. Guardian approval granted is withheld until CI is green for {HEAD}."
        result = _evaluate(comments=[_comment(body)])
        self.assertFalse(result.approved, result)

    def test_revoked_marker_blocks(self) -> None:
        body = f"I am revoking my earlier approval. Guardian approval granted at {HEAD} is RESCINDED."
        result = _evaluate(comments=[_comment(body)])
        self.assertFalse(result.approved, result)

    def test_markerless_comment_does_not_emit_a_negation_blocker(self) -> None:
        # An unrelated comment that merely contains a cue word ("revoked the
        # deploy token") is not a "negated marker": it must not pollute the gate
        # with a spurious soft blocker.
        sources, approvers, blockers, soft, warnings = _comment_source(
            policy=POLICY,
            comments=[_comment("Reminder: we revoked the old deploy token yesterday.")],
            head_sha=HEAD,
            pr_author=GUARDIAN,
        )
        self.assertEqual((sources, blockers, soft, warnings), ([], [], [], []))

    def test_negated_marker_records_soft_blocker_not_silent_pass(self) -> None:
        # A negated marker must leave an auditable trace in the gate JSON.
        body = f"No Guardian approval granted for {HEAD}. NOT authorized to merge."
        sources, approvers, blockers, soft, warnings = _comment_source(
            policy=POLICY,
            comments=[_comment(body)],
            head_sha=HEAD,
            pr_author=GUARDIAN,
        )
        self.assertEqual(sources, [])
        self.assertTrue(soft, "a negated marker must produce a soft blocker")

    def test_negated_marker_is_soft_not_hard(self) -> None:
        # A negated comment alongside a valid approval is demoted to a warning:
        # it must not poison an otherwise-current approval (proves it is soft).
        result = _evaluate(
            comments=[
                _comment(f"Guardian approval granted\n\n{HEAD}"),
                _comment(f"Guardian approval granted earlier is now RESCINDED at {HEAD}."),
            ]
        )
        self.assertTrue(result.approved, result)
        self.assertTrue(
            any("negate" in msg or "revoke" in msg for msg in result.warnings),
            result,
        )


class LabelActorLatestTransitionTests(unittest.TestCase):
    def test_relabel_by_non_guardian_is_latest_and_blocks(self) -> None:
        events = [_labeled(GUARDIAN), _labeled(GUARDIAN, "unlabeled"), _labeled(MALLORY)]
        ok, actor = _valid_label_actor(POLICY, events, GUARDIAN)
        self.assertFalse(ok)
        self.assertIsNone(actor)

    def test_relabel_by_guardian_is_latest_and_accepts(self) -> None:
        events = [_labeled(MALLORY), _labeled(MALLORY, "unlabeled"), _labeled(GUARDIAN)]
        ok, actor = _valid_label_actor(POLICY, events, GUARDIAN)
        self.assertTrue(ok)
        self.assertEqual(actor, GUARDIAN)

    def test_final_unlabel_is_not_an_approval(self) -> None:
        events = [_labeled(GUARDIAN), _labeled(GUARDIAN, "unlabeled")]
        ok, _actor = _valid_label_actor(POLICY, events, GUARDIAN)
        self.assertFalse(ok)

    def test_latest_resolved_by_timestamp_not_list_order(self) -> None:
        # The events+timeline feed the workflow builds is not guaranteed to be
        # chronological, so "latest" must follow created_at, not array position.
        # Here the non-Guardian relabel is the most RECENT transition but appears
        # FIRST in the list: it must still win and block.
        def stamped(actor: str, event: str, created_at: str) -> dict:
            return {**_labeled(actor, event), "created_at": created_at}

        events = [
            stamped(MALLORY, "labeled", "2026-07-29T03:00:00Z"),
            stamped(GUARDIAN, "labeled", "2026-07-29T01:00:00Z"),
            stamped(GUARDIAN, "unlabeled", "2026-07-29T02:00:00Z"),
        ]
        ok, actor = _valid_label_actor(POLICY, events, GUARDIAN)
        self.assertFalse(ok, actor)

    def test_full_gate_blocks_when_latest_label_actor_is_not_guardian(self) -> None:
        events = [_labeled(GUARDIAN), _labeled(GUARDIAN, "unlabeled"), _labeled(MALLORY)]
        result = _evaluate(
            comments=[_comment(f"Guardian approval granted\n\n{HEAD}")],
            events=events,
        )
        self.assertFalse(result.approved, result)
        # The label blocker names the label value, e.g.
        # "Guardian label 'guardian:approved' was not applied by a configured Guardian".
        self.assertTrue(
            any("was not applied by a configured Guardian" in b for b in result.blockers),
            result.blockers,
        )

    def test_full_gate_approves_when_latest_label_actor_is_guardian(self) -> None:
        events = [_labeled(MALLORY), _labeled(MALLORY, "unlabeled"), _labeled(GUARDIAN)]
        result = _evaluate(
            comments=[_comment(f"Guardian approval granted\n\n{HEAD}")],
            events=events,
        )
        self.assertTrue(result.approved, result)


class CcaCompletionWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = REPO_ROOT / ".github" / "workflows" / "shiki-cca-completion.yml"
        self.model = validate_shiki.load_workflow_contract(self.path)

    def _pull_request_types(self) -> list[str]:
        on = self.model.get("on") or {}
        pull_request = on.get("pull_request") or {}
        return list(pull_request.get("types") or [])

    def test_reruns_on_label_change(self) -> None:
        types = self._pull_request_types()
        self.assertIn("labeled", types)
        self.assertIn("unlabeled", types)

    def test_mergegate_job_permissions_are_read_only(self) -> None:
        permissions = workflow_job_permissions(self.model, "mergegate")
        self.assertTrue(permissions, "mergegate job must pin an explicit permissions block")
        self.assertTrue(all(value == "read" for value in permissions.values()), permissions)

    def test_contract_pins_mergegate_read_only(self) -> None:
        contract = validate_shiki.WORKFLOW_CONTRACTS["shiki-cca-completion.yml"]
        job_permissions = contract.get("job_permissions", {})
        self.assertIn("mergegate", job_permissions)
        self.assertTrue(all(value == "read" for value in job_permissions["mergegate"].values()))
        # The pinned contract must match what the workflow actually declares.
        self.assertEqual(
            workflow_job_permissions(self.model, "mergegate"),
            job_permissions["mergegate"],
        )

    def test_removing_mergegate_permissions_breaks_the_contract(self) -> None:
        contract = validate_shiki.WORKFLOW_CONTRACTS["shiki-cca-completion.yml"]
        expected = contract["job_permissions"]["mergegate"]
        mutated = copy.deepcopy(self.model)
        mutated["jobs"]["mergegate"].pop("permissions", None)
        # This is exactly the comparison validate_workflow_contracts performs;
        # removing the block must make it diverge from the pinned contract.
        self.assertNotEqual(workflow_job_permissions(mutated, "mergegate"), expected)


if __name__ == "__main__":
    unittest.main()
