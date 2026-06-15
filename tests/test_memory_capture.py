"""Auto-capture redaction and fail-open behavior (proposal 0001 v2 section 3.3).

Auto-capture must never copy secret-like tokens, command output bodies, or env
values into a committed memory file. It stores a short redacted claim plus
structured evidence references, records redaction.status, skips (writes nothing)
when it cannot guarantee a safe claim, and never raises into its callers.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shiki_test_support import synthetic_secrets  # noqa: F401  (path bootstrap + secrets)

from shiki_memory import capture_failure, redact_text
from shiki_process import ensure_control_dirs

GOAL_ID = "G-20260613T000000000000Z-0000aaaa"
# Secret-shaped strings assembled at runtime (no literal token in source).
SECRETS = list(synthetic_secrets().values())
A_TOKEN = synthetic_secrets()["github_pat"]


def _seed_goal(root: Path) -> str:
    ensure_control_dirs(root)
    (root / ".shiki" / "goals" / f"{GOAL_ID}.json").write_text(
        json.dumps({"id": GOAL_ID, "status": "running", "title": "t", "outcome": "o"}),
        encoding="utf-8",
    )
    return GOAL_ID


def _read_memory(root: Path, memory_id: str) -> dict:
    return json.loads((root / ".shiki" / "memories" / f"{memory_id}.json").read_text(encoding="utf-8"))


class RedactTextTests(unittest.TestCase):
    def test_clean_text_is_unchanged_and_not_flagged(self) -> None:
        redacted, found = redact_text("Lock declarations were missing on review-fix files.")
        self.assertFalse(found)
        self.assertEqual(redacted, "Lock declarations were missing on review-fix files.")

    def test_each_secret_pattern_is_redacted(self) -> None:
        for secret in SECRETS:
            redacted, found = redact_text(f"runner failed with {secret} in output")
            self.assertTrue(found, f"secret not detected: {secret}")
            self.assertNotIn(secret, redacted, f"secret leaked: {secret}")
            self.assertIn("[REDACTED]", redacted)

    def test_pem_private_key_is_redacted(self) -> None:
        redacted, found = redact_text("-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----")
        self.assertTrue(found)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", redacted)


class CaptureFailureTests(unittest.TestCase):
    def test_clean_capture_writes_redaction_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goal = _seed_goal(root)
            result = capture_failure(
                root, source_kind="repair", area="locks",
                claim="Repair packet for T-0001: 2 failing checks (locks).",
                goal_id=goal, task_id=None, evidence_refs=[],
            )
            self.assertTrue(result.written)
            mem = _read_memory(root, result.memory_id)
            self.assertEqual(mem["redaction"]["status"], "clean")
            self.assertEqual(mem["status"], "raw")
            self.assertEqual(mem["source"]["kind"], "repair")

    def test_secret_in_claim_is_redacted_and_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goal = _seed_goal(root)
            secret = A_TOKEN
            result = capture_failure(
                root, source_kind="runner_fail", area="runner",
                claim=f"runner failed: token {secret} rejected",
                goal_id=goal, task_id=None, evidence_refs=[],
            )
            self.assertTrue(result.written)
            raw = (root / ".shiki" / "memories" / f"{result.memory_id}.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)  # the secret must never reach disk
            self.assertEqual(json.loads(raw)["redaction"]["status"], "redacted")

    def test_evidence_refs_are_structured_not_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goal = _seed_goal(root)
            ledger_ref = ".shiki/ledger/L-20260613T000000000000Z-0000bbbb"
            (root / ledger_ref).write_text(
                json.dumps({"id": "L-20260613T000000000000Z-0000bbbb", "goal_id": goal,
                            "task_id": None, "type": "check", "actor": "x",
                            "timestamp": "2026-06-13T00:00:00+00:00", "summary": "s", "evidence": []}),
                encoding="utf-8",
            )
            result = capture_failure(
                root, source_kind="cca_fail", area="cca",
                claim="CCA verdict failed after 2 reruns.",
                goal_id=goal, task_id=None, evidence_refs=[ledger_ref],
            )
            self.assertTrue(result.written, result.warnings)
            mem = _read_memory(root, result.memory_id)
            self.assertEqual(mem["evidence"], [{"kind": "ledger", "path": ledger_ref}])

    def test_capture_failure_never_raises_on_bad_input(self) -> None:
        # An unknown goal (source validation fails) must fail open: no write, no raise.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ensure_control_dirs(root)
            result = capture_failure(
                root, source_kind="loop_stop", area="locks", claim="x",
                goal_id="G-99999999", task_id=None, evidence_refs=[],
            )
            self.assertFalse(result.written)
            self.assertTrue(result.warnings)

    def test_unsalvageable_secret_claim_is_skipped(self) -> None:
        # A claim that is essentially only a secret cannot yield a safe short
        # claim; capture writes nothing and warns rather than persisting it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            goal = _seed_goal(root)
            result = capture_failure(
                root, source_kind="runner_fail", area="runner",
                claim=A_TOKEN,
                goal_id=goal, task_id=None, evidence_refs=[],
            )
            self.assertFalse(result.written)
            self.assertTrue(result.warnings)


if __name__ == "__main__":
    unittest.main()
