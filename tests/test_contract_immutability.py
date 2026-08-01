"""ADR 0015 Contract immutability on the NORMAL task-PR path.

Every governance gain made on 2026-07-29/31 — contract approval before dispatch,
the bookkeeping-closeout exemption, the drop from ~3.5 to ~0.3 operator approvals
per task — is enforced downstream of a task's ``risk_level``. Until this rule
landed, an implementation PR could edit its OWN ``.shiki/tasks/<id>.json`` and the
normal path never objected: ``enforce_untrusted_shiki_mutations`` blocks deleting a
task file and blocks FOREIGN task files, but the PR's own task file was exempt.
Driving the gate directly with base ``risk_level=critical`` and head
``risk_level=low`` produced ZERO blocking reasons and ``_builtin_guardian_risk_required``
returned ``False`` — the Guardian gate simply disappeared.

``mergegate_check.normal_task_contract_immutability_reasons`` now binds a normal
task PR's own task file to the base snapshot: when the task file exists on base,
its governance contract (``_NORMAL_PATH_GOVERNANCE_FIELDS``) is frozen and its
``risk_level`` may never resolve WEAKER than base; when it does not, the PR is a
first registration (head authority); when the own task file changes with no base
snapshot to check it against, it fails closed. Only the mutable set the ADR 0017
closeout classifier already uses (``_CLOSEOUT_MUTABLE_TASK_FIELDS``) — plus
loop-written bookkeeping like ``pre_pr_code_review`` — may move.
``resolved_guardian_risk_never_weaker`` makes the Guardian gate resolve at the
base risk, so the Guardian requirement survives a self-lowered ``risk_level`` even
if the immutability block were bypassed.

These tests DRIVE the real gate functions (never read the source): the
immutability reasons for every block/pass case, and the guardian-risk resolution
for the self-lowered-risk case — asserting the guardian OUTCOME, not merely that
the PR blocks, so a future change that blocks for an unrelated reason cannot mask
a regression.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import mergegate_check
from mergegate_check import (
    ChangedFile,
    _builtin_guardian_risk_required,
    _CLOSEOUT_MUTABLE_TASK_FIELDS,
    _guardian_risk_labels,
    _NORMAL_PATH_GOVERNANCE_FIELDS,
    normal_task_contract_immutability_reasons,
    resolved_guardian_risk_never_weaker,
)

TASK_ID = "T-20260801T003238841526Z-9f3f6f63"
GOAL_ID = "G-20260801T003238840135Z-14193adf"
TASK_FILE = f".shiki/tasks/{TASK_ID}.json"

# A well-formed base task contract. The base snapshot is authority over the
# governance fields; the head PR may move only the mutable/bookkeeping fields.
BASE_TASK = {
    "id": TASK_ID,
    "goal_id": GOAL_ID,
    "title": "Bind a task PR's own contract to the base snapshot on the normal path",
    "scope": "Freeze the normal task-PR path to the base snapshot.",
    "non_goals": ["No change to contract/goal_reconcile/post_merge modes."],
    "required_skills": ["tdd", "code-review"],
    "risk_level": "critical",
    "locks": ["path:scripts/mergegate_check.py"],
    "acceptance_checks": ["a self-lowered risk_level is blocked and still requires a Guardian"],
    "test_command": "python3 -m unittest discover -s tests",
    "dependencies": [],
    "assigned_runtime": "claude-code",
    "github_issue": None,
    "status": "planned",
    "expected_pr": None,
    "expected_branch": "shiki/t-9f3f6f63",
    "ledger_evidence": [],
}


def cf(status: str, path: str) -> ChangedFile:
    return ChangedFile(status, path)


class ContractImmutabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "base" / ".shiki"
        (self.base / "tasks").mkdir(parents=True)
        (self.base / "tasks" / f"{TASK_ID}.json").write_text(
            json.dumps(BASE_TASK, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- driving helpers --------------------------------------------------

    def head(self, **overrides) -> dict:
        task = dict(BASE_TASK)
        task.update(overrides)
        return task

    def reasons(
        self,
        head_task: dict,
        *,
        base: Path | None = "__default__",
        changed: list[ChangedFile] | None = None,
        task_id: str = TASK_ID,
    ) -> list[str]:
        base_shiki = self.base if base == "__default__" else base
        changed_files = changed if changed is not None else [cf("M", f".shiki/tasks/{task_id}.json")]
        return normal_task_contract_immutability_reasons(
            base_shiki=base_shiki,
            task_id=task_id,
            head_task=head_task,
            changed_files_status=changed_files,
        )

    def guardian_required(self, head_task: dict, *, base_task: dict | None = BASE_TASK) -> bool:
        """The single Guardian decision point, driven through the same functions
        ``main`` composes: resolve the risk never-weaker-than-base, then ask the
        built-in gate whether that risk forces a Guardian."""
        resolved = resolved_guardian_risk_never_weaker(base_task, head_task)
        labels = _guardian_risk_labels({"labels": []}, {"risk_level": resolved})
        return _builtin_guardian_risk_required(labels)

    # --- the headline case: self-lowered risk -----------------------------

    def test_risk_lowered_critical_to_low_is_blocked_and_guardian_survives(self) -> None:
        head = self.head(risk_level="low", status="review", expected_pr=42)
        # (a) the immutability gate blocks the change.
        self.assertTrue(self.reasons(head), "a self-lowered risk_level must be blocked")
        # (b) the Guardian requirement SURVIVES: resolved at base (critical) risk.
        self.assertTrue(
            self.guardian_required(head),
            "the Guardian requirement must survive a self-lowered risk_level",
        )
        # Control that pins the mechanism: WITHOUT never-weaker resolution the head's
        # own low risk would drop the gate — proving the resolution is load-bearing.
        head_only_labels = _guardian_risk_labels({"labels": []}, {"risk_level": head["risk_level"]})
        self.assertFalse(
            _builtin_guardian_risk_required(head_only_labels),
            "control: the head's lowered risk alone does NOT force a Guardian",
        )
        # And an untampered critical head still requires a Guardian (no false drop).
        self.assertTrue(self.guardian_required(self.head(status="review")))

    # --- each governance field changed alone is blocked -------------------

    def test_locks_widened_is_blocked(self) -> None:
        head = self.head(locks=["path:scripts/mergegate_check.py", "path:scripts/shiki_loop.py"])
        self.assertTrue(self.reasons(head))

    def test_each_other_governance_field_change_is_blocked(self) -> None:
        # scope, non_goals, required_skills, acceptance_checks, test_command — one
        # case per field. (risk_level and locks have their own tests above.)
        cases = {
            "scope": "a smuggled scope rewrite",
            "non_goals": ["a smuggled non-goal"],
            "required_skills": ["diagnose"],
            "acceptance_checks": ["a smuggled acceptance criterion"],
            "test_command": "echo skip-the-tests",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                head = self.head(**{field: value})
                blocking = self.reasons(head)
                self.assertTrue(blocking, f"changing {field} alone must block")
                self.assertTrue(
                    any(field in reason for reason in blocking),
                    f"a blocking reason must name the field {field!r}: {blocking}",
                )

    def test_every_governance_field_is_covered(self) -> None:
        # The frozen set is exactly the seven governance fields, and each one, when
        # it alone diverges from base, is blocked. Iterating the constant guards
        # against the set silently shrinking.
        self.assertEqual(
            _NORMAL_PATH_GOVERNANCE_FIELDS,
            ("scope", "non_goals", "required_skills", "risk_level", "locks", "acceptance_checks", "test_command"),
        )
        for field in _NORMAL_PATH_GOVERNANCE_FIELDS:
            with self.subTest(field=field):
                head = self.head(**{field: "shiki:governance-tamper-sentinel"})
                self.assertTrue(self.reasons(head), f"tampering {field} must block")

    # --- each mutable field changed alone passes --------------------------

    def test_each_mutable_field_change_alone_passes(self) -> None:
        cases = {
            "status": "review",
            "expected_pr": 99,
            "closeout_pr": 99,
            "expected_branch": "shiki/t-9f3f6f63-closeout",
            "ledger_evidence": ["L-20260801T000000000000Z-abcabcab"],
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                head = self.head(**{field: value})
                self.assertEqual(self.reasons(head), [], f"changing {field} alone must NOT block")

    def test_mutable_set_is_the_single_closeout_constant(self) -> None:
        # The mutable-field set is the SAME constant the ADR 0017 closeout
        # classifier uses, not a second copy, and it is DISJOINT from the frozen
        # governance set so the two cannot drift into overlap.
        self.assertEqual(
            _CLOSEOUT_MUTABLE_TASK_FIELDS,
            frozenset({"status", "expected_pr", "closeout_pr", "expected_branch", "ledger_evidence"}),
        )
        self.assertTrue(
            set(_NORMAL_PATH_GOVERNANCE_FIELDS).isdisjoint(_CLOSEOUT_MUTABLE_TASK_FIELDS),
            "frozen governance fields must be disjoint from the mutable set",
        )

    # --- production-safety: non-governance loop fields may move -----------

    def test_loop_written_bookkeeping_fields_pass(self) -> None:
        # pre_pr_code_review and cca_rerun_count are written onto the task file by
        # the goal loop's create_pr gate and synced to the impl PR branch, so they
        # differ from the base registration on a real PR. They are neither
        # governance nor mutable, and freezing the COMPLEMENT of the mutable set
        # would wrongly block them; this pins that they pass.
        head = self.head(
            status="review",
            expected_pr=7,
            pre_pr_code_review={"verdict": "clean", "findings": [], "ledger_id": "L-x"},
            cca_rerun_count=2,
        )
        self.assertEqual(self.reasons(head), [])

    # --- base-when-present resolution -------------------------------------

    def test_task_absent_from_base_is_first_registration_and_passes(self) -> None:
        # A base snapshot exists but does not contain THIS task: first registration,
        # head is authority. Even a would-be governance divergence passes because
        # there is no base contract to diverge from.
        other = "T-20260801T003238841526Z-0000ffff"
        head = self.head(id=other, risk_level="low")
        head["id"] = other
        self.assertEqual(
            self.reasons(head, changed=[cf("A", f".shiki/tasks/{other}.json")], task_id=other),
            [],
        )

    def test_task_identical_to_base_passes(self) -> None:
        self.assertEqual(self.reasons(self.head()), [])

    def test_changed_task_file_with_no_base_snapshot_is_blocked(self) -> None:
        # Fail closed: the comparison is required (own task file changed) but there
        # is no base to check it against.
        self.assertTrue(self.reasons(self.head(), base=None))
        # Also when the provided base path does not exist on disk.
        missing = Path(self._tmp.name) / "does-not-exist" / ".shiki"
        self.assertTrue(self.reasons(self.head(), base=missing))

    def test_unreadable_base_task_is_blocked(self) -> None:
        # A malformed base task cannot be compared against: fail closed rather than
        # crash or pass an unverifiable contract change.
        (self.base / "tasks" / f"{TASK_ID}.json").write_text("{ this is not valid json", encoding="utf-8")
        self.assertTrue(self.reasons(self.head()))

    # --- scoping: only the own task file, only add/modify ------------------

    def test_pr_not_touching_its_own_task_file_is_unaffected(self) -> None:
        # A governance-divergent head is irrelevant when the diff does not carry the
        # own task file. Even with no base snapshot, no reason is produced.
        head = self.head(risk_level="low")
        self.assertEqual(self.reasons(head, base=None, changed=[cf("M", "src/app.py")]), [])
        self.assertEqual(self.reasons(head, changed=[cf("M", "src/app.py")]), [])

    def test_own_task_file_deletion_is_not_reported_here(self) -> None:
        # A delete is blocked by enforce_untrusted_shiki_mutations, not this gate;
        # a deleted own task file carries no contract to compare, so this stays
        # silent (no double-report).
        self.assertEqual(self.reasons(self.head(), changed=[cf("D", TASK_FILE)]), [])

    # --- special modes are not double-reported ----------------------------

    def test_bookkeeping_closeout_shape_yields_no_reason(self) -> None:
        # A bookkeeping closeout reaches this gate (it is a normal-path PR) but
        # keeps every governance field byte-identical to base and moves only the
        # mutable set, so it produces no reason — behaving exactly as before, with
        # no duplicated blocking reason.
        head = self.head(
            status="done",
            expected_pr=42,
            closeout_pr=43,
            expected_branch="shiki/t-9f3f6f63-closeout",
            ledger_evidence=["L-20260801T000000000000Z-c10c10c1"],
        )
        self.assertEqual(self.reasons(head), [])
        # And its risk is unchanged, so guardian resolution is a no-op (still critical).
        self.assertEqual(resolved_guardian_risk_never_weaker(BASE_TASK, head), "critical")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
