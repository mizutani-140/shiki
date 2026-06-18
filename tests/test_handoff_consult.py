"""write_task_handoff always emits a Distilled Rules section (proposal §3.5/§3.7).

The handoff writer injects the selected active distilled rules (with MEM ids) and
always renders the "## Distilled Rules" heading — "none applicable" when nothing
matches. Generation is failure-tolerant and never mutates memory/task/goal state;
dispatch regenerates it every time (no write-if-missing cache).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shiki_test_support import synthetic_secrets  # noqa: F401  (path bootstrap)

from shiki_process import ensure_control_dirs
from shiki_tasks import write_task_handoff

GOAL_ID = "G-20260612T152357704854Z-8a5508cf"
TASK_ID = "T-20260612T152357707653Z-e200274d"


def _seed(root: Path, *, locks, skills=("tdd", "code-review")):
    ensure_control_dirs(root)
    (root / ".shiki" / "goals" / f"{GOAL_ID}.json").write_text(
        json.dumps({"id": GOAL_ID, "status": "planned", "title": "Memory Loop",
                    "outcome": "o", "required_skills": []}),
        encoding="utf-8")
    (root / ".shiki" / "tasks" / f"{TASK_ID}.json").write_text(
        json.dumps({"id": TASK_ID, "goal_id": GOAL_ID, "status": "ready",
                    "assigned_runtime": "claude-code", "expected_branch": "b",
                    "scope": "s", "acceptance_checks": [], "locks": list(locks),
                    "required_skills": list(skills)}),
        encoding="utf-8")


def _write_rule(root: Path, mem_id, *, area="memory", applies_to=None, tags=None,
                rule="Declare ledger self-references", active=True,
                superseded_by=None, revoked_at=None,
                last_verified="2026-06-10T00:00:00+00:00"):
    mem_dir = root / ".shiki" / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    entry = {"id": mem_id, "schema_version": 1, "status": "distilled", "area": area,
             "applies_to": applies_to or [], "tags": tags or [], "rule": rule,
             "active": active, "superseded_by": superseded_by, "revoked_at": revoked_at,
             "last_verified": last_verified, "approved_by": "op", "approved_at": "x"}
    (mem_dir / f"{mem_id}.json").write_text(json.dumps(entry), encoding="utf-8")


class HandoffConsultTests(unittest.TestCase):
    def test_section_present_none_applicable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _seed(root, locks=["path:scripts/shiki_memory.py"])
            handoff_file, _ = write_task_handoff(root, TASK_ID)
            body = handoff_file.read_text(encoding="utf-8")
            self.assertIn("## Distilled Rules", body)
            self.assertIn("none applicable", body)

    def test_execution_protocol_guardrail_present(self):
        # The handoff must tell the implementer to edit only — the Shiki loop owns
        # commit/push/PR creation. Without this guardrail a bypassPermissions
        # implementer can self-open a loop-incompatible PR and break create_pr.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _seed(root, locks=["path:scripts/shiki_memory.py"])
            body = write_task_handoff(root, TASK_ID)[0].read_text(encoding="utf-8")
            self.assertIn("## Execution Protocol", body)
            self.assertIn("git commit", body)
            self.assertIn("git push", body)
            for token in ("gh", "pull request"):
                self.assertIn(token, body)
            # The guardrail must precede the Scope so the implementer reads it first.
            self.assertLess(body.index("## Execution Protocol"), body.index("## Scope"))

    def test_matching_rule_injected_with_mem_id(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _seed(root, locks=["path:scripts/shiki_memory.py"])
            _write_rule(root, "MEM-20260612T143733349559Z-fe72ae6a",
                        area="memory", rule="Declare ledger self-references")
            handoff_file, _ = write_task_handoff(root, TASK_ID)
            body = handoff_file.read_text(encoding="utf-8")
            self.assertIn("## Distilled Rules", body)
            self.assertIn("Declare ledger self-references", body)
            self.assertIn("(MEM-20260612T143733349559Z-fe72ae6a)", body)
            self.assertNotIn("none applicable", body)

    def test_revoked_rule_never_injected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _seed(root, locks=["path:scripts/shiki_memory.py"])
            _write_rule(root, "MEM-20260612T143733349559Z-fe72ae6a",
                        area="memory", active=False, revoked_at="2026-06-11T00:00:00+00:00")
            body = write_task_handoff(root, TASK_ID)[0].read_text(encoding="utf-8")
            self.assertIn("none applicable", body)

    def test_missing_goal_degrades_to_none_applicable(self):
        # A desynced mirror (goal file absent) must degrade, never crash the
        # now-unconditional dispatch regeneration.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _seed(root, locks=["path:scripts/shiki_memory.py"])
            (root / ".shiki" / "goals" / f"{GOAL_ID}.json").unlink()
            handoff_file, _ = write_task_handoff(root, TASK_ID)  # must not raise
            body = handoff_file.read_text(encoding="utf-8")
            self.assertIn("## Distilled Rules", body)
            self.assertIn("none applicable", body)

    def test_writer_overwrites_stale_handoff(self):
        # The write-if-missing cache is removed: regenerating always overwrites a
        # stale on-disk handoff with the freshly injected section.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _seed(root, locks=["path:scripts/shiki_memory.py"])
            handoff_path = root / ".shiki" / "handoffs" / f"{TASK_ID}-task.md"
            handoff_path.parent.mkdir(parents=True, exist_ok=True)
            handoff_path.write_text("STALE — no distilled rules here\n", encoding="utf-8")
            _write_rule(root, "MEM-20260612T143733349559Z-fe72ae6a",
                        area="memory", rule="Declare ledger self-references")
            write_task_handoff(root, TASK_ID)
            body = handoff_path.read_text(encoding="utf-8")
            self.assertNotIn("STALE", body)
            self.assertIn("## Distilled Rules", body)
            self.assertIn("(MEM-20260612T143733349559Z-fe72ae6a)", body)

    def test_regenerates_and_does_not_mutate_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _seed(root, locks=["path:scripts/shiki_memory.py"])
            mem_id = "MEM-20260612T143733349559Z-fe72ae6a"
            _write_rule(root, mem_id, area="memory")
            mem_path = root / ".shiki" / "memories" / f"{mem_id}.json"
            task_path = root / ".shiki" / "tasks" / f"{TASK_ID}.json"
            goal_path = root / ".shiki" / "goals" / f"{GOAL_ID}.json"
            before = {p: p.read_text(encoding="utf-8") for p in (mem_path, task_path, goal_path)}
            handoff_file, _ = write_task_handoff(root, TASK_ID)
            first = handoff_file.read_text(encoding="utf-8")
            # Regenerate: writer always rewrites (no cache); content is stable.
            write_task_handoff(root, TASK_ID)
            self.assertEqual(handoff_file.read_text(encoding="utf-8"), first)
            for p, original in before.items():
                self.assertEqual(p.read_text(encoding="utf-8"), original,
                                 f"consult injection must not mutate {p.name}")


if __name__ == "__main__":
    unittest.main()
