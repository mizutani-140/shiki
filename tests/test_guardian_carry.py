"""The base-sync carry (guardian_comment_carried), driven through the REAL evaluator.

Every test builds a REAL temporary git repository and lets
``evaluate_guardian_approval`` recompute the proof in-process via the real
``verify_pure_base_sync`` — git is never mocked. The one thing we spy on is the
proof CALL, wrapping the real function, so we can prove the candidate-independent
gates (risk, label escalation, retargeted base, edited comment, later negation)
are decided BEFORE the git proof ever runs, and that a non-configured actor's
comment is never even parsed for a SHA.

The carry salvages exactly ONE thing: a Guardian *comment* approval whose bound
SHA is a prior head, when the current head is a proven pure base sync of it. It
joins the human-secondary set alongside ``guardian_comment`` and
``github_review``; the ``guardian:approved`` label leg is always still required,
so removing the label defeats even a valid carry.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_guardian
from shiki_guardian import evaluate_guardian_approval, load_guardian_policy

REPO_ROOT = Path(shiki_test_support.SCRIPTS_DIR).parent

# Timestamps are fixed strings (ISO-8601 sorts lexically). Candidate is earlier;
# the negation used in the revocation test is strictly later.
T_CANDIDATE = "2026-08-01T00:00:00Z"
T_LATER = "2026-08-02T00:00:00Z"
T_EARLIER = "2026-07-31T00:00:00Z"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True)


def _rev(cwd: Path, ref: str) -> str:
    return _git(cwd, "rev-parse", ref).stdout.strip()


class _Repo:
    """A tiny builder over a real git repository (mirrors test_sync_proof)."""

    def __init__(self, path: Path):
        self.path = path
        _git(path, "init", "-q", "-b", "main")
        _git(path, "config", "user.email", "t@t")
        _git(path, "config", "user.name", "t")

    def commit(self, files: dict[str, str], message: str) -> str:
        for name, content in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-qm", message)
        return _rev(self.path, "HEAD")

    def checkout(self, ref: str, *, new_branch: str | None = None) -> None:
        if new_branch:
            _git(self.path, "checkout", "-q", "-b", new_branch, ref)
        else:
            _git(self.path, "checkout", "-q", ref)

    def merge(self, other: str, *, message: str = "sync") -> str:
        _git(self.path, "merge", "-q", "--no-ff", "-m", message, other)
        return _rev(self.path, "HEAD")

    def set_origin_main(self, sha: str) -> None:
        _git(self.path, "update-ref", "refs/remotes/origin/main", sha)


class _CarryBase(unittest.TestCase):
    """Shared fixture: a policy plus a real base-sync repo (a_sha -> head_sha)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_guardian_policy(REPO_ROOT)
        cls.marker = cls.policy.comment_marker
        cls.guardian = cls.policy.users[0]
        cls.label = cls.policy.label

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.repo = _Repo(self.dir)
        self.a_sha, self.head_sha = self._build_base_sync()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_base_sync(self) -> tuple[str, str]:
        """One clean base sync of disjoint work; origin/main sits at the merged base.

        The reviewed head ``a`` merges the advanced base ``b1`` into ``m``; the
        mechanical merge of ``a`` and the base re-derives tree(m), so
        ``verify_pure_base_sync`` proves it.
        """
        b0 = self.repo.commit({"shared.txt": "base\n", "README.md": "r\n"}, "base")
        self.repo.checkout(b0, new_branch="feat")
        a = self.repo.commit({"feature.txt": "feature\n"}, "A")
        self.repo.checkout("main")
        b1 = self.repo.commit({"other.txt": "o1\n"}, "B1")
        self.repo.checkout("feat")
        m = self.repo.merge(b1)
        self.repo.set_origin_main(b1)
        return a, m

    # -- fixture builders ----------------------------------------------------

    def _comment(self, sha: str, *, actor: str | None = None, created_at: str = T_CANDIDATE,
                 updated_at: str | None = None, negate: bool = False, body: str | None = None) -> dict:
        if body is None:
            if negate:
                body = f"No {self.marker} for {sha}. NOT authorized to merge."
            else:
                body = f"{self.marker}\n\n{sha}"
        comment: dict = {"user": {"login": actor or self.guardian}, "body": body, "created_at": created_at}
        if updated_at is not None:
            comment["updated_at"] = updated_at
        return comment

    def _pr(self, *, labels: list[str] | None = None, base_ref: str = "main") -> dict:
        label_names = labels if labels is not None else [self.label]
        return {
            "number": 55,
            "headRefOid": self.head_sha,
            "baseRefName": base_ref,
            "author": {"login": "implementer"},
            "labels": [{"name": name} for name in label_names],
        }

    def _events(self, *, extra: list[dict] | None = None) -> list[dict]:
        events = [{"event": "labeled", "label": {"name": self.label}, "actor": {"login": self.guardian}}]
        if extra:
            events.extend(extra)
        return events

    def evaluate(self, *, pr=None, comments=None, events=None, base_sync_carry=True,
                 default_branch="main", resolved_risk="high", head_sha=None):
        return evaluate_guardian_approval(
            policy=self.policy,
            pr=pr if pr is not None else self._pr(),
            reviews=[],
            comments=comments if comments is not None else [self._comment(self.a_sha)],
            label_events=events if events is not None else self._events(),
            head_sha=head_sha if head_sha is not None else self.head_sha,
            expected_repo="mizutani-140/shiki",
            base_sync_carry=base_sync_carry,
            default_branch=default_branch,
            carry_target=self.dir,
            resolved_risk=resolved_risk,
        )

    # -- shared assertions ---------------------------------------------------

    def assertCarried(self, result) -> None:
        self.assertTrue(result.approved, f"expected a carried approval: {result}")
        self.assertIn("guardian_comment_carried", result.sources, result)
        self.assertEqual(result.carried_from_head, self.a_sha, result)

    def assertStillRequired(self, result) -> None:
        """No carry happened AND the Guardian requirement still resolves required."""
        self.assertFalse(result.approved, f"carry must NOT approve: {result}")
        self.assertNotIn("guardian_comment_carried", result.sources, result)
        self.assertEqual(result.carried_from_head, "", result)
        self.assertTrue(
            any("Guardian" in blocker for blocker in result.blockers),
            f"a Guardian blocker must remain: {result.blockers}",
        )


class PositiveCarryTests(_CarryBase):
    def test_stale_comment_carries_across_a_proven_base_sync(self) -> None:
        result = self.evaluate()
        self.assertCarried(result)
        # The label leg is also satisfied; the carry is the human-secondary source.
        self.assertIn("guardian_label", result.sources)
        self.assertEqual(result.carry_refused_reasons, (), result)

    def test_carry_records_the_proven_prior_head(self) -> None:
        result = self.evaluate()
        self.assertEqual(result.carried_from_head, self.a_sha)
        self.assertNotEqual(self.a_sha, self.head_sha)


class FreshCommentWinsTests(_CarryBase):
    def test_current_head_comment_resolves_as_guardian_comment_not_carried(self) -> None:
        # Both a fresh current-head comment AND a stale one are present.
        result = self.evaluate(comments=[
            self._comment(self.a_sha, created_at=T_EARLIER),
            self._comment(self.head_sha, created_at=T_LATER),
        ])
        self.assertTrue(result.approved, result)
        self.assertIn("guardian_comment", result.sources)
        self.assertNotIn("guardian_comment_carried", result.sources)
        self.assertEqual(result.carried_from_head, "")


class RiskGateTests(_CarryBase):
    def test_critical_risk_refuses_before_the_git_proof(self) -> None:
        with mock.patch.object(
            shiki_guardian, "verify_pure_base_sync", wraps=shiki_guardian.verify_pure_base_sync
        ) as spy:
            result = self.evaluate(resolved_risk="critical")
        self.assertStillRequired(result)
        self.assertFalse(spy.called, "the git proof must not run once risk refuses the carry")
        self.assertTrue(any("not exactly high" in r for r in result.carry_refused_reasons), result)

    def test_label_only_escalation_refuses_before_the_git_proof(self) -> None:
        # Task risk is high, but a maintainer added a risk:critical LABEL.
        pr = self._pr(labels=[self.label, "risk:critical"])
        with mock.patch.object(
            shiki_guardian, "verify_pure_base_sync", wraps=shiki_guardian.verify_pure_base_sync
        ) as spy:
            result = self.evaluate(pr=pr, resolved_risk="high")
        self.assertStillRequired(result)
        self.assertFalse(spy.called, "a label escalation must refuse before the git proof")
        self.assertTrue(any("label escalation" in r for r in result.carry_refused_reasons), result)


class BaseGateTests(_CarryBase):
    def test_retargeted_base_refuses_before_the_git_proof(self) -> None:
        pr = self._pr(base_ref="release-1.x")
        with mock.patch.object(
            shiki_guardian, "verify_pure_base_sync", wraps=shiki_guardian.verify_pure_base_sync
        ) as spy:
            result = self.evaluate(pr=pr)
        self.assertStillRequired(result)
        self.assertFalse(spy.called)
        self.assertTrue(any("not the default branch" in r for r in result.carry_refused_reasons), result)

    def test_base_ref_changed_timeline_entry_refuses(self) -> None:
        events = self._events(extra=[{"event": "base_ref_changed", "actor": {"login": self.guardian}}])
        with mock.patch.object(
            shiki_guardian, "verify_pure_base_sync", wraps=shiki_guardian.verify_pure_base_sync
        ) as spy:
            result = self.evaluate(events=events)
        self.assertStillRequired(result)
        self.assertFalse(spy.called)
        self.assertTrue(any("retargeted" in r for r in result.carry_refused_reasons), result)


class CommentShapeTests(_CarryBase):
    def test_edited_comment_refuses_before_the_git_proof(self) -> None:
        comment = self._comment(self.a_sha, created_at=T_CANDIDATE, updated_at=T_LATER)
        with mock.patch.object(
            shiki_guardian, "verify_pure_base_sync", wraps=shiki_guardian.verify_pure_base_sync
        ) as spy:
            result = self.evaluate(comments=[comment])
        self.assertStillRequired(result)
        self.assertFalse(spy.called, "an edited comment must refuse before the git proof")
        self.assertTrue(any("edited" in r for r in result.carry_refused_reasons), result)

    def test_unedited_comment_with_equal_timestamps_still_carries(self) -> None:
        comment = self._comment(self.a_sha, created_at=T_CANDIDATE, updated_at=T_CANDIDATE)
        self.assertCarried(self.evaluate(comments=[comment]))

    def test_two_sha_body_is_never_a_candidate(self) -> None:
        body = f"{self.marker}\n\n{self.a_sha}\n{self.a_sha}"
        comment = self._comment(self.a_sha, body=body)
        with mock.patch.object(
            shiki_guardian, "verify_pure_base_sync", wraps=shiki_guardian.verify_pure_base_sync
        ) as spy:
            result = self.evaluate(comments=[comment])
        self.assertStillRequired(result)
        self.assertFalse(spy.called, "a two-SHA body is never a carry candidate")


class ActorOrderingTests(_CarryBase):
    def test_non_configured_actor_is_dropped_before_extraction(self) -> None:
        # A comment from a non-Guardian carrying a perfectly valid prior head must
        # never even be parsed for a SHA — the git proof must not run for it.
        comment = self._comment(self.a_sha, actor="random-user")
        with mock.patch.object(
            shiki_guardian, "verify_pure_base_sync", wraps=shiki_guardian.verify_pure_base_sync
        ) as spy:
            result = self.evaluate(comments=[comment])
        self.assertStillRequired(result)
        self.assertFalse(spy.called, "a non-configured actor's comment must never be parsed for a carry SHA")

    def test_configured_actor_reaches_the_proof(self) -> None:
        # Positive control: a configured Guardian's stale comment DOES reach the
        # proof, with the candidate's SHA as a_sha and the current head as head_sha.
        with mock.patch.object(
            shiki_guardian, "verify_pure_base_sync", wraps=shiki_guardian.verify_pure_base_sync
        ) as spy:
            result = self.evaluate()
        self.assertCarried(result)
        self.assertTrue(spy.called)
        _, kwargs = spy.call_args
        self.assertEqual(kwargs["a_sha"], self.a_sha)
        self.assertEqual(kwargs["head_sha"], self.head_sha)


class NegationTests(_CarryBase):
    def test_later_negation_refuses_and_is_a_hard_blocker(self) -> None:
        comments = [
            self._comment(self.a_sha, created_at=T_CANDIDATE),
            self._comment(self.a_sha, created_at=T_LATER, negate=True),
        ]
        result = self.evaluate(comments=comments)
        self.assertStillRequired(result)
        hard = "Guardian approval carry refused: a configured Guardian posted a negation"
        # HARD: the revocation is in blockers, NOT merely a warning.
        self.assertTrue(any(hard in b for b in result.blockers), f"expected hard blocker: {result.blockers}")
        self.assertFalse(any(hard in w for w in result.warnings), f"must not be a warning: {result.warnings}")

    def test_earlier_negation_does_not_defeat_a_later_candidate(self) -> None:
        # A negation strictly BEFORE the candidate does not defeat it.
        comments = [
            self._comment(self.a_sha, created_at=T_EARLIER, negate=True),
            self._comment(self.a_sha, created_at=T_LATER),
        ]
        self.assertCarried(self.evaluate(comments=comments))

    def test_markerless_guardian_revocation_defeats_the_carry(self) -> None:
        # A configured Guardian's freehand revocation (no approval marker) must
        # still defeat the carry with a HARD blocker; a Guardian should not have to
        # paste the exact marker to revoke.
        comments = [
            self._comment(self.a_sha, created_at=T_CANDIDATE),
            {"user": {"login": self.guardian},
             "body": "Do NOT merge. I revoke my Guardian approval.",
             "created_at": T_LATER},
        ]
        result = self.evaluate(comments=comments)
        self.assertStillRequired(result)
        hard = "Guardian approval carry refused: a configured Guardian posted a negation"
        self.assertTrue(any(hard in b for b in result.blockers), f"expected hard blocker: {result.blockers}")
        self.assertFalse(any(hard in w for w in result.warnings), f"must not be a warning: {result.warnings}")

    def test_markerless_revocation_from_non_guardian_does_not_defeat(self) -> None:
        # Only a CONFIGURED Guardian's revocation defeats the carry.
        comments = [
            self._comment(self.a_sha, created_at=T_CANDIDATE),
            {"user": {"login": "random-user"},
             "body": "I revoke my Guardian approval.",
             "created_at": T_LATER},
        ]
        self.assertCarried(self.evaluate(comments=comments))


class LabelRevocationTests(_CarryBase):
    def test_removing_the_label_defeats_a_valid_carry(self) -> None:
        # The carry would succeed, but the guardian:approved label is absent.
        pr = self._pr(labels=[])
        result = self.evaluate(pr=pr)
        self.assertFalse(result.approved, f"a missing label must defeat approval: {result}")
        self.assertTrue(
            any("Guardian label" in b and "missing" in b for b in result.blockers),
            result.blockers,
        )


class CrossPrTests(_CarryBase):
    def test_sha_from_another_pr_is_never_carried(self) -> None:
        # A SHA copied from an unrelated PR is a real 40-hex object that is not a
        # local object here; the proof refuses it (step 4), so the carry refuses.
        with tempfile.TemporaryDirectory() as other:
            other_repo = _Repo(Path(other))
            orphan = other_repo.commit({"gone.txt": "x\n"}, "orphan")
        result = self.evaluate(comments=[self._comment(orphan)])
        self.assertStillRequired(result)
        self.assertTrue(
            any("carry refused" in r for r in result.carry_refused_reasons), result
        )


class FeatureSwitchTests(_CarryBase):
    def test_empty_default_branch_behaves_exactly_as_the_pre_change_gate(self) -> None:
        off = self.evaluate(base_sync_carry=False, default_branch="")
        empty = self.evaluate(base_sync_carry=True, default_branch="")
        self.assertEqual(off.approved, empty.approved)
        self.assertEqual(off.sources, empty.sources)
        self.assertEqual(off.blockers, empty.blockers)
        self.assertEqual(off.warnings, empty.warnings)
        # The pre-change gate: a stale comment leaves the requirement required.
        self.assertFalse(off.approved)
        self.assertEqual(off.carried_from_head, "")
        self.assertEqual(off.carry_refused_reasons, ())

    def test_flag_off_never_runs_the_git_proof(self) -> None:
        with mock.patch.object(
            shiki_guardian, "verify_pure_base_sync", wraps=shiki_guardian.verify_pure_base_sync
        ) as spy:
            self.evaluate(base_sync_carry=False, default_branch="main")
        self.assertFalse(spy.called, "carry off must never run the git proof")


class PolicyKeyTests(_CarryBase):
    def test_no_invented_policy_key_can_enable_the_carry(self) -> None:
        # An invented guardian-policy key must be inert: only the --base-sync-carry
        # FLAG enables the carry, never any policy field.
        import json

        from shiki_guardian import load_guardian_policy_file

        data = json.loads((REPO_ROOT / ".shiki" / "guardian-policy.json").read_text(encoding="utf-8"))
        data["base_sync_carry"] = True
        data["approval_sources"]["guardian_comment"]["base_sync_carry_enabled"] = True
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "guardian-policy.json"
            policy_path.write_text(json.dumps(data), encoding="utf-8")
            invented = load_guardian_policy_file(policy_path)
        result = evaluate_guardian_approval(
            policy=invented,
            pr=self._pr(),
            reviews=[],
            comments=[self._comment(self.a_sha)],
            label_events=self._events(),
            head_sha=self.head_sha,
            expected_repo="mizutani-140/shiki",
            base_sync_carry=False,   # flag OFF despite the invented policy keys
            default_branch="main",
            carry_target=self.dir,
            resolved_risk="high",
        )
        self.assertFalse(result.approved, f"an invented policy key must not enable the carry: {result}")
        self.assertNotIn("guardian_comment_carried", result.sources)
        self.assertEqual(result.carried_from_head, "")


if __name__ == "__main__":
    unittest.main()
