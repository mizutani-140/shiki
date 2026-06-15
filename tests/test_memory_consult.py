"""Consult — deterministic distilled-rule injection (proposal 0001 v2 section 3.5).

T3 injects only active, non-superseded, non-revoked distilled rules into the
handoff, selected deterministically and stable-sorted (last_verified desc, id
asc), each printed with its MEM id. Because the frozen spec references
task.area / goal.area but neither the task nor goal schema defines area/tags
(CI-08 assumption, operator-approved), those operands are a DERIVED,
non-persisted consult context computed from the task locks (path->area) and
required_skills (->tags). No schema change; reading never mutates state; when no
rule matches the handoff still emits "## Distilled Rules" with "none applicable".
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shiki_test_support import synthetic_secrets  # noqa: F401  (path bootstrap)

from shiki_memory import (
    derive_consult_context,
    load_all_memories,
    render_distilled_rules_section,
    select_distilled_rules,
)
from shiki_process import ensure_control_dirs

GOAL_ID = "G-20260612T152357704854Z-8a5508cf"


def _task(locks=None, skills=None, task_id="T-20260612T152357707653Z-e200274d"):
    return {
        "id": task_id,
        "goal_id": GOAL_ID,
        "locks": locks if locks is not None else ["path:scripts/shiki_memory.py"],
        "required_skills": skills if skills is not None else ["tdd", "code-review"],
        "scope": "s",
        "assigned_runtime": "claude-code",
        "expected_branch": "b",
        "acceptance_checks": [],
    }


def _goal(skills=None):
    return {"id": GOAL_ID, "status": "planned", "title": "Memory Loop",
            "outcome": "o", "required_skills": skills or []}


def _rule(mem_id, *, area="memory", applies_to=None, tags=None, active=True,
          superseded_by=None, revoked_at=None, last_verified="2026-06-10T00:00:00+00:00",
          rule="Always declare ledger self-references", status="distilled"):
    return {
        "id": mem_id,
        "schema_version": 1,
        "status": status,
        "area": area,
        "applies_to": applies_to if applies_to is not None else [],
        "tags": tags if tags is not None else [],
        "rule": rule,
        "active": active,
        "superseded_by": superseded_by,
        "revoked_at": revoked_at,
        "last_verified": last_verified,
        "approved_by": "mizutani-140",
        "approved_at": "2026-06-10T00:00:00+00:00",
    }


class DeriveConsultContextTests(unittest.TestCase):
    def test_locks_map_to_areas_and_skills_to_tags(self):
        ctx = derive_consult_context(
            _task(locks=["path:scripts/shiki_memory.py", "path:scripts/mergegate_check.py"],
                  skills=["tdd", "code-review"]),
            _goal(),
        )
        self.assertIn("memory", ctx["areas"])
        self.assertIn("mergegate", ctx["areas"])
        self.assertIn("tdd", ctx["tags"])
        self.assertIn("code-review", ctx["tags"])

    def test_goal_skills_contribute_tags(self):
        ctx = derive_consult_context(_task(skills=[]), _goal(skills=["diagnose"]))
        self.assertIn("diagnose", ctx["tags"])


class SelectDistilledRulesTests(unittest.TestCase):
    def test_match_by_area(self):
        # task locks -> derived area "memory"; rule.area == "memory"
        rules = select_distilled_rules(
            _task(locks=["path:scripts/shiki_memory.py"]), _goal(),
            [_rule("MEM-0001", area="memory", applies_to=[], tags=[])],
        )
        self.assertEqual([r["id"] for r in rules], ["MEM-0001"])

    def test_match_by_applies_to(self):
        rules = select_distilled_rules(
            _task(locks=["path:scripts/mergegate_check.py"]), _goal(),
            [_rule("MEM-0002", area="other", applies_to=["mergegate"], tags=[])],
        )
        self.assertEqual([r["id"] for r in rules], ["MEM-0002"])

    def test_match_by_tags(self):
        rules = select_distilled_rules(
            _task(locks=["path:docs/x.md"], skills=["tdd"]), _goal(),
            [_rule("MEM-0003", area="other", applies_to=[], tags=["tdd"])],
        )
        self.assertEqual([r["id"] for r in rules], ["MEM-0003"])

    def test_none_applicable_when_no_overlap(self):
        rules = select_distilled_rules(
            _task(locks=["path:scripts/shiki_memory.py"], skills=["tdd"]), _goal(),
            [_rule("MEM-0004", area="runner", applies_to=["loop"], tags=["perf"])],
        )
        self.assertEqual(rules, [])

    def test_exclude_inactive_revoked_superseded(self):
        mems = [
            _rule("MEM-0010", area="memory", active=False),
            _rule("MEM-0011", area="memory", active=False,
                  revoked_at="2026-06-11T00:00:00+00:00"),
            _rule("MEM-0012", area="memory", superseded_by="MEM-0099"),
            _rule("MEM-0013", area="memory", status="verified"),  # not distilled
            _rule("MEM-0014", area="memory"),  # the only valid match
        ]
        rules = select_distilled_rules(_task(locks=["path:scripts/shiki_memory.py"]), _goal(), mems)
        self.assertEqual([r["id"] for r in rules], ["MEM-0014"])

    def test_exclude_unscoped_rule(self):
        # active distilled rule with no overlapping selector is excluded
        rules = select_distilled_rules(
            _task(locks=["path:scripts/shiki_memory.py"]), _goal(),
            [_rule("MEM-0020", area="cca", applies_to=[], tags=[])],
        )
        self.assertEqual(rules, [])

    def test_stable_sort_last_verified_desc_then_id_asc(self):
        mems = [
            _rule("MEM-b", area="memory", last_verified="2026-06-01T00:00:00+00:00"),
            _rule("MEM-a", area="memory", last_verified="2026-06-09T00:00:00+00:00"),
            _rule("MEM-c", area="memory", last_verified="2026-06-09T00:00:00+00:00"),
            _rule("MEM-d", area="memory", last_verified=None),  # missing -> last
        ]
        rules = select_distilled_rules(_task(locks=["path:scripts/shiki_memory.py"]), _goal(), mems)
        self.assertEqual([r["id"] for r in rules], ["MEM-a", "MEM-c", "MEM-b", "MEM-d"])

    def test_selector_does_not_mutate_inputs(self):
        mems = [_rule("MEM-0030", area="memory")]
        before = json.dumps(mems, sort_keys=True)
        select_distilled_rules(_task(locks=["path:scripts/shiki_memory.py"]), _goal(), mems)
        self.assertEqual(json.dumps(mems, sort_keys=True), before)


class RenderSectionTests(unittest.TestCase):
    def test_none_applicable_section(self):
        lines = render_distilled_rules_section([])
        self.assertIn("## Distilled Rules", lines)
        self.assertIn("none applicable", lines)

    def test_rules_render_with_mem_ids(self):
        lines = render_distilled_rules_section(
            [_rule("MEM-0001", rule="Declare ledger self-references")]
        )
        text = "\n".join(lines)
        self.assertIn("## Distilled Rules", text)
        self.assertIn("Declare ledger self-references", text)
        self.assertIn("(MEM-0001)", text)
        self.assertNotIn("none applicable", text)


class NoSchemaChangeTests(unittest.TestCase):
    def test_task_and_goal_schemas_have_no_area_or_tags(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("task.schema.json", "goal.schema.json"):
            schema = json.loads((root / ".shiki" / "schemas" / name).read_text(encoding="utf-8"))
            props = schema.get("properties", {})
            self.assertNotIn("area", props, f"{name} must not gain an area field (CI-08 assumption)")
            self.assertNotIn("tags", props, f"{name} must not gain a tags field (CI-08 assumption)")


class LoadAllMemoriesTests(unittest.TestCase):
    def test_reads_without_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ensure_control_dirs(root)
            mem_dir = root / ".shiki" / "memories"
            mem_dir.mkdir(parents=True, exist_ok=True)
            entry = _rule("MEM-20260615T000000000000Z-aaaaaaaa", area="memory")
            (mem_dir / f"{entry['id']}.json").write_text(json.dumps(entry), encoding="utf-8")
            before = (mem_dir / f"{entry['id']}.json").read_text(encoding="utf-8")
            loaded = load_all_memories(root)
            self.assertEqual([m["id"] for m in loaded], [entry["id"]])
            self.assertEqual((mem_dir / f"{entry['id']}.json").read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
