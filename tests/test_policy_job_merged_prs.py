"""The MergeGate policy job must resolve the implementation-PR merge proof for
EVERY judged PR, not only ``post_merge_reconcile`` ones (ADR 0017).

``.github/workflows/shiki-cca-completion.yml`` has two consumers of the merged-PR
proof: the CCA *signal* (``guardian_approval_signal.py`` in "Prepare CCA context")
and the authoritative gate (``mergegate_check.py`` in "Enforce MergeGate"). ADR
0017's bookkeeping-closeout exemption (condition 6) needs the merged implementation
PR proven, and a closeout carries NO ``post_merge_reconcile`` marker. The signal
already resolves ``MERGED_PRS`` unconditionally, but "Enforce MergeGate" used to
resolve it only inside ``grep -q '<!-- shiki:post_merge_reconcile -->'``. So a
high/critical closeout could show the CCA signal exempt (``required: false``) while
the policy check still blocked with "Guardian label 'guardian:approved' is missing"
-- one gate exempted the PR, the other did not, and the exemption could never
produce a merge without an operator approval (PR #212, PR #213).

These tests pin the workflow's *shape*:

* the "Enforce MergeGate" ``MERGED_PRS`` resolution is not nested inside a
  ``post_merge_reconcile`` marker test;
* it reads ``expected_pr`` from the BASE snapshot task file
  (``.shiki/gha/base-shiki/.shiki/tasks/<task_id>.json`` -- a field the PR head
  cannot forge), never the head's, and includes the PR only when its state is
  MERGED;
* it resolves the value the SAME way "Prepare CCA context" already does, so a
  ``post_merge_reconcile`` PR (marker + task) flows through the identical steps and
  receives exactly the ``--merged-prs`` value it received before; and
* a wiring-completeness enumeration: every invocation of ``mergegate_check.py`` and
  ``guardian_approval_signal.py`` in this workflow receives BOTH ``--base-shiki``
  and ``--merged-prs``, so a future invocation added without them fails this suite
  instead of silently reintroducing the gap.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "shiki-cca-completion.yml"

POST_MERGE_RECONCILE_MARKER = "<!-- shiki:post_merge_reconcile -->"

# The merged-PR resolution: the substrings that MUST appear, byte-for-byte, in the
# resolution wherever it runs. "Prepare CCA context" and "Enforce MergeGate" must
# share every one of these so both resolve the proof the same way.
RESOLUTION_SUBSTRINGS = (
    'MERGED_PRS=""',
    # TASK_ID with the SAME semantics mergegate_check.py uses.
    "grep -oE 'T-([0-9]{4,}|[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8})'",
    # The BASE snapshot task file -- never the head's checked-out .shiki/tasks/.
    'base_task=".shiki/gha/base-shiki/.shiki/tasks/${task_id}.json"',
    # expected_pr read from the BASE task, a field the PR head cannot write.
    "jq -r '.expected_pr // empty' \"$base_task\"",
    # Gated on a real MERGED state before the number is included.
    'gh pr view "$base_pr"',
    "--json state",
    'if [ "$state" = "MERGED" ]; then',
    'MERGED_PRS="$base_pr"',
)


class WorkflowText(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW_PATH.read_text(encoding="utf-8")

    # --- step extraction --------------------------------------------------

    def _step_block(self, step_name: str) -> str:
        """Return the text of the ``- name: <step_name>`` step: every line from
        the step header up to (but excluding) the next ``- name:`` step."""
        lines = self.text.splitlines()
        start = next(
            (i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}"),
            None,
        )
        self.assertIsNotNone(start, f"step {step_name!r} not found in workflow")
        block = [lines[start]]
        for line in lines[start + 1 :]:
            if line.strip().startswith("- name:"):
                break
            block.append(line)
        return "\n".join(block)

    def _script_invocations(self, script_basename: str) -> list[str]:
        """Every shell invocation of ``scripts/<script_basename>`` in the
        workflow, joining backslash-continued lines into one command string.

        A line is an invocation only when it is a command (not a ``#`` comment,
        so the prose references to ``mergegate_check.py`` are skipped) that
        contains the ``scripts/<basename>`` token.
        """
        token = f"scripts/{script_basename}"
        lines = self.text.splitlines()
        invocations: list[str] = []
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if token in stripped and not stripped.startswith("#"):
                block = [lines[i]]
                while block[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                    i += 1
                    block.append(lines[i])
                invocations.append("\n".join(block))
            i += 1
        return invocations

    # --- acceptance 1: not gated by the post_merge_reconcile marker --------

    def test_merged_prs_not_nested_in_post_merge_reconcile_marker(self) -> None:
        block = self._step_block("Enforce MergeGate")
        # The value can still be set...
        self.assertIn('MERGED_PRS="$base_pr"', block)
        # ...but the resolution is no longer gated behind the closeout-only
        # marker. Hoisted out (like "Prepare CCA context"), the marker test is
        # gone from the step, so nothing in the resolution can be nested inside
        # it. A closeout carries no marker, so a gated resolution left it empty.
        self.assertNotIn(
            POST_MERGE_RECONCILE_MARKER,
            block,
            "MERGED_PRS resolution must not be nested inside a "
            "post_merge_reconcile marker test",
        )

    # --- acceptance 2: base snapshot, never the head; only when MERGED -----

    def test_merged_prs_read_from_base_snapshot_not_head(self) -> None:
        block = self._step_block("Enforce MergeGate")
        # The task file is the BASE snapshot under .shiki/gha/base-shiki/...
        self.assertIn(
            'base_task=".shiki/gha/base-shiki/.shiki/tasks/${task_id}.json"',
            block,
        )
        # ...and expected_pr is read from that base task file, never the head's.
        self.assertIn("jq -r '.expected_pr // empty' \"$base_task\"", block)
        # There must be no read of expected_pr from anything other than the base
        # task file (e.g. the head checkout or the live PR json).
        for line in block.splitlines():
            if ".expected_pr" in line and not line.strip().startswith("#"):
                self.assertIn(
                    '"$base_task"',
                    line,
                    f"expected_pr must be read only from the base snapshot: {line.strip()!r}",
                )

    def test_merged_prs_included_only_when_state_merged(self) -> None:
        block = self._step_block("Enforce MergeGate")
        self.assertIn('gh pr view "$base_pr"', block)
        self.assertIn("--json state", block)
        merged_gate = block.index('if [ "$state" = "MERGED" ]; then')
        include = block.index('MERGED_PRS="$base_pr"')
        self.assertLess(
            merged_gate,
            include,
            "MERGED_PRS must be set only inside the state == MERGED gate",
        )

    # --- acceptance 3: resolved the SAME way as Prepare CCA context --------

    def test_resolution_matches_prepare_cca_context(self) -> None:
        # "Prepare CCA context" already resolves the proof unconditionally for the
        # CCA signal. "Enforce MergeGate" must resolve it the same way, so a
        # post_merge_reconcile PR (which carries the marker AND a task whose base
        # expected_pr is merged) flows through the identical steps and receives
        # exactly the --merged-prs value it received before this change.
        cca_context = self._step_block("Prepare CCA context")
        mergegate = self._step_block("Enforce MergeGate")
        for needle in RESOLUTION_SUBSTRINGS:
            self.assertIn(needle, cca_context, f"missing from Prepare CCA context: {needle!r}")
            self.assertIn(needle, mergegate, f"missing from Enforce MergeGate: {needle!r}")
        # Neither step gates the resolution behind the closeout-only marker; the
        # signal never did, and the gate no longer does.
        self.assertNotIn(POST_MERGE_RECONCILE_MARKER, cca_context)
        self.assertNotIn(POST_MERGE_RECONCILE_MARKER, mergegate)

    # --- acceptance 4: wiring completeness --------------------------------

    def test_merge_proof_invocations_receive_base_shiki_and_merged_prs(self) -> None:
        for script in ("mergegate_check.py", "guardian_approval_signal.py"):
            invocations = self._script_invocations(script)
            self.assertGreaterEqual(
                len(invocations),
                1,
                f"no invocation of scripts/{script} found; enumeration is vacuous",
            )
            for invocation in invocations:
                # The enumeration must have found a real command, not prose.
                self.assertTrue(
                    invocation.splitlines()[0].strip().startswith(f"python3 scripts/{script}"),
                    f"discovered non-command for {script}: {invocation.splitlines()[0]!r}",
                )
                self.assertIn(
                    "--base-shiki",
                    invocation,
                    f"invocation of {script} is missing --base-shiki:\n{invocation}",
                )
                self.assertIn(
                    "--merged-prs",
                    invocation,
                    f"invocation of {script} is missing --merged-prs:\n{invocation}",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
