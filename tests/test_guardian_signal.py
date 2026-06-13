"""Deterministic Guardian-approval signal for the CCA judge (ADR 0010).

The signal must resolve risk the SAME way MergeGate does (by the PR-body task
id), and must FAIL CLOSED whenever the authoritative task risk cannot be
determined, so a missing/null/stale risk can never downgrade a high-risk PR to
"not required".
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import guardian_approval_signal as sig

HEAD = "a" * 40
TASK_ID = "T-0001"
POLICY = {
    "version": 1,
    "description": "test",
    "applies_to_risk": ["high", "critical"],
    "approvers": {"users": ["mizutani-140"], "teams": []},
    "approval_sources": {
        "external_ai_guardian_review": {
            "enabled": True,
            "fence": "external-ai-guardian-review",
            "require_head_sha": True,
            "allowed_models": ["GPT-5.5 Pro"],
            "allowed_roles": ["external_guardian_reviewer"],
            "not_operator_approval": True,
        }
    },
    "solo_maintainer": {
        "enabled": True,
        "allow_pr_author_as_guardian": True,
        "rationale": "solo maintainer",
    },
}
ARTIFACT = (
    "```external-ai-guardian-review\n"
    '{"kind":"external_ai_guardian_review","reviewer":{"type":"ai_model",'
    '"model":"GPT-5.5 Pro","role":"external_guardian_reviewer"},'
    '"repo":"mizutani-140/shiki","pr":130,"head_sha":"' + HEAD + '",'
    '"verdict":"approve","merge_permission":"autonomous_merge_permitted",'
    '"not_operator_approval":true}\n```'
)


def _run(root: Path, *, labels, task_risk, comments, body=None, task_json=None):
    (root / ".shiki" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / ".shiki" / "guardian-policy.json").write_text(json.dumps(POLICY), encoding="utf-8")
    if task_json is not None:
        (root / ".shiki" / "tasks" / f"{TASK_ID}.json").write_text(task_json, encoding="utf-8")
    elif task_risk is not None:
        (root / ".shiki" / "tasks" / f"{TASK_ID}.json").write_text(
            json.dumps({"id": TASK_ID, "goal_id": "G-0001", "expected_pr": 130, "risk_level": task_risk}),
            encoding="utf-8",
        )
    pr = {
        "number": 130,
        "headRefOid": HEAD,
        "author": {"login": "mizutani-140"},
        "labels": [{"name": n} for n in labels],
        "body": (TASK_ID if body is None else body),
    }
    (root / "pr.json").write_text(json.dumps(pr), encoding="utf-8")
    (root / "comments.json").write_text(json.dumps(comments), encoding="utf-8")
    out = root / "signal.json"
    rc = sig.main_with_argv([
        "--pr-json", str(root / "pr.json"),
        "--guardian-policy", str(root / ".shiki" / "guardian-policy.json"),
        "--guardian-comments", str(root / "comments.json"),
        "--expected-repository", "mizutani-140/shiki",
        "--shiki-root", str(root),
        "--output", str(out),
    ])
    assert rc == 0
    return json.loads(out.read_text(encoding="utf-8"))


class GuardianSignalTests(unittest.TestCase):
    def test_task_risk_gates_even_without_pr_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal = _run(Path(tmp), labels=[], task_risk="high", comments=[])
        self.assertTrue(signal["required"])  # task risk via PR-body id, not PR label
        self.assertFalse(signal["approved"])  # no approval present

    def test_valid_ai_artifact_approves_with_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal = _run(Path(tmp), labels=[], task_risk="high",
                          comments=[{"user": {"login": "mizutani-140"}, "body": ARTIFACT}])
        self.assertTrue(signal["required"])
        self.assertTrue(signal["approved"])
        self.assertIn("external_ai_guardian_review", signal["sources"])
        self.assertIn("GPT-5.5 Pro", signal["ai_reviewers"])
        self.assertNotIn("mizutani-140", signal["approvers"])

    def test_low_risk_not_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal = _run(Path(tmp), labels=[], task_risk="low", comments=[])
        self.assertFalse(signal["required"])
        self.assertTrue(signal["approved"])

    def test_no_task_id_in_body_fails_closed(self) -> None:
        # The PR body carries no Shiki task id: risk is undeterminable -> require.
        with tempfile.TemporaryDirectory() as tmp:
            signal = _run(Path(tmp), labels=[], task_risk="high", comments=[], body="no task here")
        self.assertTrue(signal["required"])
        self.assertFalse(signal["approved"])

    def test_corrupt_task_file_fails_closed(self) -> None:
        # The body references a task whose file is corrupt: fail closed.
        with tempfile.TemporaryDirectory() as tmp:
            signal = _run(Path(tmp), labels=[], task_risk=None, comments=[], task_json="{ not json")
        self.assertTrue(signal["required"])
        self.assertFalse(signal["approved"])

    def test_missing_risk_level_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal = _run(Path(tmp), labels=[], task_risk=None, comments=[],
                          task_json=json.dumps({"id": TASK_ID, "goal_id": "G-0001", "expected_pr": 130}))
        self.assertTrue(signal["required"])
        self.assertFalse(signal["approved"])

    def test_pr_label_cannot_downgrade_undetermined_risk(self) -> None:
        # Even a risk:low PR label cannot make an undeterminable-risk PR "not
        # required" — fail closed wins.
        with tempfile.TemporaryDirectory() as tmp:
            signal = _run(Path(tmp), labels=["risk:low"], task_risk=None, comments=[], body="no task id")
        self.assertTrue(signal["required"])

    def test_unreadable_policy_fails_closed_when_high_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".shiki" / "tasks").mkdir(parents=True, exist_ok=True)
            (root / ".shiki" / "tasks" / f"{TASK_ID}.json").write_text(
                json.dumps({"id": TASK_ID, "goal_id": "G-0001", "expected_pr": 130, "risk_level": "critical"}),
                encoding="utf-8",
            )
            (root / "pr.json").write_text(
                json.dumps({"number": 130, "headRefOid": HEAD, "labels": [], "body": TASK_ID}), encoding="utf-8")
            out = root / "signal.json"
            rc = sig.main_with_argv([
                "--pr-json", str(root / "pr.json"),
                "--guardian-policy", str(root / ".shiki" / "missing-policy.json"),
                "--expected-repository", "mizutani-140/shiki",
                "--shiki-root", str(root),
                "--output", str(out),
            ])
            self.assertEqual(rc, 0)
            signal = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(signal["required"])
        self.assertFalse(signal["approved"])


if __name__ == "__main__":
    unittest.main()
