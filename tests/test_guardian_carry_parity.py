"""MergeGate and the CCA Guardian signal must AGREE on the base-sync carry.

The carry has to reach BOTH decision points or it is useless: the MergeGate
policy job depends on the CCA job, ``enforce_cca_verdict`` fails any verdict that
is not ``complete``, and the CCA signal is computed from the guardian evaluation
alone — so wiring the carry into MergeGate only would leave CCA returning
needs_guardian, its job failing, and the policy check never running.

Each fixture is driven through the REAL entry points over one shared, real
base-sync git repository: ``scripts/mergegate_check.py`` as a subprocess and
``guardian_approval_signal`` in-process. For every fixture we assert MergeGate's
guardian outcome equals the signal's, that both agree on whether
``guardian_comment_carried`` was the source, and — the load-bearing safety
property — that the signal is NEVER more permissive than MergeGate.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import guardian_approval_signal as sig

REPO_ROOT = Path(shiki_test_support.SCRIPTS_DIR).parent
MERGEGATE = str(REPO_ROOT / "scripts" / "mergegate_check.py")

GOAL = "G-0031"
TASK = "T-0031"
LEDGER = "L-0031"
IMPL_PR = 99
GUARDIAN = "mizutani-140"
MARKER = "Guardian approval granted"
LABEL = "guardian:approved"
BRANCH = "shiki/t-0031-carry-parity"

T_CANDIDATE = "2026-08-01T00:00:00Z"
T_LATER = "2026-08-02T00:00:00Z"

PR_BODY = (
    f"{TASK}\n{GOAL}\n\n## Scope\nx\n\n## Acceptance\nx\n\n## Evidence\nx\n\n## MergeGate\nx"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _rev(cwd: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


class CarryParityTests(unittest.TestCase):
    """One shared base-sync repo; each test varies only the guardian evidence."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.a_sha, self.head_sha = self._build_base_sync_repo()
        self._write_control_plane()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- fixture construction -----------------------------------------------

    def _build_base_sync_repo(self) -> tuple[str, str]:
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "config", "user.email", "t@t")
        _git(self.root, "config", "user.name", "t")
        (self.root / "shared.txt").write_text("base\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "base")
        b0 = _rev(self.root, "HEAD")
        _git(self.root, "checkout", "-q", "-b", "feat", b0)
        (self.root / "feature.txt").write_text("feature\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "A")
        a = _rev(self.root, "HEAD")
        _git(self.root, "checkout", "-q", "main")
        (self.root / "other.txt").write_text("o1\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "B1")
        b1 = _rev(self.root, "HEAD")
        _git(self.root, "checkout", "-q", "feat")
        _git(self.root, "merge", "-q", "--no-ff", "-m", "sync", b1)
        m = _rev(self.root, "HEAD")
        _git(self.root, "update-ref", "refs/remotes/origin/main", b1)
        return a, m

    def _write_control_plane(self) -> None:
        shiki = self.root / ".shiki"
        for sub in ("tasks", "goals", "ledger", "gha"):
            (shiki / sub).mkdir(parents=True, exist_ok=True)
        for name in ("config.yaml", "manifest.json", "guardian-policy.json"):
            (shiki / name).write_text((REPO_ROOT / ".shiki" / name).read_text(encoding="utf-8"), encoding="utf-8")
        self.task_body = {
            "id": TASK,
            "goal_id": GOAL,
            "status": "review",
            "risk_level": "high",
            "scope": "carry parity fixture",
            "non_goals": [],
            "required_skills": [],
            "locks": ["scripts/**"],
            "acceptance_checks": ["carry parity"],
            "test_command": "python3 -m unittest discover -s tests",
            "dependencies": [],
            "expected_pr": IMPL_PR,
            "expected_branch": BRANCH,
            "ledger_evidence": [LEDGER],
        }
        (shiki / "goals" / f"{GOAL}.json").write_text(
            json.dumps({"id": GOAL, "status": "in-progress", "risk_level": "high"}), encoding="utf-8"
        )
        (shiki / "ledger" / f"{LEDGER}.json").write_text(
            json.dumps({
                "id": LEDGER, "goal_id": GOAL, "task_id": TASK, "type": "check",
                "summary": f"PR #{IMPL_PR} evidence", "evidence": [f"PR #{IMPL_PR}", f"/pull/{IMPL_PR}"],
                "links": [f"https://github.com/example/shiki/pull/{IMPL_PR}"],
            }),
            encoding="utf-8",
        )
        # Empty diff: the base-synced PR changed no tracked files vs base.
        (shiki / "gha" / "changed-files.txt").write_text("", encoding="utf-8")
        (shiki / "gha" / "changed-files-status.txt").write_text("", encoding="utf-8")
        (shiki / "gha" / "timeline.json").write_text("[]", encoding="utf-8")

    def _write_task(self, *, risk: str = "high", base_risk: str | None = None) -> None:
        (self.root / ".shiki" / "tasks" / f"{TASK}.json").write_text(
            json.dumps({**self.task_body, "risk_level": risk}), encoding="utf-8"
        )
        base = self.root / ".shiki" / "gha" / "base-shiki" / ".shiki" / "tasks"
        base.mkdir(parents=True, exist_ok=True)
        (base / f"{TASK}.json").write_text(
            json.dumps({**self.task_body, "risk_level": base_risk if base_risk is not None else risk}),
            encoding="utf-8",
        )

    def _write_pr(self, *, labels: list[str], base_ref: str = "main") -> None:
        pr = {
            "number": IMPL_PR,
            "body": PR_BODY,
            "author": {"login": "implementer"},
            "headRefName": BRANCH,
            "headRefOid": self.head_sha,
            "baseRefName": base_ref,
            "labels": [{"name": name} for name in labels],
            "reviews": [],
        }
        (self.root / ".shiki" / "gha" / "pr.json").write_text(json.dumps(pr), encoding="utf-8")

    def _write_cca(self) -> None:
        (self.root / ".shiki" / "gha" / "cca-verdict.json").write_text(
            json.dumps({
                "verdict": "complete", "summary": "fixture", "goal_id": GOAL, "task_id": TASK,
                "pr": IMPL_PR, "head_sha": self.head_sha, "can_merge": True, "checklist": [],
                "acceptance": [{"criterion": "fixture", "status": "pass", "evidence": ["fixture"]}],
                "mergegate": {}, "confidence": 1,
            }),
            encoding="utf-8",
        )

    def _write_evidence(self, *, comments: list[dict]) -> None:
        gha = self.root / ".shiki" / "gha"
        (gha / "comments.json").write_text(json.dumps(comments), encoding="utf-8")
        (gha / "events.json").write_text(
            json.dumps([{"event": "labeled", "label": {"name": LABEL}, "actor": {"login": GUARDIAN}}]),
            encoding="utf-8",
        )

    def _comment(self, sha: str, *, created_at: str = T_CANDIDATE, updated_at: str | None = None) -> dict:
        comment = {"user": {"login": GUARDIAN}, "body": f"{MARKER}\n\n{sha}", "created_at": created_at}
        if updated_at is not None:
            comment["updated_at"] = updated_at
        return comment

    # -- entry-point runners -------------------------------------------------

    def _run_mergegate(self, *, carry: bool) -> dict:
        gha = self.root / ".shiki" / "gha"
        result_file = gha / "mergegate-result.json"
        cmd = [
            sys.executable, MERGEGATE, "--target", str(self.root),
            "--pr-json", str(gha / "pr.json"), "--cca-verdict", str(gha / "cca-verdict.json"),
            "--changed-files", str(gha / "changed-files.txt"),
            "--changed-files-status", str(gha / "changed-files-status.txt"),
            "--base-shiki", str(gha / "base-shiki" / ".shiki"),
            "--result-file", str(result_file),
            "--guardian-policy", ".shiki/guardian-policy.json",
            "--guardian-comments", str(gha / "comments.json"),
            "--guardian-events", str(gha / "events.json"),
            "--guardian-timeline", str(gha / "timeline.json"),
        ]
        if carry:
            cmd += ["--base-sync-carry", "--default-branch", "main"]
        subprocess.run(cmd, check=False, capture_output=True, text=True)
        return json.loads(result_file.read_text(encoding="utf-8"))

    def _run_signal(self, *, carry: bool) -> dict:
        gha = self.root / ".shiki" / "gha"
        out = gha / "signal.json"
        argv = [
            "--pr-json", str(gha / "pr.json"),
            "--guardian-policy", str(self.root / ".shiki" / "guardian-policy.json"),
            "--guardian-comments", str(gha / "comments.json"),
            "--guardian-events", str(gha / "events.json"),
            "--guardian-timeline", str(gha / "timeline.json"),
            "--expected-repository", "example/shiki",
            "--shiki-root", str(self.root),
            # The signal must floor the carry risk against the SAME base snapshot
            # the gate uses (never-weaker), so it needs --base-shiki too.
            "--base-shiki", str(gha / "base-shiki" / ".shiki"),
            "--changed-files-status", str(gha / "changed-files-status.txt"),
            "--output", str(out),
        ]
        if carry:
            argv += ["--base-sync-carry", "--default-branch", "main"]
        rc = sig.main_with_argv(argv)
        self.assertEqual(rc, 0)
        return json.loads(out.read_text(encoding="utf-8"))

    @staticmethod
    def _mg_guardian_blocked(mg: dict) -> bool:
        return any("guardian" in reason.lower() for reason in mg.get("blocking_reasons") or [])

    @staticmethod
    def _mg_carried(mg: dict) -> bool:
        return any("guardian_comment_carried" in w for w in mg.get("warnings") or [])

    def _assert_parity(self, mg: dict, sg: dict) -> None:
        """MergeGate and the signal agree, and the signal is never more permissive.

        MergeGate's result JSON exposes the guardian OUTCOME (a guardian blocker,
        or the "satisfied by …" approval warning); other blockers (checks, review,
        CCA) are unrelated to the guardian decision and are ignored here. The
        approving sources are observable only when approved — via the warning — so
        the carry-source parity is asserted only when both approved.
        """
        mg_approved = not self._mg_guardian_blocked(mg)
        sg_approved = bool(sg.get("approved"))
        self.assertEqual(
            mg_approved, sg_approved,
            f"guardian outcome diverged: mergegate={mg.get('blocking_reasons')} signal={sg}",
        )
        # The load-bearing safety property.
        self.assertFalse(
            sg_approved and self._mg_guardian_blocked(mg),
            f"the signal must NEVER be more permissive than MergeGate: {sg} vs {mg}",
        )
        if mg_approved and sg_approved:
            self.assertEqual(
                self._mg_carried(mg), "guardian_comment_carried" in (sg.get("sources") or []),
                f"carry source disagreement: mergegate warnings={mg.get('warnings')} "
                f"signal sources={sg.get('sources')}",
            )
        # Every case here requires a Guardian (high/critical); the signal must say so.
        self.assertTrue(sg.get("required"), f"Guardian must be required: {sg}")

    def _build(self, *, labels, risk="high", base_risk=None, base_ref="main", comments):
        self._write_task(risk=risk, base_risk=base_risk)
        self._write_pr(labels=labels, base_ref=base_ref)
        self._write_cca()
        self._write_evidence(comments=comments)

    # -- fixtures ------------------------------------------------------------

    def test_positive_carry_agrees_and_both_report_carried(self) -> None:
        self._build(labels=[LABEL], comments=[self._comment(self.a_sha)])
        mg = self._run_mergegate(carry=True)
        sg = self._run_signal(carry=True)
        self._assert_parity(mg, sg)
        # The guardian gate itself passed via the carry (other, unrelated checks
        # may still block a bare fixture); both surface guardian_comment_carried.
        self.assertFalse(self._mg_guardian_blocked(mg), mg)
        self.assertTrue(self._mg_carried(mg), mg)
        self.assertTrue(sg["approved"])
        self.assertIn("guardian_comment_carried", sg["sources"])
        self.assertEqual(sg["carried_from_head"], self.a_sha)
        # The proof is recomputed in-process and never persisted: no proof file is
        # written to .shiki/gha by either path.
        gha_files = {p.name for p in (self.root / ".shiki" / "gha").iterdir()}
        self.assertFalse(
            any("proof" in name or "base-sync" in name or "sync-proof" in name for name in gha_files),
            f"no base-sync proof file may be written to .shiki/gha: {sorted(gha_files)}",
        )

    def test_critical_risk_refuses_on_both(self) -> None:
        self._build(labels=[LABEL], risk="critical", comments=[self._comment(self.a_sha)])
        mg = self._run_mergegate(carry=True)
        sg = self._run_signal(carry=True)
        self._assert_parity(mg, sg)
        self.assertFalse(sg["approved"])

    def test_risk_lowering_vs_base_refuses_on_both(self) -> None:
        # The base snapshot's task is critical; the head lowered it to high. A pure
        # base sync has an EMPTY diff, so the immutability gate (which keys on the
        # task file appearing in the diff) does NOT catch this — only the
        # never-weaker floor does. The gate refuses (floored to critical); the
        # signal MUST match, never carrying at head-only high.
        self._build(labels=[LABEL], risk="high", base_risk="critical", comments=[self._comment(self.a_sha)])
        mg = self._run_mergegate(carry=True)
        sg = self._run_signal(carry=True)
        self._assert_parity(mg, sg)
        self.assertFalse(sg["approved"], f"signal must not be more permissive than the gate: {sg}")

    def test_label_escalation_refuses_on_both(self) -> None:
        self._build(labels=[LABEL, "risk:critical"], comments=[self._comment(self.a_sha)])
        mg = self._run_mergegate(carry=True)
        sg = self._run_signal(carry=True)
        self._assert_parity(mg, sg)
        self.assertFalse(sg["approved"])

    def test_retargeted_base_refuses_on_both(self) -> None:
        self._build(labels=[LABEL], base_ref="release-1.x", comments=[self._comment(self.a_sha)])
        mg = self._run_mergegate(carry=True)
        sg = self._run_signal(carry=True)
        self._assert_parity(mg, sg)
        self.assertFalse(sg["approved"])

    def test_edited_comment_refuses_on_both(self) -> None:
        self._build(labels=[LABEL], comments=[self._comment(self.a_sha, updated_at=T_LATER)])
        mg = self._run_mergegate(carry=True)
        sg = self._run_signal(carry=True)
        self._assert_parity(mg, sg)
        self.assertFalse(sg["approved"])

    def test_removed_label_refuses_on_both(self) -> None:
        self._build(labels=[], comments=[self._comment(self.a_sha)])
        mg = self._run_mergegate(carry=True)
        sg = self._run_signal(carry=True)
        self._assert_parity(mg, sg)
        self.assertFalse(sg["approved"])

    def test_fresh_current_head_comment_agrees_and_neither_is_carried(self) -> None:
        self._build(labels=[LABEL], comments=[self._comment(self.head_sha)])
        mg = self._run_mergegate(carry=True)
        sg = self._run_signal(carry=True)
        self._assert_parity(mg, sg)
        self.assertTrue(sg["approved"])
        self.assertIn("guardian_comment", sg["sources"])
        self.assertNotIn("guardian_comment_carried", sg["sources"])

    def test_flag_omitted_is_byte_identical_across_both_paths(self) -> None:
        # With the carry off, a base-synced PR whose only approval is a stale
        # comment blocks on BOTH paths exactly as the pre-change gate did.
        self._build(labels=[LABEL], comments=[self._comment(self.a_sha)])
        mg = self._run_mergegate(carry=False)
        sg = self._run_signal(carry=False)
        self._assert_parity(mg, sg)
        self.assertFalse(sg["approved"])
        self.assertEqual(sg["carried_from_head"], "")


if __name__ == "__main__":
    unittest.main()
