"""Behavior tests for ``shiki guardian status`` (T-...-628504fe).

These exercise the public surface of ``shiki_guardian_status`` — the report
builder, the paste-ready approval body, head-SHA stability detection, the two
render formats, and the offline CLI path. They never hit the network: live gh
gathering is bypassed via the offline ``--pr-json`` mode (the same seam the CCA
workflow uses). The decisive behavior test is the round-trip: the body this
command prints, posted by a configured Guardian, actually satisfies the
authoritative ``evaluate_guardian_approval``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import shiki_guardian_status as status
from shiki_guardian import evaluate_guardian_approval, load_guardian_policy_file

HEAD = "a" * 40
MARKER = "Guardian approval granted"
LABEL = "guardian:approved"
REPO = "mizutani-140/shiki"
GUARDIAN = "mizutani-140"

POLICY_DATA = {
    "version": 1,
    "description": "test",
    "applies_to_risk": ["high", "critical"],
    "approvers": {"users": [GUARDIAN], "teams": []},
    "approval_sources": {
        "github_review": {"enabled": True, "require_approved_state": True},
        "guardian_label": {"enabled": True, "label": LABEL, "require_label_actor": True},
        "guardian_comment": {"enabled": True, "marker": MARKER, "require_head_sha": True},
    },
    "solo_maintainer": {
        "enabled": True,
        "allow_pr_author_as_guardian": True,
        "rationale": "solo maintainer test",
    },
    "exclusions": {
        "github_actions_review_bridge_counts_as_guardian": False,
        "advisory_claude_review_counts_as_guardian": False,
    },
}


def _policy(root: Path):
    path = root / "guardian-policy.json"
    path.write_text(json.dumps(POLICY_DATA), encoding="utf-8")
    return load_guardian_policy_file(path)


def _pr(labels=(LABEL,), *, number=130):
    return {
        "number": number,
        "headRefOid": HEAD,
        "author": {"login": GUARDIAN},
        "labels": [{"name": name} for name in labels],
        "body": "T-0001",
    }


def _label_events():
    return [{"event": "labeled", "label": {"name": LABEL}, "actor": {"login": GUARDIAN}}]


def _report(root: Path, *, comments=(), reviews=(), sha_will_change=False, reasons=(), head=HEAD):
    return status.build_status_report(
        policy=_policy(root),
        pr=_pr(),
        reviews=list(reviews),
        comments=list(comments),
        label_events=_label_events(),
        head_sha=head,
        expected_repo=REPO,
        repository=REPO,
        pr_number=130,
        sha_will_change=sha_will_change,
        sha_change_reasons=tuple(reasons),
    )


class ApprovalBodyTests(unittest.TestCase):
    def test_body_carries_marker_line_initial_and_full_sha(self) -> None:
        body = status.build_approval_body(MARKER, HEAD)
        self.assertTrue(body.splitlines()[0].startswith(MARKER))
        self.assertIn(HEAD, body)

    def test_body_round_trips_through_the_authoritative_evaluator(self) -> None:
        # The decisive check: the printed body, posted verbatim by a configured
        # Guardian, must satisfy the SAME evaluator MergeGate uses.
        with tempfile.TemporaryDirectory() as tmp:
            policy = _policy(Path(tmp))
        body = status.build_approval_body(MARKER, HEAD)
        result = evaluate_guardian_approval(
            policy=policy,
            pr=_pr(),
            reviews=[],
            comments=[{"user": {"login": GUARDIAN}, "body": body}],
            label_events=_label_events(),
            head_sha=HEAD,
            expected_repo=REPO,
        )
        self.assertTrue(result.approved)
        self.assertIn("guardian_comment", result.sources)


class ShaChangeTests(unittest.TestCase):
    def test_clean_tree_is_stable(self) -> None:
        changed, reasons = status.detect_sha_change(local_head=HEAD, live_head=HEAD, porcelain_paths=[])
        self.assertFalse(changed)
        self.assertEqual(reasons, [])

    def test_unpushed_commit_changes_sha(self) -> None:
        changed, reasons = status.detect_sha_change(local_head="b" * 40, live_head=HEAD, porcelain_paths=[])
        self.assertTrue(changed)
        self.assertTrue(any("about to change" in r for r in reasons))

    def test_unsynced_ledger_evidence_changes_sha(self) -> None:
        changed, reasons = status.detect_sha_change(
            local_head=HEAD, live_head=HEAD, porcelain_paths=[".shiki/ledger/L-1.json"]
        )
        self.assertTrue(changed)
        self.assertTrue(any("ledger evidence" in r for r in reasons))

    def test_other_uncommitted_change_changes_sha(self) -> None:
        changed, reasons = status.detect_sha_change(
            local_head=HEAD, live_head=HEAD, porcelain_paths=["scripts/x.py"]
        )
        self.assertTrue(changed)
        self.assertTrue(any("Uncommitted working-tree changes" in r for r in reasons))


class StatusReportTests(unittest.TestCase):
    def test_label_only_names_missing_comment_and_prints_full_sha_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = _report(Path(tmp), comments=[])
        self.assertFalse(report.approved)
        self.assertIn("guardian_label", [source for source, _ in report.satisfied])
        self.assertTrue(any("review or current-head" in m for m in report.missing))
        self.assertIsNotNone(report.approval_body)
        self.assertIn(MARKER, report.approval_body)
        self.assertIn(HEAD, report.approval_body)  # full 40-char SHA, not abbreviated

    def test_both_present_reports_approved_and_names_approver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = _report(
                Path(tmp),
                comments=[{"user": {"login": GUARDIAN}, "body": status.build_approval_body(MARKER, HEAD)}],
            )
        self.assertTrue(report.approved)
        by = " ".join(by for _, by in report.satisfied)
        self.assertIn(GUARDIAN, by)

    def test_sha_about_to_change_withholds_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = _report(Path(tmp), sha_will_change=True, reasons=("Local HEAD differs …",))
        self.assertIsNone(report.approval_body)
        self.assertIn("about to change", report.approval_body_withheld_reason)
        rendered = status.render_report(report, fmt="text")
        self.assertIn("WITHHELD", rendered)

    def test_non_full_sha_withholds_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = _report(Path(tmp), head="abc123")
        self.assertIsNone(report.approval_body)
        self.assertIn("full 40-character", report.approval_body_withheld_reason)

    def test_comment_format_carries_marker_blockers_and_paste_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = _report(Path(tmp), comments=[])
        rendered = status.render_report(report, fmt="comment")
        self.assertIn(status.COMMENT_MARKER, rendered)  # hidden marker for idempotent update
        self.assertIn("What the gate is waiting for", rendered)
        self.assertTrue(any(m in rendered for m in report.missing))
        self.assertIn(MARKER, rendered)  # paste-ready body
        self.assertIn(HEAD, rendered)


class OfflineCliTests(unittest.TestCase):
    def _write_evidence(self, root: Path, comments):
        (root / "pr.json").write_text(json.dumps(_pr()), encoding="utf-8")
        (root / "comments.json").write_text(json.dumps(comments), encoding="utf-8")
        (root / "events.json").write_text(json.dumps(_label_events()), encoding="utf-8")
        (root / "guardian-policy.json").write_text(json.dumps(POLICY_DATA), encoding="utf-8")

    def test_offline_end_to_end_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_evidence(root, comments=[])
            out = root / "report.txt"
            rc = status.main_with_argv([
                "--target", str(root),
                "--pr", "130",
                "--repo", REPO,
                "--guardian-policy", str(root / "guardian-policy.json"),
                "--pr-json", str(root / "pr.json"),
                "--comments", str(root / "comments.json"),
                "--events", str(root / "events.json"),
                "--no-git",
                "--output", str(out),
            ])
            self.assertEqual(rc, 0)
            rendered = out.read_text(encoding="utf-8")
        self.assertIn("NOT APPROVED", rendered)
        self.assertIn(HEAD, rendered)
        self.assertIn(MARKER, rendered)

    def test_offline_pr_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_evidence(root, comments=[])
            with self.assertRaises(Exception):
                status.main_with_argv([
                    "--target", str(root),
                    "--pr", "999",
                    "--repo", REPO,
                    "--guardian-policy", str(root / "guardian-policy.json"),
                    "--pr-json", str(root / "pr.json"),
                    "--no-git",
                ])


if __name__ == "__main__":
    unittest.main()
