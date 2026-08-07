"""The bookkeeping-closeout Guardian exemption on the CCA *signal* path (SADR-0017).

``scripts/guardian_approval_signal.py`` writes ``.shiki/gha/guardian-approval.json``,
the deterministic Guardian-approval result the CCA judge reads for CCA-08. Before
this change it resolved risk only from the head task's ``risk_level`` and had no
base snapshot, no changed-files status and no merged-PR proof, so it could not
classify a closeout at all: a closeout PR for a high/critical task passed the
MergeGate *metadata* check (which learned the SADR-0017 exemption) but the CCA
signal still returned ``needs_guardian``, leaving the exemption real but
unreachable (PR #209).

The fix imports the SAME ``mergegate_check.is_bookkeeping_closeout`` classifier
and feeds it the SAME inputs MergeGate feeds it. These tests prove:

* a proven closeout for a *critical* task yields ``required=false``;
* each SADR-0017 disqualifier IN ISOLATION re-requires Guardian approval;
* each missing/unbuildable input fails closed to ``required=true``;
* the emitted JSON records whether the exemption was applied; and
* a PARITY property — for a representative matrix, the signal's exemption decision
  equals MergeGate's for the same inputs, and the signal is NEVER more permissive
  than MergeGate (it never drops Guardian approval that MergeGate would demand).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import guardian_approval_signal as sig
from mergegate_check import (
    ChangedFile,
    _builtin_guardian_risk_required,
    _guardian_risk_labels,
    is_bookkeeping_closeout,
)
from shiki_guardian import load_guardian_policy_file, risk_requires_guardian

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_GUARDIAN_POLICY = REPO_ROOT / ".shiki" / "guardian-policy.json"

GOAL_ID = "G-20260731T031357585622Z-8d094bd1"
TASK_ID = "T-20260731T031357587095Z-2cb11839"
SIBLING_ID = "T-20260731T031357587095Z-51b11a60"
IMPL_PR = 400
CLOSEOUT_PR = 401
HEAD_SHA = "c" * 40

COMPLETION_LEDGER = "L-20260731T040000000000Z-c0mp0002"
PULL_LEDGER = "L-20260731T040000000001Z-pu110002"
REPORT_ID = "R-20260731T040000000002Z-5c0re003"
REPORT_REL = f".shiki/reports/{REPORT_ID}.json"

# The frozen governance contract shared byte-for-byte by the base and head task.
# risk_level is CRITICAL so the acceptance check "a proven closeout for a critical
# task yields required=false" is exercised directly, and every disqualifier's
# fallback re-forces the gate from a critical risk.
_GOVERNANCE = {
    "title": "Give the CCA Guardian signal the bookkeeping closeout exemption",
    "scope": "Import is_bookkeeping_closeout into the signal and plumb its inputs.",
    "non_goals": ["No change to any other gate."],
    "dependencies": [],
    "locks": [
        "path:scripts/guardian_approval_signal.py",
        "path:.github/workflows/shiki-cca-completion.yml",
        "path:tests/test_guardian_signal_closeout.py",
    ],
    "assigned_runtime": "claude-code",
    "risk_level": "critical",
    "required_skills": ["tdd", "code-review"],
    "acceptance_checks": ["a proven closeout does not require Guardian approval"],
    "test_command": "python3 -m unittest discover -s tests",
    "expected_branch": "shiki/t-20260731-signal-closeout",
    "github_issue": 211,
}

_SENTINEL = object()


def cf(status: str, path: str, old_path: str | None = None) -> ChangedFile:
    return ChangedFile(status, path, old_path=old_path)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Scenario:
    """A mutable, on-disk bookkeeping-closeout scenario, evaluated end-to-end
    through ``guardian_approval_signal.main`` AND compared against MergeGate.

    ``build`` lays out a VALID completing single-task closeout: the head checkout
    (``self.target`` = the signal's ``--shiki-root``) carries the terminal state,
    ``self.base`` is the base branch's ``.shiki`` snapshot, ``self.merged`` proves
    the implementation PR merged, and ``self.changed`` is the diff. Tests mutate
    exactly one axis, then call ``run_signal`` / ``classify``.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.target = root / "repo"
        self.base = root / "base" / ".shiki"
        self.merged: set[int] = {IMPL_PR}
        self.changed: list[ChangedFile] = []
        self.head_task: dict = {}
        self.labels: list[str] = []

    # --- construction -----------------------------------------------------

    def _task(self, *, status: str, expected_pr, ledger_evidence, closeout_pr=None) -> dict:
        task = {"id": TASK_ID, "goal_id": GOAL_ID, **_GOVERNANCE,
                "status": status, "expected_pr": expected_pr,
                "ledger_evidence": list(ledger_evidence)}
        if closeout_pr is not None:
            task["closeout_pr"] = closeout_pr
        return task

    def build(self, *, completes: bool = True) -> "Scenario":
        # Base snapshot (origin/main): the merged impl PR left the task at review,
        # naming the impl PR, its lock active and its goal in-progress.
        _write(self.base / "tasks" / f"{TASK_ID}.json",
               self._task(status="review", expected_pr=IMPL_PR, ledger_evidence=["L-base00002"]))
        _write(self.base / "locks" / f"{TASK_ID}.json",
               {"task_id": TASK_ID, "goal_id": GOAL_ID, "state": "active", "owner": "shiki-run"})
        _write(self.base / "goals" / f"{GOAL_ID}.json",
               {"id": GOAL_ID, "status": "in-progress", "title": "g", "risk_level": "critical"})

        # HEAD (closeout branch cut from origin/main): terminal state.
        head_ledgers = [COMPLETION_LEDGER, PULL_LEDGER] if completes else [PULL_LEDGER]
        self.head_task = self._task(status="done", expected_pr=CLOSEOUT_PR,
                                    ledger_evidence=head_ledgers, closeout_pr=CLOSEOUT_PR)
        self.rewrite_head_task()
        _write(self.target / ".shiki" / "locks" / f"{TASK_ID}.json",
               {"task_id": TASK_ID, "goal_id": GOAL_ID, "state": "released", "owner": "shiki-run"})
        _write(self.target / ".shiki" / "ledger" / f"{COMPLETION_LEDGER}.json",
               {"id": COMPLETION_LEDGER, "goal_id": GOAL_ID, "task_id": None,
                "type": "completion", "summary": "goal complete", "evidence": [REPORT_REL]})
        _write(self.target / ".shiki" / "ledger" / f"{PULL_LEDGER}.json",
               {"id": PULL_LEDGER, "goal_id": GOAL_ID, "task_id": TASK_ID, "type": "lock",
                "summary": "closeout /pull", "evidence": [f".shiki/tasks/{TASK_ID}.json"]})

        if completes:
            _write(self.target / ".shiki" / "dag" / f"{GOAL_ID}.json",
                   {"goal_id": GOAL_ID, "nodes": [TASK_ID], "edges": []})
            _write(self.target / ".shiki" / "goals" / f"{GOAL_ID}.json",
                   {"id": GOAL_ID, "status": "complete", "title": "g", "risk_level": "critical"})
            _write(self.target / REPORT_REL, {"id": REPORT_ID, "goal_id": GOAL_ID, "status": "complete"})
            self.changed = [
                cf("M", f".shiki/tasks/{TASK_ID}.json"),
                cf("M", f".shiki/locks/{TASK_ID}.json"),
                cf("M", f".shiki/goals/{GOAL_ID}.json"),
                cf("A", f".shiki/worktrees/{TASK_ID}.json"),
                cf("A", REPORT_REL),
                cf("A", f".shiki/ledger/{COMPLETION_LEDGER}.json"),
                cf("A", f".shiki/ledger/{PULL_LEDGER}.json"),
            ]
        else:
            _write(self.target / ".shiki" / "dag" / f"{GOAL_ID}.json",
                   {"goal_id": GOAL_ID, "nodes": [TASK_ID, SIBLING_ID], "edges": []})
            _write(self.target / ".shiki" / "tasks" / f"{SIBLING_ID}.json",
                   {"id": SIBLING_ID, "goal_id": GOAL_ID, "status": "running", "title": "s"})
            _write(self.target / ".shiki" / "goals" / f"{GOAL_ID}.json",
                   {"id": GOAL_ID, "status": "in-progress", "title": "g", "risk_level": "critical"})
            self.changed = [
                cf("M", f".shiki/tasks/{TASK_ID}.json"),
                cf("M", f".shiki/locks/{TASK_ID}.json"),
                cf("A", f".shiki/ledger/{PULL_LEDGER}.json"),
            ]
        return self

    def build_noncompleting(self) -> "Scenario":
        return self.build(completes=False)

    def rewrite_head_task(self) -> None:
        _write(self.target / ".shiki" / "tasks" / f"{TASK_ID}.json", self.head_task)

    # --- evaluation -------------------------------------------------------

    def _write_status_file(self) -> Path:
        lines: list[str] = []
        for entry in self.changed:
            if entry.status == "R" and entry.old_path:
                lines.append(f"R100\t{entry.old_path}\t{entry.path}")
            else:
                lines.append(f"{entry.status}\t{entry.path}")
        path = self.target / "changed-files-status.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        return path

    def _write_pr(self, body) -> Path:
        pr = {
            "number": CLOSEOUT_PR,
            "headRefOid": HEAD_SHA,
            "author": {"login": "mizutani-140"},
            "labels": [{"name": name} for name in self.labels],
            "reviews": [],
            "body": (f"Closeout {TASK_ID} for goal {GOAL_ID}." if body is _SENTINEL else body),
        }
        path = self.target / "pr.json"
        path.write_text(json.dumps(pr), encoding="utf-8")
        return path

    def run_signal(
        self,
        *,
        base_shiki=_SENTINEL,
        changed_files_status=_SENTINEL,
        merged_prs=_SENTINEL,
        body=_SENTINEL,
        guardian_policy=_SENTINEL,
    ) -> dict:
        """Run the real signal CLI and return the emitted JSON. Each input can be
        overridden (e.g. to "" for the missing-input fail-closed cases)."""
        status_path = self._write_status_file()
        pr_path = self._write_pr(body)
        base_arg = str(self.base) if base_shiki is _SENTINEL else base_shiki
        status_arg = str(status_path) if changed_files_status is _SENTINEL else changed_files_status
        merged_arg = (
            ",".join(str(n) for n in sorted(self.merged)) if merged_prs is _SENTINEL else merged_prs
        )
        policy_arg = str(REPO_GUARDIAN_POLICY) if guardian_policy is _SENTINEL else guardian_policy
        out = self.target / "guardian-approval.json"
        rc = sig.main_with_argv([
            "--pr-json", str(pr_path),
            "--guardian-policy", policy_arg,
            "--expected-repository", "mizutani-140/shiki",
            "--shiki-root", str(self.target),
            "--base-shiki", base_arg,
            "--changed-files-status", status_arg,
            "--merged-prs", merged_arg,
            "--output", str(out),
        ])
        assert rc == 0, f"signal exited {rc}"
        return json.loads(out.read_text(encoding="utf-8"))

    def classify(self, *, base: Path | None = _SENTINEL, merged=_SENTINEL) -> bool:
        """MergeGate's exemption decision: the SAME classifier over the SAME inputs."""
        base_shiki = self.base if base is _SENTINEL else base
        merged_prs = self.merged if merged is _SENTINEL else merged
        return is_bookkeeping_closeout(
            target=self.target,
            task=self.head_task,
            task_id=TASK_ID,
            goal_id=GOAL_ID,
            base_shiki=base_shiki,
            changed_files_status=self.changed,
            merged_pr_numbers=merged_prs,
        )

    def mergegate_required(self, *, exempt: bool) -> bool:
        """MergeGate's Guardian requirement for this PR, mirroring
        ``enforce_guardian_policy`` (builtin high/critical OR policy-driven)."""
        pr = {"number": CLOSEOUT_PR, "headRefOid": HEAD_SHA,
              "labels": [{"name": name} for name in self.labels], "reviews": [], "body": ""}
        risk_labels = _guardian_risk_labels(pr, self.head_task, bookkeeping_closeout=exempt)
        if _builtin_guardian_risk_required(risk_labels):
            return True
        policy = load_guardian_policy_file(REPO_GUARDIAN_POLICY)
        return risk_requires_guardian(risk_labels, policy)


class GuardianSignalCloseoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def new(self) -> Scenario:
        return Scenario(self.root / f"c{self.id().rsplit('.', 1)[-1]}")

    # --- positive: a proven closeout drops the requirement ----------------

    def test_proven_completing_critical_closeout_not_required(self) -> None:
        signal = self.new().build(completes=True).run_signal()
        self.assertFalse(signal["required"])
        self.assertTrue(signal["approved"])
        self.assertTrue(signal["bookkeeping_closeout_exemption"])

    def test_proven_noncompleting_closeout_not_required(self) -> None:
        signal = self.new().build_noncompleting().run_signal()
        self.assertFalse(signal["required"])
        self.assertTrue(signal["bookkeeping_closeout_exemption"])

    # --- disqualifiers in isolation re-require Guardian approval -----------

    def _assert_disqualified(self, s: Scenario) -> None:
        signal = s.run_signal()
        self.assertTrue(signal["required"], "disqualified closeout must require Guardian approval")
        self.assertFalse(signal["bookkeeping_closeout_exemption"])
        # The classifier agrees it is not a closeout (no exemption).
        self.assertFalse(s.classify())

    def test_non_shiki_path_requires_guardian(self) -> None:
        s = self.new().build()
        s.changed.append(cf("M", "scripts/guardian_approval_signal.py"))
        self._assert_disqualified(s)

    def test_governance_field_edit_requires_guardian(self) -> None:
        s = self.new().build()
        s.head_task["acceptance_checks"] = ["a smuggled acceptance criterion"]
        s.rewrite_head_task()
        self._assert_disqualified(s)

    def test_deletion_requires_guardian(self) -> None:
        s = self.new().build()
        s.changed.append(cf("D", f".shiki/ledger/{PULL_LEDGER}.json"))
        self._assert_disqualified(s)

    def test_non_terminal_status_requires_guardian(self) -> None:
        s = self.new().build()
        s.head_task["status"] = "review"  # not review -> done
        s.rewrite_head_task()
        self._assert_disqualified(s)

    def test_second_scorecard_requires_guardian(self) -> None:
        s = self.new().build()
        second = "R-20260731T040000000003Z-5c0re004"
        _write(s.target / ".shiki" / "reports" / f"{second}.json", {"id": second, "goal_id": GOAL_ID})
        # Cover the extra report via the completion ledger so ONLY the second-report
        # rule (condition 5), not files-outside-locks (condition 3), disqualifies.
        _write(s.target / ".shiki" / "ledger" / f"{COMPLETION_LEDGER}.json",
               {"id": COMPLETION_LEDGER, "goal_id": GOAL_ID, "task_id": None, "type": "completion",
                "summary": "goal complete", "evidence": [REPORT_REL, f".shiki/reports/{second}.json"]})
        s.changed.append(cf("A", f".shiki/reports/{second}.json"))
        self._assert_disqualified(s)

    def test_unmerged_implementation_pr_requires_guardian(self) -> None:
        s = self.new().build()
        s.merged = set()  # the base expected_pr (impl PR) is not proven merged
        self._assert_disqualified(s)

    # --- missing / unbuildable inputs fail closed -------------------------

    def test_missing_base_snapshot_requires_guardian(self) -> None:
        s = self.new().build()
        signal = s.run_signal(base_shiki="")
        self.assertTrue(signal["required"])
        self.assertFalse(signal["bookkeeping_closeout_exemption"])

    def test_nonexistent_base_snapshot_requires_guardian(self) -> None:
        s = self.new().build()
        signal = s.run_signal(base_shiki=str(s.root / "no-such-base" / ".shiki"))
        self.assertTrue(signal["required"])
        self.assertFalse(signal["bookkeeping_closeout_exemption"])

    def test_missing_changed_files_status_requires_guardian(self) -> None:
        s = self.new().build()
        signal = s.run_signal(changed_files_status="")
        self.assertTrue(signal["required"])
        self.assertFalse(signal["bookkeeping_closeout_exemption"])

    def test_nonexistent_changed_files_status_requires_guardian(self) -> None:
        s = self.new().build()
        signal = s.run_signal(changed_files_status=str(s.target / "absent-status.txt"))
        self.assertTrue(signal["required"])
        self.assertFalse(signal["bookkeeping_closeout_exemption"])

    def test_unresolvable_implementation_pr_requires_guardian(self) -> None:
        # The base snapshot names no implementation PR (expected_pr null), so the
        # merge proof cannot be established -> no exemption.
        s = self.new().build()
        base_task = json.loads((s.base / "tasks" / f"{TASK_ID}.json").read_text(encoding="utf-8"))
        base_task["expected_pr"] = None
        _write(s.base / "tasks" / f"{TASK_ID}.json", base_task)
        signal = s.run_signal()
        self.assertTrue(signal["required"])
        self.assertFalse(signal["bookkeeping_closeout_exemption"])

    def test_unresolvable_task_id_requires_guardian(self) -> None:
        # A PR body with no Shiki task id: the exemption cannot resolve a task, and
        # (as before) risk is undeterminable -> fail closed.
        s = self.new().build()
        signal = s.run_signal(body="Closeout PR with no task id in the body.")
        self.assertTrue(signal["required"])
        self.assertFalse(signal["bookkeeping_closeout_exemption"])

    # --- auditability -----------------------------------------------------

    def test_signal_records_exemption_applied_flag(self) -> None:
        # Positive records True; a disqualified closeout records False. The flag is
        # ALWAYS present so the decision is auditable in the uploaded evidence.
        proven = self.new().build().run_signal()
        self.assertIn("bookkeeping_closeout_exemption", proven)
        self.assertTrue(proven["bookkeeping_closeout_exemption"])

        s = self.new().build()
        s.changed.append(cf("M", "scripts/guardian_approval_signal.py"))
        disq = s.run_signal()
        self.assertIn("bookkeeping_closeout_exemption", disq)
        self.assertFalse(disq["bookkeeping_closeout_exemption"])

    def test_maintainer_label_still_forces_gate_on_exempt_pr(self) -> None:
        # The exemption downgrades only the TASK's risk; a maintainer-applied
        # risk:critical PR label still forces Guardian approval — exactly as in
        # MergeGate — so the exemption is not a bypass.
        s = self.new().build()
        s.labels = ["risk:critical"]
        signal = s.run_signal()
        self.assertTrue(signal["required"])
        # The exemption WAS applied to the task risk; the label is what re-forces it.
        self.assertTrue(signal["bookkeeping_closeout_exemption"])
        self.assertTrue(s.classify())

    # --- parity: signal == MergeGate, and never more permissive ----------

    def test_parity_signal_matches_mergegate_and_is_never_more_permissive(self) -> None:
        # A representative matrix of (label-set, mutation) cases. For each, the
        # signal's exemption decision must EQUAL MergeGate's classifier, the
        # resulting Guardian requirement must match, and the signal must NEVER be
        # more permissive (never required=false where MergeGate requires it).
        def positive(labels):
            s = self.new().build()
            s.labels = list(labels)
            return s

        def positive_noncompleting(labels):
            s = self.new().build_noncompleting()
            s.labels = list(labels)
            return s

        def non_shiki(labels):
            s = positive(labels)
            s.changed.append(cf("M", "scripts/guardian_approval_signal.py"))
            return s

        def governance_edit(labels):
            s = positive(labels)
            s.head_task["locks"] = ["path:scripts/other.py"]
            s.rewrite_head_task()
            return s

        def deletion(labels):
            s = positive(labels)
            s.changed.append(cf("D", f".shiki/ledger/{PULL_LEDGER}.json"))
            return s

        def non_terminal(labels):
            s = positive(labels)
            s.head_task["status"] = "review"
            s.rewrite_head_task()
            return s

        def unmerged(labels):
            s = positive(labels)
            s.merged = set()
            return s

        builders = [
            positive, positive_noncompleting, non_shiki,
            governance_edit, deletion, non_terminal, unmerged,
        ]
        label_sets = [[], ["risk:critical"], ["risk:low"]]

        checked = 0
        for build in builders:
            for labels in label_sets:
                s = build(labels)
                signal = s.run_signal()
                exempt = s.classify()
                mg_required = s.mergegate_required(exempt=exempt)

                self.assertEqual(
                    signal["bookkeeping_closeout_exemption"], exempt,
                    f"{build.__name__} labels={labels}: exemption decision diverged",
                )
                self.assertEqual(
                    signal["required"], mg_required,
                    f"{build.__name__} labels={labels}: required diverged from MergeGate",
                )
                # Never more permissive: the signal must not drop a Guardian
                # requirement that MergeGate would enforce.
                self.assertFalse(
                    signal["required"] is False and mg_required is True,
                    f"{build.__name__} labels={labels}: signal MORE permissive than MergeGate",
                )
                checked += 1
        self.assertEqual(checked, len(builders) * len(label_sets))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
