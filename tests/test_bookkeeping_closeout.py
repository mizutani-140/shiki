"""The bookkeeping-closeout Guardian exemption (SADR-0017).

After a high/critical task's implementation PR merges under full Guardian
approval, the autonomous loop opens a *closeout* PR that carries only the task's
terminal state — ``task.status = done``, ``lock.state = released`` and (when the
task completes its goal) ``goal.status = complete`` plus exactly one scorecard
report. It contains zero lines of implementation, so it needed no review to
produce and inherits no new risk; yet MergeGate used to demand a *second*
Guardian approval for it, because ``_guardian_risk_labels`` derived the
requirement from the same task's ``risk_level``. Across 31 PRs merged
2026-07-29/30, 7 of 21 Guardian approval actions were exactly this.

``mergegate_check.is_bookkeeping_closeout`` classifies such a PR, and the single
Guardian decision point (``enforce_guardian_policy`` via ``_guardian_risk_labels``)
evaluates a qualifying PR as if the task were low risk. Every other gate runs
unchanged.

These tests pin the classifier against the positive case and every disqualifier
IN ISOLATION, and assert the Guardian-requirement FALLBACK — that a disqualified
closeout re-inherits the task's real risk and requires Guardian approval — rather
than merely that the PR blocks, so a future change that blocks for an unrelated
reason cannot mask a classifier regression. The exemption is proven only from the
diff, the base snapshot and live PR state (the merged-PR set); the classifier
never reads the PR at all, so a label, marker or PR-body claim can never classify
a non-qualifying PR.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import mergegate_check
from mergegate_check import (
    ChangedFile,
    _builtin_guardian_risk_required,
    _guardian_risk_labels,
    enforce_guardian_policy,
    is_bookkeeping_closeout,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_GUARDIAN_POLICY = REPO_ROOT / ".shiki" / "guardian-policy.json"

GOAL_ID = "G-20260731T004111130646Z-0dbace30"
TASK_ID = "T-20260731T004111132162Z-bfcf2c9e"
SIBLING_ID = "T-20260731T004111132162Z-5ib11a60"
IMPL_PR = 300
CLOSEOUT_PR = 301
HEAD_SHA = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"

# A real closeout is cut from the implementation branch onto ``<branch>-closeout``
# (scripts/shiki_loop.py: ``branch = f"shiki/{task_id.lower()}-closeout"``), and the
# goal loop rewrites the closeout task file's ``expected_branch`` to that new branch.
# So on EVERY real closeout the base task's expected_branch (the implementation
# branch) and the head task's expected_branch (the closeout branch) DIFFER. The
# fixture reflects that real shape; a fixture that shared one expected_branch would
# never exercise the field the loop always changes.
IMPL_BRANCH = "shiki/t-20260731t004111132162z-bfcf2c9e-derive-the-bookkeeping-closeout-exemption-in-mer"
CLOSEOUT_BRANCH = "shiki/t-20260731t004111132162z-bfcf2c9e-closeout"

COMPLETION_LEDGER = "L-20260731T010000000000Z-c0mp0001"
PULL_LEDGER = "L-20260731T010000000001Z-pu110001"
REPORT_ID = "R-20260731T010000000002Z-5c0re001"
REPORT_REL = f".shiki/reports/{REPORT_ID}.json"

# The frozen governance contract shared byte-for-byte by the base and head task.
# expected_branch is deliberately NOT in here: the goal loop rewrites it to the
# closeout branch on every closeout, so it is supplied per-task (base -> impl
# branch, head -> closeout branch) and is EXPECTED to differ.
_GOVERNANCE = {
    "title": "Derive the bookkeeping closeout exemption in MergeGate",
    "scope": "Add is_bookkeeping_closeout and consult it at the Guardian decision point.",
    "non_goals": ["No change to any other MergeGate gate."],
    "dependencies": [],
    "locks": [
        "path:scripts/mergegate_check.py",
        "path:tests/test_bookkeeping_closeout.py",
    ],
    "assigned_runtime": "claude-code",
    "risk_level": "high",
    "required_skills": ["tdd", "code-review"],
    "acceptance_checks": ["a bookkeeping closeout does not require Guardian approval"],
    "test_command": "python3 -m unittest discover -s tests",
    "github_issue": 210,
}


def cf(status: str, path: str, old_path: str | None = None) -> ChangedFile:
    return ChangedFile(status, path, old_path=old_path)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Closeout:
    """A mutable, on-disk bookkeeping-closeout scenario.

    By default it is a VALID completing single-task closeout: the diff carries the
    task file (review -> done), the lock (active -> released), the goal
    (in-progress -> complete), one scorecard report, the goal-level completion
    ledger and the task-scoped /pull ledger. Every test starts from ``build`` (or
    ``build_noncompleting``) and mutates exactly one axis before calling
    ``classify``.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.target = root / "repo"
        self.base = root / "base" / ".shiki"
        self.merged: set[int] = {IMPL_PR}
        self.changed: list[ChangedFile] = []
        self.head_task: dict = {}

    # --- construction -----------------------------------------------------

    def _task(self, *, status: str, expected_pr, ledger_evidence, expected_branch, closeout_pr=None) -> dict:
        task = {"id": TASK_ID, "goal_id": GOAL_ID, **_GOVERNANCE,
                "status": status, "expected_pr": expected_pr,
                "expected_branch": expected_branch,
                "ledger_evidence": list(ledger_evidence)}
        if closeout_pr is not None:
            task["closeout_pr"] = closeout_pr
        return task

    def build(self, *, completes: bool = True) -> "Closeout":
        # Base snapshot (origin/main): the merged impl PR left the task at review,
        # naming the impl PR, on the implementation branch, its lock active and its
        # goal in-progress.
        _write(self.base / "tasks" / f"{TASK_ID}.json",
               self._task(status="review", expected_pr=IMPL_PR, expected_branch=IMPL_BRANCH,
                          ledger_evidence=["L-base00001"]))
        _write(self.base / "locks" / f"{TASK_ID}.json",
               {"task_id": TASK_ID, "goal_id": GOAL_ID, "state": "active", "owner": "shiki-run"})
        _write(self.base / "goals" / f"{GOAL_ID}.json",
               {"id": GOAL_ID, "status": "in-progress", "title": "g", "risk_level": "high"})

        # HEAD (closeout branch cut from origin/main): terminal state, with
        # expected_branch rewritten to the closeout branch (as the goal loop does).
        head_ledgers = [COMPLETION_LEDGER, PULL_LEDGER] if completes else [PULL_LEDGER]
        self.head_task = self._task(status="done", expected_pr=CLOSEOUT_PR, expected_branch=CLOSEOUT_BRANCH,
                                    ledger_evidence=head_ledgers, closeout_pr=CLOSEOUT_PR)
        _write(self.target / ".shiki" / "tasks" / f"{TASK_ID}.json", self.head_task)
        _write(self.target / ".shiki" / "locks" / f"{TASK_ID}.json",
               {"task_id": TASK_ID, "goal_id": GOAL_ID, "state": "released", "owner": "shiki-run"})
        # Goal-level completion ledger (task_id null) cites the scorecard report;
        # the /pull ledger is task-scoped. Both are listed in the head task's
        # ledger_evidence so _derive_task_mirror_locks covers their paths + refs.
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
                   {"id": GOAL_ID, "status": "complete", "title": "g", "risk_level": "high"})
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
            # Multi-task goal, this is NOT the last task: a sibling stays running,
            # so the goal is untouched and no scorecard is added.
            _write(self.target / ".shiki" / "dag" / f"{GOAL_ID}.json",
                   {"goal_id": GOAL_ID, "nodes": [TASK_ID, SIBLING_ID], "edges": []})
            _write(self.target / ".shiki" / "tasks" / f"{SIBLING_ID}.json",
                   {"id": SIBLING_ID, "goal_id": GOAL_ID, "status": "running", "title": "s"})
            _write(self.target / ".shiki" / "goals" / f"{GOAL_ID}.json",
                   {"id": GOAL_ID, "status": "in-progress", "title": "g", "risk_level": "high"})
            self.changed = [
                cf("M", f".shiki/tasks/{TASK_ID}.json"),
                cf("M", f".shiki/locks/{TASK_ID}.json"),
                cf("A", f".shiki/ledger/{PULL_LEDGER}.json"),
            ]
        return self

    def build_noncompleting(self) -> "Closeout":
        return self.build(completes=False)

    # --- mutation helpers -------------------------------------------------

    def rewrite_head_task(self) -> None:
        _write(self.target / ".shiki" / "tasks" / f"{TASK_ID}.json", self.head_task)

    def write_base_task_raw(self, text: str) -> None:
        path = self.base / "tasks" / f"{TASK_ID}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # --- evaluation -------------------------------------------------------

    def classify(self, *, base: Path | None = "__default__") -> bool:
        base_shiki = self.base if base == "__default__" else base
        return is_bookkeeping_closeout(
            target=self.target,
            task=self.head_task,
            task_id=TASK_ID,
            goal_id=GOAL_ID,
            base_shiki=base_shiki,
            changed_files_status=self.changed,
            merged_pr_numbers=self.merged,
        )

    def pr(self, labels: list[dict] | None = None, body: str = "") -> dict:
        return {"number": CLOSEOUT_PR, "headRefOid": HEAD_SHA,
                "labels": labels or [], "reviews": [], "body": body}

    def guardian_required_decision(self, *, bookkeeping: bool, labels: list[dict] | None = None) -> bool:
        """The single Guardian decision point: does the task's risk force the gate?"""
        risk_labels = _guardian_risk_labels(self.pr(labels), self.head_task, bookkeeping_closeout=bookkeeping)
        return _builtin_guardian_risk_required(risk_labels)

    def guardian_gate_blocks(self, *, bookkeeping: bool) -> bool:
        """The real gate over the repo Guardian policy with NO approval evidence:
        an exempted PR sails through; a non-exempted high-risk PR blocks."""
        gha = self.target / ".shiki" / "gha"
        gha.mkdir(parents=True, exist_ok=True)
        for name in ("comments", "events", "timeline"):
            (gha / f"{name}.json").write_text("[]", encoding="utf-8")
        blocking: list[str] = []
        warnings: list[str] = []
        enforce_guardian_policy(
            pr=self.pr(),
            task=self.head_task,
            target=self.target,
            guardian_policy=str(REPO_GUARDIAN_POLICY),
            guardian_comments=str(gha / "comments.json"),
            guardian_events=str(gha / "events.json"),
            guardian_timeline=str(gha / "timeline.json"),
            blocking=blocking,
            warnings=warnings,
            bookkeeping_closeout=bookkeeping,
        )
        return any("Guardian" in reason for reason in blocking)


class BookkeepingCloseoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def new(self) -> Closeout:
        return Closeout(self.root / f"c{self.id().rsplit('.', 1)[-1]}")

    # --- positive ---------------------------------------------------------

    def test_positive_completing_is_classified_and_needs_no_guardian(self) -> None:
        c = self.new().build(completes=True)
        self.assertTrue(c.classify())
        # The single decision point: exempted -> guardian NOT required.
        self.assertFalse(c.guardian_required_decision(bookkeeping=True))
        # The real gate confirms it: an exempted high-risk closeout does not block
        # for missing Guardian approval.
        self.assertFalse(c.guardian_gate_blocks(bookkeeping=True))
        # Control: without the exemption the SAME high-risk PR would require it.
        self.assertTrue(c.guardian_required_decision(bookkeeping=False))
        self.assertTrue(c.guardian_gate_blocks(bookkeeping=False))

    def test_positive_noncompleting_is_classified(self) -> None:
        c = self.new().build_noncompleting()
        self.assertTrue(c.classify())
        self.assertFalse(c.guardian_required_decision(bookkeeping=True))

    def test_positive_expected_branch_differs_is_classified(self) -> None:
        # A REAL closeout is cut onto <branch>-closeout, so base.expected_branch (the
        # implementation branch) and head.expected_branch (the closeout branch)
        # DIFFER — the goal loop performs this rewrite on every closeout by
        # construction. A closeout that moves ONLY the terminal-state fields plus
        # expected_branch must still classify as a bookkeeping closeout. This case is
        # the point of SADR-0017's whitelist fix: it FAILS if expected_branch is
        # removed from _CLOSEOUT_MUTABLE_TASK_FIELDS, because condition 4 would then
        # reject the branch rewrite before any other condition is reached.
        c = self.new().build(completes=True)
        base_task = json.loads((c.base / "tasks" / f"{TASK_ID}.json").read_text())
        self.assertEqual(base_task["expected_branch"], IMPL_BRANCH)
        self.assertEqual(c.head_task["expected_branch"], CLOSEOUT_BRANCH)
        self.assertNotEqual(base_task["expected_branch"], c.head_task["expected_branch"])
        self.assertTrue(c.classify())
        # The single decision point still exempts it (no Guardian required).
        self.assertFalse(c.guardian_required_decision(bookkeeping=True))

    # --- disqualifiers (each in isolation; assert the Guardian fallback) ---

    def _assert_disqualified(self, c: Closeout) -> None:
        # Classifier rejects it, so the code passes bookkeeping_closeout=False and
        # the task's real (high) risk re-forces the Guardian gate. Assert the
        # fallback itself, not merely that some gate blocks.
        self.assertFalse(c.classify())
        self.assertTrue(c.guardian_required_decision(bookkeeping=c.classify()))
        self.assertTrue(c.guardian_gate_blocks(bookkeeping=c.classify()))

    def test_non_shiki_file_disqualifies(self) -> None:
        c = self.new().build()
        c.changed.append(cf("M", "scripts/mergegate_check.py"))  # covered by a lock, still not .shiki
        self._assert_disqualified(c)

    def test_deleted_shiki_path_disqualifies(self) -> None:
        c = self.new().build()
        c.changed.append(cf("D", f".shiki/ledger/{PULL_LEDGER}.json"))
        self._assert_disqualified(c)

    def test_file_outside_effective_locks_disqualifies(self) -> None:
        # A .shiki ledger that is NOT in the task's ledger_evidence is under .shiki
        # (condition 1 passes) but outside the derived mirror locks (condition 3).
        c = self.new().build()
        stray = "L-20260731T010000000009Z-5tray001"
        _write(c.target / ".shiki" / "ledger" / f"{stray}.json",
               {"id": stray, "goal_id": GOAL_ID, "task_id": TASK_ID, "type": "check", "evidence": []})
        c.changed.append(cf("A", f".shiki/ledger/{stray}.json"))
        self._assert_disqualified(c)

    def test_governance_field_edit_disqualifies(self) -> None:
        # Editing acceptance_checks (a governance field) diverges from the base
        # snapshot; risk_level stays high so the fallback is unambiguous.
        c = self.new().build()
        c.head_task["acceptance_checks"] = ["a smuggled acceptance criterion"]
        c.rewrite_head_task()
        self._assert_disqualified(c)

    def test_every_governance_field_change_disqualifies(self) -> None:
        # Guard against the whitelist drifting wider than the one entry SADR-0017
        # adds. Two independent locks:
        #   (a) the mutable set is EXACTLY these five entries — adding a sixth (or
        #       dropping one) fails here directly; and
        #   (b) every field of the frozen governance contract STILL disqualifies a
        #       closeout when the head diverges from the base on it.
        # (b) iterates the hardcoded _GOVERNANCE contract, NOT the complement of the
        # mutable set — so promoting a governance field INTO the mutable set does not
        # silently drop it from this guard: the tampered field would then be skipped
        # by condition 4, the closeout would classify, and the assertFalse would fail.
        self.assertEqual(
            mergegate_check._CLOSEOUT_MUTABLE_TASK_FIELDS,
            frozenset({"status", "expected_pr", "closeout_pr", "ledger_evidence", "expected_branch"}),
        )
        # expected_branch is now mutable, so it must NOT be part of the frozen
        # contract; a couple of real contract fields must be.
        self.assertNotIn("expected_branch", _GOVERNANCE)
        self.assertIn("risk_level", _GOVERNANCE)
        self.assertIn("acceptance_checks", _GOVERNANCE)
        for field in _GOVERNANCE:
            with self.subTest(field=field):
                c = self.new().build()
                self.assertTrue(c.classify())  # qualifies before tampering
                c.head_task[field] = "shiki:governance-tamper-sentinel"
                c.rewrite_head_task()
                self.assertFalse(c.classify())

    def test_risk_level_downgrade_is_not_classified(self) -> None:
        # A closeout that downgrades its own risk_level differs from the base
        # snapshot (condition 4) and is NOT classified. (The fallback for a
        # downgraded head is separately gated; here we pin only the classifier,
        # which reads the immutable base to detect the tamper.)
        c = self.new().build()
        c.head_task["risk_level"] = "low"
        c.rewrite_head_task()
        self.assertFalse(c.classify())

    def test_task_head_status_not_done_disqualifies(self) -> None:
        c = self.new().build()
        c.head_task["status"] = "review"
        c.rewrite_head_task()
        self._assert_disqualified(c)

    def test_base_task_status_not_review_disqualifies(self) -> None:
        c = self.new().build()
        base_task = json.loads((c.base / "tasks" / f"{TASK_ID}.json").read_text())
        base_task["status"] = "planned"
        _write(c.base / "tasks" / f"{TASK_ID}.json", base_task)
        self._assert_disqualified(c)

    def test_goal_status_not_complete_disqualifies(self) -> None:
        c = self.new().build()
        _write(c.target / ".shiki" / "goals" / f"{GOAL_ID}.json",
               {"id": GOAL_ID, "status": "blocked", "title": "g", "risk_level": "high"})
        self._assert_disqualified(c)

    def test_lock_not_released_disqualifies(self) -> None:
        c = self.new().build()
        _write(c.target / ".shiki" / "locks" / f"{TASK_ID}.json",
               {"task_id": TASK_ID, "goal_id": GOAL_ID, "state": "active", "owner": "shiki-run"})
        self._assert_disqualified(c)

    def test_base_lock_not_active_disqualifies(self) -> None:
        c = self.new().build()
        _write(c.base / "locks" / f"{TASK_ID}.json",
               {"task_id": TASK_ID, "goal_id": GOAL_ID, "state": "released", "owner": "shiki-run"})
        self._assert_disqualified(c)

    def test_second_scorecard_report_disqualifies(self) -> None:
        c = self.new().build()
        second = "R-20260731T010000000003Z-5c0re002"
        _write(c.target / ".shiki" / "reports" / f"{second}.json", {"id": second, "goal_id": GOAL_ID})
        # Cover the extra report by the completion ledger so condition 3 passes and
        # ONLY the "second report" rule (condition 5) can disqualify it.
        _write(c.target / ".shiki" / "ledger" / f"{COMPLETION_LEDGER}.json",
               {"id": COMPLETION_LEDGER, "goal_id": GOAL_ID, "task_id": None, "type": "completion",
                "summary": "goal complete", "evidence": [REPORT_REL, f".shiki/reports/{second}.json"]})
        c.changed.append(cf("A", f".shiki/reports/{second}.json"))
        self._assert_disqualified(c)

    def test_goal_transition_without_completion_disqualifies(self) -> None:
        # A NON-completing closeout (a sibling still runs) that nonetheless carries
        # goal -> complete + a report is not a legitimate completion.
        c = self.new().build_noncompleting()
        _write(c.target / ".shiki" / "goals" / f"{GOAL_ID}.json",
               {"id": GOAL_ID, "status": "complete", "title": "g", "risk_level": "high"})
        c.changed.append(cf("M", f".shiki/goals/{GOAL_ID}.json"))
        self._assert_disqualified(c)

    def test_report_without_completion_disqualifies(self) -> None:
        c = self.new().build_noncompleting()
        _write(c.target / REPORT_REL, {"id": REPORT_ID, "goal_id": GOAL_ID})
        # Cover it via a ledger in ledger_evidence so only condition 5 can reject.
        _write(c.target / ".shiki" / "ledger" / f"{PULL_LEDGER}.json",
               {"id": PULL_LEDGER, "goal_id": GOAL_ID, "task_id": TASK_ID, "type": "lock",
                "summary": "closeout /pull", "evidence": [REPORT_REL]})
        c.changed.append(cf("A", REPORT_REL))
        self._assert_disqualified(c)

    def test_impl_pr_not_merged_disqualifies(self) -> None:
        c = self.new().build()
        c.merged = set()  # the base expected_pr (impl PR) is not proven merged
        self._assert_disqualified(c)

    def test_impl_pr_unresolvable_disqualifies(self) -> None:
        c = self.new().build()
        base_task = json.loads((c.base / "tasks" / f"{TASK_ID}.json").read_text())
        base_task["expected_pr"] = None
        _write(c.base / "tasks" / f"{TASK_ID}.json", base_task)
        self._assert_disqualified(c)

    def test_missing_base_snapshot_disqualifies(self) -> None:
        c = self.new().build()
        self.assertFalse(c.classify(base=None))

    def test_unreadable_base_task_disqualifies(self) -> None:
        c = self.new().build()
        c.write_base_task_raw("{ this is not valid json")
        self.assertFalse(c.classify())

    def test_missing_dag_disqualifies(self) -> None:
        c = self.new().build()
        (c.target / ".shiki" / "dag" / f"{GOAL_ID}.json").unlink()
        self._assert_disqualified(c)

    def test_contract_shaped_registration_is_not_classified(self) -> None:
        # A contract/goal_reconcile PR registers a NEW planned task: no base task
        # snapshot exists for it and the head status is planned. Never a closeout.
        c = self.new().build()
        c.head_task["status"] = "planned"
        c.rewrite_head_task()
        (c.base / "tasks" / f"{TASK_ID}.json").unlink()  # not yet on the base branch
        self.assertFalse(c.classify())

    # --- label / marker / PR-body cannot classify -------------------------

    def test_classifier_never_reads_the_pr(self) -> None:
        # Structural guarantee: the classifier takes no `pr` parameter, so a label,
        # marker or PR-body claim CANNOT influence classification.
        params = set(inspect.signature(is_bookkeeping_closeout).parameters)
        self.assertNotIn("pr", params)

    def test_label_or_marker_does_not_classify_non_qualifying_pr(self) -> None:
        # A non-qualifying closeout (carries a non-.shiki file) stays disqualified,
        # and the real Guardian gate still fires, even though a maintainer-looking
        # label and a body marker are present on the PR.
        c = self.new().build()
        c.changed.append(cf("M", "scripts/mergegate_check.py"))
        self.assertFalse(c.classify())
        risk_labels = _guardian_risk_labels(
            c.pr(labels=[{"name": "bookkeeping"}], body="<!-- shiki:bookkeeping_closeout -->"),
            c.head_task,
            bookkeeping_closeout=c.classify(),
        )
        self.assertTrue(_builtin_guardian_risk_required(risk_labels))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
