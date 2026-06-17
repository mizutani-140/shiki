"""End-to-end verification scaffolding (PRD 0002 T5, ADR 0011).

T5 delivers a *skeleton*, not a finished contract test, because the path it must
drive (loop-observed TDD evidence + independent pre-PR code-review) is built by
T2 and T3, which are not yet merged. These structural assertions pin down what
the skeleton must honestly contain today so a sibling editing the same surfaces
cannot silently drift it:

  * the skeleton shell test exists, is executable, and is shellcheck-shaped
    (``set -euo pipefail``);
  * its header docstring HONESTLY separates what it asserts now from the TODO
    blocks that depend on not-yet-merged T2/T3 functions (round-1 finding: do
    not over-promise);
  * it names the exact T2/T3 integration points it will wire once they land, so
    the TODO markers are greppable and auditable;
  * the live acceptance-gate plan doc exists and pins the zero-manual-pre-clearing
    bar from ADR 0011.

This is a *meta* test over the skeleton's text, deliberately. It does not run the
loop end-to-end yet (that is the skeleton's job once T2/T3 merge); it guards that
the skeleton stays an honest, greppable placeholder until then.
"""

from __future__ import annotations

import re
import stat
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

ROOT = Path(shiki_test_support.REPO_ROOT)
SKELETON = ROOT / "scripts" / "test_shiki_loop_e2e.sh"
ACCEPTANCE_DOC = ROOT / "docs" / "agents" / "autonomous-loop-e2e-acceptance.md"


class SkeletonExistenceTests(unittest.TestCase):
    def test_skeleton_script_exists_and_is_executable(self) -> None:
        self.assertTrue(SKELETON.is_file(), f"missing skeleton: {SKELETON}")
        mode = SKELETON.stat().st_mode
        self.assertTrue(
            mode & stat.S_IXUSR,
            "skeleton must be executable so the CI test_shiki_*.sh loop runs it",
        )

    def test_skeleton_is_shellcheck_shaped(self) -> None:
        text = SKELETON.read_text(encoding="utf-8")
        self.assertTrue(
            text.startswith("#!/usr/bin/env bash"),
            "skeleton must keep the bash shebang",
        )
        self.assertIn(
            "set -euo pipefail",
            text,
            "skeleton must fail-fast like every other contract test",
        )


class HonestDocstringTests(unittest.TestCase):
    """Round-1 finding: the header must not over-promise. It must state plainly
    what it asserts *now* versus what is a TODO pending T2/T3."""

    def setUp(self) -> None:
        self.text = SKELETON.read_text(encoding="utf-8")

    def test_header_separates_asserts_now_from_todo(self) -> None:
        lowered = self.text.lower()
        # An explicit "asserts now" vs "TODO" split must be present in the header.
        self.assertIn("asserts now", lowered, "header must list what it asserts NOW")
        self.assertIn("todo", lowered, "header must list TODO (deferred) coverage")

    def test_header_admits_it_is_a_skeleton_not_full_coverage(self) -> None:
        lowered = self.text.lower()
        self.assertIn("skeleton", lowered)
        # It must not claim to already drive the full T2/T3 path.
        self.assertNotIn("fully drives dispatch->tdd-evidence->code-review", lowered)

    def test_todo_markers_are_greppable(self) -> None:
        # CI / reviewers grep for these to find the un-wired seams.
        self.assertGreaterEqual(
            len(re.findall(r"TODO\(T2\)", self.text)),
            1,
            "skeleton must mark T2 seams with TODO(T2)",
        )
        self.assertGreaterEqual(
            len(re.findall(r"TODO\(T3\)", self.text)),
            1,
            "skeleton must mark T3 seams with TODO(T3)",
        )


class IntegrationPointTests(unittest.TestCase):
    """The skeleton must name the exact T2/T3 functions it will call once merged,
    so the dependency contract is explicit and a sibling editing those functions
    can see what T5 expects."""

    def setUp(self) -> None:
        self.text = SKELETON.read_text(encoding="utf-8")

    def test_names_t2_tdd_evidence_seam(self) -> None:
        # T2 inserts a loop-owned test-run + tdd-evidence step at the top of the
        # create_pr action and threads a TDD section into github_pr_body.
        self.assertIn("test_command", self.text)
        self.assertIn("## TDD evidence (loop-observed)", self.text)

    def test_names_t3_code_review_seam(self) -> None:
        # T3 adds an independent read-only reviewer dispatch before create_pr and
        # writes the ## Pre-PR code review PR-body section.
        self.assertIn("## Pre-PR code review", self.text)
        self.assertIn("--allowedTools", self.text)

    def test_names_shared_create_pr_branch(self) -> None:
        # The shared surfaces a sibling also edits: execute_action's create_pr
        # branch and github_pr_body.
        self.assertIn("execute_action", self.text)
        self.assertIn("github_pr_body", self.text)

    def test_names_failure_repair_path(self) -> None:
        self.assertIn("dispatch_repair", self.text)


class AcceptanceDocTests(unittest.TestCase):
    def test_doc_exists(self) -> None:
        self.assertTrue(ACCEPTANCE_DOC.is_file(), f"missing doc: {ACCEPTANCE_DOC}")

    def test_doc_pins_zero_manual_pre_clearing(self) -> None:
        text = ACCEPTANCE_DOC.read_text(encoding="utf-8").lower()
        self.assertIn("zero manual pre-clearing", text)
        self.assertIn("auto-merge", text)
        self.assertIn("goal complete", text)
        # The live command the gate runs.
        self.assertIn("shiki loop run", text)

    def test_doc_requires_durable_evidence(self) -> None:
        text = ACCEPTANCE_DOC.read_text(encoding="utf-8").lower()
        self.assertIn("evidence", text)
        self.assertIn("low-risk", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
