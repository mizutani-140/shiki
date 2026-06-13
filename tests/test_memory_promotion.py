"""Memory Loop T1 — promotion state machine and HITL boundary (proposal 0001 v2 §3.1/3.4).

Covers the five operator acceptance rejections:

1. raw -> verified direct promotion rejected
2. raw -> distilled direct promotion rejected
3. verified with zero local evidence rejected
4. distilled without approval_ledger rejected
5. distill --approve refused in autonomous execution context
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_memory import (
    AUTONOMOUS_CONTEXT_ENV,
    MEMORY_LEDGER_TYPE,
    MEMORY_STATUSES,
    MEMORY_TRANSITIONS,
    capture_memory,
    distill_memory,
    investigate_memory,
    load_memory,
    memory_entry_errors,
    memory_transition_errors,
    promote_memory,
    revoke_memory,
    supersede_memory,
)
from shiki_process import ShikiError


def raw_entry() -> dict:
    return {
        "id": "MEM-20260613T000000000000Z-0000aaaa",
        "schema_version": 1,
        "status": "raw",
        "area": "locks",
        "applies_to": ["locks"],
        "tags": ["lock-declaration"],
        "claim": "Lock declarations were missing for repair branches.",
        "evidence": [],
        "source": {"kind": "manual", "goal_id": "G-0012", "task_id": None},
        "created_at": "2026-06-13T00:00:00+00:00",
        "updated_at": "2026-06-13T00:00:00+00:00",
        "redaction": {"status": "clean", "notes": ""},
    }


def entry_with_status(status: str, root: Path) -> dict:
    """Build a structurally complete entry for the requested status.

    Local evidence and approval ledger files are created under root so the
    happy path validates; tests then remove pieces to prove rejections.
    """
    entry = raw_entry()
    ledger_dir = root / ".shiki" / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    evidence_ledger = ledger_dir / "L-20260613T000000000000Z-0000bbbb.json"
    evidence_ledger.write_text("{}\n", encoding="utf-8")
    local_evidence = {"kind": "ledger", "path": ".shiki/ledger/L-20260613T000000000000Z-0000bbbb.json"}
    if status == "raw":
        return entry
    entry["status"] = "investigated"
    entry["investigation"] = {"summary": "Reproduced the lock omission.", "refs": []}
    if status == "investigated":
        return entry
    entry["status"] = "verified"
    entry["evidence"] = [local_evidence]
    entry["verification"] = {
        "verified_at": "2026-06-13T00:00:00+00:00",
        "validator": "validate_memory",
        "evidence": [local_evidence],
    }
    entry["last_verified"] = "2026-06-13T00:00:00+00:00"
    if status == "verified":
        return entry
    approval_ledger = ledger_dir / "L-20260613T000000000000Z-0000cccc.json"
    approval_ledger.write_text("{}\n", encoding="utf-8")
    entry["status"] = "distilled"
    entry["rule"] = "Declare every lock before dispatching a repair branch."
    entry["approved_by"] = "mizutani-140"
    entry["approved_at"] = "2026-06-13T00:00:00+00:00"
    entry["approval_ledger"] = ".shiki/ledger/L-20260613T000000000000Z-0000cccc.json"
    entry["active"] = True
    entry["supersedes"] = []
    entry["superseded_by"] = None
    entry["revoked_at"] = None
    entry["revoked_by"] = None
    entry["revocation_ledger"] = None
    return entry


class MemoryEntryValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_each_status_happy_path_validates(self) -> None:
        for status in MEMORY_STATUSES:
            entry = entry_with_status(status, self.root)
            self.assertEqual(memory_entry_errors(entry, root=self.root), [], status)

    def test_raw_prohibits_distilled_fields(self) -> None:
        entry = raw_entry()
        entry["rule"] = "premature rule"
        errors = memory_entry_errors(entry, root=self.root)
        self.assertTrue(any("rule" in error for error in errors))

    def test_investigated_requires_investigation_summary(self) -> None:
        entry = entry_with_status("investigated", self.root)
        entry["investigation"] = {"refs": []}
        self.assertTrue(memory_entry_errors(entry, root=self.root))

    def test_verified_with_zero_local_evidence_rejected(self) -> None:
        entry = entry_with_status("verified", self.root)
        remote_only = [{"kind": "pr_check", "pr": 126, "check": "MergeGate metadata check", "required": False}]
        entry["evidence"] = remote_only
        entry["verification"]["evidence"] = remote_only
        errors = memory_entry_errors(entry, root=self.root)
        self.assertTrue(any("local evidence" in error for error in errors), errors)

    def test_verified_local_evidence_path_must_exist(self) -> None:
        entry = entry_with_status("verified", self.root)
        missing = {"kind": "ledger", "path": ".shiki/ledger/L-20260613T000000000000Z-deadbeef.json"}
        entry["evidence"] = [missing]
        entry["verification"]["evidence"] = [missing]
        self.assertTrue(memory_entry_errors(entry, root=self.root))

    def test_distilled_without_approval_ledger_rejected(self) -> None:
        entry = entry_with_status("distilled", self.root)
        del entry["approval_ledger"]
        errors = memory_entry_errors(entry, root=self.root)
        self.assertTrue(any("approval_ledger" in error for error in errors), errors)

    def test_distilled_null_approval_ledger_rejected(self) -> None:
        entry = entry_with_status("distilled", self.root)
        entry["approval_ledger"] = None
        self.assertTrue(memory_entry_errors(entry, root=self.root))

    def test_distilled_approval_ledger_must_exist(self) -> None:
        entry = entry_with_status("distilled", self.root)
        entry["approval_ledger"] = ".shiki/ledger/L-20260613T000000000000Z-deadbeef.json"
        self.assertTrue(memory_entry_errors(entry, root=self.root))

    def test_active_rule_cannot_be_revoked_or_superseded(self) -> None:
        entry = entry_with_status("distilled", self.root)
        entry["revoked_at"] = "2026-06-13T01:00:00+00:00"
        self.assertTrue(memory_entry_errors(entry, root=self.root))
        entry = entry_with_status("distilled", self.root)
        entry["superseded_by"] = "MEM-20260613T000000000000Z-0000ffff"
        self.assertTrue(memory_entry_errors(entry, root=self.root))

    def test_revoked_rule_requires_revocation_evidence(self) -> None:
        entry = entry_with_status("distilled", self.root)
        entry["active"] = False
        entry["revoked_at"] = "2026-06-13T01:00:00+00:00"
        errors = memory_entry_errors(entry, root=self.root)
        self.assertTrue(any("revoked_by" in error or "revocation_ledger" in error for error in errors), errors)

    def test_unknown_status_and_area_rejected(self) -> None:
        entry = raw_entry()
        entry["status"] = "draft"
        self.assertTrue(memory_entry_errors(entry, root=self.root))
        entry = raw_entry()
        entry["area"] = "frontend"
        self.assertTrue(memory_entry_errors(entry, root=self.root))


class MemoryTransitionTests(unittest.TestCase):
    def test_allowed_chain_has_no_errors(self) -> None:
        for from_status, to_status in (
            ("raw", "investigated"),
            ("investigated", "verified"),
            ("verified", "distilled"),
        ):
            self.assertEqual(memory_transition_errors(from_status, to_status), [])

    def test_raw_to_verified_direct_promotion_rejected(self) -> None:
        errors = memory_transition_errors("raw", "verified")
        self.assertTrue(errors)
        self.assertIn("raw -> verified", "; ".join(errors))

    def test_raw_to_distilled_direct_promotion_rejected(self) -> None:
        errors = memory_transition_errors("raw", "distilled")
        self.assertTrue(errors)
        self.assertIn("raw -> distilled", "; ".join(errors))

    def test_investigated_to_distilled_skipping_rejected(self) -> None:
        self.assertTrue(memory_transition_errors("investigated", "distilled"))

    def test_no_backward_or_self_transitions(self) -> None:
        for from_status in MEMORY_STATUSES:
            self.assertTrue(memory_transition_errors(from_status, from_status))
        self.assertTrue(memory_transition_errors("verified", "raw"))
        self.assertTrue(memory_transition_errors("distilled", "verified"))

    def test_unknown_status_rejected(self) -> None:
        self.assertTrue(memory_transition_errors("raw", "approved"))
        self.assertTrue(memory_transition_errors("draft", "investigated"))

    def test_transition_map_matches_status_chain(self) -> None:
        self.assertEqual(
            MEMORY_TRANSITIONS,
            {
                "raw": ("investigated",),
                "investigated": ("verified",),
                "verified": ("distilled",),
                "distilled": (),
            },
        )


class StatusProhibitedFieldTests(unittest.TestCase):
    """B1: a lower status must not carry a higher status's fields."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_raw_with_investigation_rejected(self) -> None:
        self.assertTrue(memory_entry_errors(raw_entry() | {"investigation": {"summary": "x", "refs": []}}, root=self.root))

    def test_raw_with_verification_rejected(self) -> None:
        self.assertTrue(memory_entry_errors(raw_entry() | {"verification": {"verified_at": "t", "evidence": []}}, root=self.root))

    def test_raw_with_lifecycle_field_rejected(self) -> None:
        self.assertTrue(memory_entry_errors(raw_entry() | {"active": True}, root=self.root))

    def test_investigated_with_verification_rejected(self) -> None:
        entry = entry_with_status("investigated", self.root)
        entry["verification"] = {"verified_at": "t", "evidence": []}
        self.assertTrue(memory_entry_errors(entry, root=self.root))

    def test_investigated_with_last_verified_rejected(self) -> None:
        entry = entry_with_status("investigated", self.root)
        entry["last_verified"] = "2026-06-13T00:00:00Z"
        self.assertTrue(memory_entry_errors(entry, root=self.root))

    def test_verified_with_lifecycle_field_rejected(self) -> None:
        entry = entry_with_status("verified", self.root)
        entry["active"] = True
        self.assertTrue(memory_entry_errors(entry, root=self.root))


class EvidenceKindPathTests(unittest.TestCase):
    """B3: a local evidence kind must point at its own state directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.root / ".shiki" / "goals").mkdir(parents=True, exist_ok=True)
        (self.root / ".shiki" / "goals" / "G-0001.json").write_text("{}\n")

    def test_ledger_kind_pointing_at_goal_rejected(self) -> None:
        entry = entry_with_status("verified", self.root)
        wrong = [{"kind": "ledger", "path": ".shiki/goals/G-0001.json"}]
        entry["evidence"] = wrong
        entry["verification"]["evidence"] = wrong
        self.assertTrue(memory_entry_errors(entry, root=self.root))

    def test_report_kind_pointing_at_ledger_rejected(self) -> None:
        entry = entry_with_status("verified", self.root)
        ledger = self.root / ".shiki" / "ledger" / "L-20260613T000000000000Z-0000bbbb.json"
        wrong = [{"kind": "report", "path": f".shiki/ledger/{ledger.name}"}]
        entry["evidence"] = wrong
        entry["verification"]["evidence"] = wrong
        self.assertTrue(memory_entry_errors(entry, root=self.root))


class RedactionTests(unittest.TestCase):
    """B4: a persisted entry may not be redaction.status=skipped."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_stored_entry_skipped_redaction_rejected(self) -> None:
        entry = raw_entry()
        entry["redaction"] = {"status": "skipped", "notes": ""}
        self.assertTrue(memory_entry_errors(entry, root=self.root))

    def test_clean_and_redacted_accepted(self) -> None:
        for status in ("clean", "redacted"):
            entry = raw_entry()
            entry["redaction"] = {"status": status, "notes": ""}
            self.assertEqual(memory_entry_errors(entry, root=self.root), [], status)


class AutonomousContextBoundaryTests(unittest.TestCase):
    """Acceptance check 5: operator-only actions are refused in autonomous context."""

    def setUp(self) -> None:
        import os
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._prev = os.environ.get(AUTONOMOUS_CONTEXT_ENV)
        os.environ[AUTONOMOUS_CONTEXT_ENV] = "1"

        def _restore() -> None:
            if self._prev is None:
                os.environ.pop(AUTONOMOUS_CONTEXT_ENV, None)
            else:
                os.environ[AUTONOMOUS_CONTEXT_ENV] = self._prev

        self.addCleanup(_restore)

    def test_distill_refused_in_autonomous_context(self) -> None:
        with self.assertRaises(ShikiError):
            distill_memory(self.root, "MEM-0001", rule="x", approved_by="op", approve=True)

    def test_revoke_refused_in_autonomous_context(self) -> None:
        with self.assertRaises(ShikiError):
            revoke_memory(self.root, "MEM-0001", revoked_by="op", reason="x")

    def test_supersede_refused_in_autonomous_context(self) -> None:
        with self.assertRaises(ShikiError):
            supersede_memory(self.root, "MEM-0001", superseded_by="MEM-0002")


if __name__ == "__main__":
    unittest.main()
