"""End-to-end loop self-drive CONTRACT meta-test (PRD 0002 T5, SADR-0011).

T1-T4 are merged, so `scripts/test_shiki_loop_e2e.sh` now drives the FULL
integrated autonomous path and asserts the loop-owned evidence in the implemented
order. These structural assertions guard that the e2e contract test stays honest
and complete — no leftover "skeleton"/TODO/"not merged" placeholder text, and it
must keep describing + asserting the IMPLEMENTED contract:

  * code-review gate runs BEFORE the TDD gate (review-first);
  * a non-clean PRE-PR review fails closed to stop_blocked (no PR to anchor a
    repair), DISTINCT from the POST-PR required-check bounded-repair path;
  * the e2e test asserts both loop-owned ledgers exist AND their order.
"""

from __future__ import annotations

import stat
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

ROOT = Path(shiki_test_support.REPO_ROOT)
E2E = ROOT / "scripts" / "test_shiki_loop_e2e.sh"
ACCEPTANCE_DOC = ROOT / "docs" / "agents" / "autonomous-loop-e2e-acceptance.md"


class E2EScriptShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = E2E.read_text(encoding="utf-8")

    def test_exists_executable_shellcheck_shaped(self) -> None:
        self.assertTrue(E2E.is_file(), f"missing e2e test: {E2E}")
        self.assertTrue(E2E.stat().st_mode & stat.S_IXUSR,
                        "e2e test must be executable so the CI test_shiki_*.sh loop runs it")
        self.assertTrue(self.text.startswith("#!/usr/bin/env bash"))
        self.assertIn("set -euo pipefail", self.text)

    def test_no_stale_skeleton_or_todo_placeholder_text(self) -> None:
        # T1-T4 are merged: the file must not still describe itself as a skeleton
        # with un-wired seams. (Guards against the obsolete placeholder contract.)
        lowered = self.text.lower()
        for stale in ("skeleton", "todo(t2)", "todo(t3)", "not merged yet",
                      "not yet merged", "once t2/t3", "once t3 lands"):
            self.assertNotIn(stale, lowered, f"stale placeholder text remains: {stale!r}")


class ImplementedContractTests(unittest.TestCase):
    """The e2e test must describe AND assert the IMPLEMENTED loop contract."""

    def setUp(self) -> None:
        self.text = E2E.read_text(encoding="utf-8")

    def test_describes_code_review_before_tdd_order(self) -> None:
        # The header must state the implemented review-first order; the position
        # of the code-review gate must precede the TDD gate in the description.
        i_review = self.text.find("code-review gate (T3)")
        i_tdd = self.text.find("TDD gate (T2)")
        self.assertNotEqual(i_review, -1, "header must name the (a) code-review gate")
        self.assertNotEqual(i_tdd, -1, "header must name the (b) TDD gate")
        self.assertLess(i_review, i_tdd, "header must show code-review BEFORE the TDD gate")

    def test_asserts_ledger_order_not_just_presence(self) -> None:
        # The executable proof must assert ORDER (code-review index < TDD index),
        # so a future reorder of execute_action's create_pr branch breaks it.
        self.assertIn("first_index", self.text)
        self.assertIn("must precede the TDD gate", self.text)

    def test_distinguishes_pre_pr_stop_blocked_from_post_pr_repair(self) -> None:
        lowered = self.text.lower()
        # Pre-PR review fails closed to stop_blocked (no PR to anchor a repair)...
        self.assertIn("stop_blocked", lowered)
        # ...and the bounded repair path is explicitly the POST-PR one.
        self.assertIn("post-pr", lowered)
        self.assertIn("repair", lowered)

    def test_asserts_both_loop_owned_ledgers(self) -> None:
        # Loop-owned T2 tdd-gate ledger (matched on its summary, not just EXEC)
        # and the independent T3 code-review ledger.
        self.assertIn("loop-observed tdd gate", self.text.lower())
        self.assertIn("code-review", self.text.lower())
        self.assertIn("reviewer", self.text.lower())


class AcceptanceDocTests(unittest.TestCase):
    def test_doc_pins_zero_manual_pre_clearing(self) -> None:
        self.assertTrue(ACCEPTANCE_DOC.is_file(), f"missing doc: {ACCEPTANCE_DOC}")
        text = ACCEPTANCE_DOC.read_text(encoding="utf-8").lower()
        self.assertIn("zero manual pre-clearing", text)
        self.assertIn("auto-merge", text)
        self.assertIn("goal complete", text)
        self.assertIn("shiki loop run", text)
        self.assertIn("low-risk", text)

    def test_doc_states_pre_pr_review_stop_blocked(self) -> None:
        text = ACCEPTANCE_DOC.read_text(encoding="utf-8").lower()
        # The doc must match the implemented T3 semantics, not the old claim.
        self.assertIn("stop_blocked", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
