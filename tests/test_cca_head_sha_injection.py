"""The CCA head SHA is workflow-supplied, and the base-ref fetch is not shallow.

Two failure modes on live PRs motivated these tests.

Symptom (a): a CCA job dies in seconds with ``fatal: origin/main...HEAD: no
merge base`` right after some other PR merges, because the base-ref fetch carries
``--depth=1`` and shallows the base ref back down even though the job checked out
with ``fetch-depth: 0``. That destroys the merge base the following
``git diff --name-only "origin/${base_ref}...HEAD"`` needs. The fix is to drop
``--depth=1`` from both base-ref fetches. Every open PR then reaches a verdict
without a base merge, so no PR's head-bound Guardian approval is invalidated by a
forced merge.

Symptom (b): ``MergeGate policy check`` blocks with ``CCA head_sha does not match
the current PR headRefOid`` on a PR whose run head and PR head are byte-identical,
because the model mistyped the SHA into its structured output (PR #195 wrote a
value four characters short). The fix is to treat the head SHA as
workflow-supplied: ``enforce_cca_verdict.inject_authoritative_head_sha`` overwrites
``verdict["head_sha"]`` from the ``SHIKI_HEAD_SHA`` environment variable before
validation and before the verdict file is written, preserving the model's value
under a separate non-authoritative key and warning when the two disagree so the
disagreement is auditable in the uploaded evidence rather than silently erased.

These tests exercise the real ``scripts/enforce_cca_verdict.py`` module and the
real ``.github/workflows/shiki-cca-completion.yml`` and never modify either.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

import enforce_cca_verdict

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "shiki-cca-completion.yml"

HEAD_SHA_ENV = "SHIKI_HEAD_SHA"
REPORTED_KEY = "head_sha_reported"

# PR #195: the model wrote a value four characters short of the real head SHA.
# The two are used verbatim so a disagreement is exercised exactly as observed.
AUTHORITATIVE_SHA = "9585b399e30df02429dc25a0afdaa37efa7aeebb"
MODEL_TRUNCATED_SHA = "9585b399e30df02429dc25a0afdaa37eaebb"

# The exact expression the manifest builder already uses for --head-sha; the
# Enforce CCA verdict step must pass SHIKI_HEAD_SHA from the same source.
HEAD_SHA_EXPRESSION = (
    "SHIKI_HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}"
)


@contextlib.contextmanager
def env(**overrides):
    """Set (value) or clear (``None``) env vars for the block, then restore."""
    saved = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def in_repo_root():
    """``validate_verdict`` loads schemas relative to the working directory."""
    previous = Path.cwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


def make_verdict(head_sha=MODEL_TRUNCATED_SHA, verdict="blocked"):
    """A schema-valid verdict with one passing blocking item.

    ``blocked`` with a single passing blocking checklist item validates cleanly
    (no blocking failure, no short-circuited evaluation, no repair_packet
    required), so the injection can be exercised end-to-end through ``main``.
    """
    return {
        "verdict": verdict,
        "summary": "Head SHA injection fixture.",
        "goal_id": "G-20260730T110603196220Z-c50d61b5",
        "task_id": "T-20260730T110603197694Z-c903d404",
        "pr": 195,
        "head_sha": head_sha,
        "can_merge": False,
        "checklist": [
            {
                "id": "CCA-01",
                "status": "pass",
                "blocking": True,
                "evidence": "Validate Shiki mirror succeeded for the head SHA.",
            },
        ],
        "acceptance": [
            {
                "criterion": "A1",
                "status": "pass",
                "evidence": ["The head SHA is the workflow-supplied value."],
            },
        ],
        "mergegate": {"required_checks": "pending"},
        "confidence": 0.9,
    }


class HeadShaInjection(unittest.TestCase):
    def test_authoritative_replaces_mismatched_model_value(self):
        verdict = make_verdict(head_sha=MODEL_TRUNCATED_SHA)
        stderr = io.StringIO()
        with env(**{HEAD_SHA_ENV: AUTHORITATIVE_SHA}):
            with contextlib.redirect_stderr(stderr):
                result = enforce_cca_verdict.inject_authoritative_head_sha(verdict)

        self.assertEqual(result["head_sha"], AUTHORITATIVE_SHA)
        self.assertEqual(result[REPORTED_KEY], MODEL_TRUNCATED_SHA)

        warning = stderr.getvalue()
        self.assertIn(AUTHORITATIVE_SHA, warning)
        self.assertIn(MODEL_TRUNCATED_SHA, warning)

    def test_matching_model_value_is_unchanged_and_no_reported_key(self):
        verdict = make_verdict(head_sha=AUTHORITATIVE_SHA)
        before = json.dumps(verdict, sort_keys=True)
        stderr = io.StringIO()
        with env(**{HEAD_SHA_ENV: AUTHORITATIVE_SHA}):
            with contextlib.redirect_stderr(stderr):
                result = enforce_cca_verdict.inject_authoritative_head_sha(verdict)

        self.assertEqual(result["head_sha"], AUTHORITATIVE_SHA)
        self.assertNotIn(REPORTED_KEY, result)
        self.assertEqual(json.dumps(result, sort_keys=True), before)
        self.assertEqual(stderr.getvalue(), "")

    def test_unset_variable_leaves_verdict_untouched(self):
        verdict = make_verdict(head_sha=MODEL_TRUNCATED_SHA)
        before = json.dumps(verdict, sort_keys=True)
        with env(**{HEAD_SHA_ENV: None}):
            result = enforce_cca_verdict.inject_authoritative_head_sha(verdict)

        self.assertEqual(json.dumps(result, sort_keys=True), before)
        self.assertNotIn(REPORTED_KEY, result)

    def test_empty_variable_leaves_verdict_untouched(self):
        verdict = make_verdict(head_sha=MODEL_TRUNCATED_SHA)
        before = json.dumps(verdict, sort_keys=True)
        with env(**{HEAD_SHA_ENV: ""}):
            result = enforce_cca_verdict.inject_authoritative_head_sha(verdict)

        self.assertEqual(json.dumps(result, sort_keys=True), before)
        self.assertNotIn(REPORTED_KEY, result)

    def test_whitespace_only_variable_leaves_verdict_untouched(self):
        # The authoritative value is stripped, so a whitespace-only variable is
        # treated as unset and must not overwrite or add a reported key.
        verdict = make_verdict(head_sha=MODEL_TRUNCATED_SHA)
        before = json.dumps(verdict, sort_keys=True)
        with env(**{HEAD_SHA_ENV: "   "}):
            result = enforce_cca_verdict.inject_authoritative_head_sha(verdict)

        self.assertEqual(json.dumps(result, sort_keys=True), before)
        self.assertNotIn(REPORTED_KEY, result)

    def test_downstream_validation_and_written_file_see_injected_value(self):
        # Prove the injection precedes validation and file writing: the value
        # validate_verdict observes and the value written to the verdict file are
        # both the authoritative one, and the model's value survives for audit.
        verdict = make_verdict(head_sha=MODEL_TRUNCATED_SHA)
        seen = {}
        original_validate = enforce_cca_verdict.validate_verdict

        def spy(candidate):
            seen["head_sha"] = candidate.get("head_sha")
            seen[REPORTED_KEY] = candidate.get(REPORTED_KEY)
            return original_validate(candidate)

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "cca-verdict.json"
            enforce_cca_verdict.validate_verdict = spy
            try:
                with env(
                    **{
                        HEAD_SHA_ENV: AUTHORITATIVE_SHA,
                        "STRUCTURED_OUTPUT": json.dumps(verdict),
                        "CCA_VERDICT_FILE": str(out_path),
                    }
                ):
                    with in_repo_root():
                        # blocked verdict returns 1 and prints itself; we assert
                        # on state, not code, and silence the diagnostic streams.
                        with contextlib.redirect_stdout(io.StringIO()):
                            with contextlib.redirect_stderr(io.StringIO()):
                                enforce_cca_verdict.main()
            finally:
                enforce_cca_verdict.validate_verdict = original_validate

            written = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(seen["head_sha"], AUTHORITATIVE_SHA)
        self.assertEqual(seen[REPORTED_KEY], MODEL_TRUNCATED_SHA)
        self.assertEqual(written["head_sha"], AUTHORITATIVE_SHA)
        self.assertEqual(written[REPORTED_KEY], MODEL_TRUNCATED_SHA)


class WorkflowShape(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def _step_block(self, step_name):
        lines = self.text.splitlines()
        start = next(
            (
                i
                for i, line in enumerate(lines)
                if line.strip() == f"- name: {step_name}"
            ),
            None,
        )
        self.assertIsNotNone(start, f"step {step_name!r} not found in workflow")
        block = [lines[start]]
        for line in lines[start + 1 :]:
            if line.strip().startswith("- name:"):
                break
            block.append(line)
        return "\n".join(block)

    def test_neither_base_fetch_is_shallow(self):
        fetch_lines = [
            line
            for line in self.text.splitlines()
            if 'git fetch origin "$base_ref"' in line
        ]
        self.assertEqual(
            len(fetch_lines),
            2,
            f"expected two base-ref fetches, found {len(fetch_lines)}",
        )
        for line in fetch_lines:
            self.assertNotIn(
                "--depth",
                line,
                f"base-ref fetch must not be shallow: {line.strip()!r}",
            )

    def test_both_jobs_checkout_full_history(self):
        # A non-shallow base fetch only helps if the checkout brought full
        # history; both jobs must keep fetch-depth: 0.
        self.assertEqual(self.text.count("fetch-depth: 0"), 2)

    def test_enforce_step_passes_authoritative_head_sha(self):
        block = self._step_block("Enforce CCA verdict")
        self.assertIn(HEAD_SHA_EXPRESSION, block)


if __name__ == "__main__":
    unittest.main()
