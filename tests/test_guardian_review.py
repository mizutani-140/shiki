"""Tests for the External AI Guardian Review adapter contract (ADR 0010/0014).

Covers the deterministic Shiki-side surfaces consumed by the Codex App External
AI Guardian UI Adapter: packet schema validation, the PR-type focus classifier,
the GPT Pro prompt builder, and reviewer response extraction + verification.
These never drive a ChatGPT UI and never let an implementer self-approve.
"""

from __future__ import annotations

import copy
import json
import unittest

import shiki_test_support  # noqa: F401  (path bootstrap)
from shiki_test_support import REPO_ROOT

from shiki_contracts import CANONICAL_EXTERNAL_AI_GUARDIAN_REVIEW_PACKET_SCHEMA_PATH
from shiki_guardian import GUARDIAN_POLICY_PATH, load_guardian_policy_file
from shiki_jsonschema import JsonSchemaError, validate_json_schema
import shiki_guardian_review as gr

SCHEMA = json.loads((REPO_ROOT / CANONICAL_EXTERNAL_AI_GUARDIAN_REVIEW_PACKET_SCHEMA_PATH).read_text(encoding="utf-8"))
POLICY = load_guardian_policy_file(REPO_ROOT / GUARDIAN_POLICY_PATH)

TASK = {
    "id": "T-20260101T000000000000Z-deadbeef",
    "goal_id": "G-20260101T000000000000Z-deadbeef",
    "scope": "Add a guardian / mergegate authority change to scripts/shiki_guardian.py",
    "non_goals": ["Do not change CCA"],
    "acceptance_checks": ["validate_shiki passes"],
    "locks": ["path:scripts/shiki_guardian.py"],
    "risk_level": "high",
}
PR_DATA = {
    "repository": "mizutani-140/shiki",
    "pr_number": 175,
    "base_sha": "aaaaaaaa",
    "head_sha": "bbbbbbbb",
    "pr_summary": "Implements the external AI guardian adapter contract.",
    "changed_files": ["scripts/shiki_guardian.py", "scripts/shiki_guardian_review.py"],
    "diff_summary": "Added validate_ai_review_artifact and the adapter module.",
    "check_results": [{"name": "Validate Shiki mirror", "status": "success"}],
}


def make_packet(**overrides):
    pr_data = copy.deepcopy(PR_DATA)
    pr_data.update(overrides.pop("pr_data", {}))
    task = copy.deepcopy(TASK)
    task.update(overrides.pop("task", {}))
    return gr.build_review_packet(
        task=task,
        pr_data=pr_data,
        implementer_report={"source": "claude full response", "summary": "implemented per handoff"},
        relevant_docs=["docs/adr/0010-external-ai-guardian-review.md"],
        generated_at="20260101T000000000000Z",
        source_refs=["gh pr view 175", ".shiki/tasks/T-...json"],
    )


def approval_artifact(**overrides):
    artifact = {
        "kind": "external_ai_guardian_review",
        "reviewer": {"type": "ai_model", "model": "GPT-5.5 Pro", "role": "external_guardian_reviewer"},
        "repo": "mizutani-140/shiki",
        "pr": 175,
        "head_sha": "bbbbbbbb",
        "verdict": "approve",
        "merge_permission": "autonomous_merge_permitted",
        "not_operator_approval": True,
    }
    artifact.update(overrides)
    return artifact


def approve_response(artifact=None, *, include_artifact=True):
    body = [
        "1. Evidence Review",
        "The packet provides the task contract and diff.",
        "2. Adversarial Review",
        "Looked for self-approval and identity spoofing; none found.",
        "3. Authority Verdict: approve",
    ]
    if include_artifact:
        body += ["```external-ai-guardian-review", json.dumps(artifact or approval_artifact()), "```"]
    return "\n".join(body)


class PacketSchemaTests(unittest.TestCase):
    def test_built_packet_is_schema_valid(self):
        packet = make_packet()
        validate_json_schema(packet, SCHEMA)  # raises on failure
        self.assertEqual(packet["kind"], "external_ai_guardian_review_packet")
        self.assertIs(packet["not_approval_evidence"], True)
        self.assertEqual(packet["pr_number"], 175)
        self.assertTrue(packet["review_focus_areas"])

    def test_missing_required_field_is_invalid(self):
        packet = make_packet()
        del packet["head_sha"]
        with self.assertRaises(JsonSchemaError):
            validate_json_schema(packet, SCHEMA)

    def test_wrong_kind_const_is_invalid(self):
        packet = make_packet()
        packet["kind"] = "external_ai_guardian_review"  # the APPROVAL kind, not a packet
        with self.assertRaises(JsonSchemaError):
            validate_json_schema(packet, SCHEMA)

    def test_not_approval_evidence_must_be_true(self):
        packet = make_packet()
        packet["not_approval_evidence"] = False
        with self.assertRaises(JsonSchemaError):
            validate_json_schema(packet, SCHEMA)

    def test_bad_risk_enum_is_invalid(self):
        packet = make_packet()
        packet["risk_level"] = "extreme"
        with self.assertRaises(JsonSchemaError):
            validate_json_schema(packet, SCHEMA)

    def test_validate_packet_helper_raises_shikierror(self):
        from shiki_process import ShikiError

        packet = make_packet()
        packet["pr_number"] = "not-an-int"
        with self.assertRaises(ShikiError):
            gr.validate_packet(packet, SCHEMA)

    def test_known_missing_evidence_flags_absent_inputs(self):
        packet = make_packet(pr_data={"check_results": [], "diff_summary": ""})
        joined = " ".join(packet["known_missing_evidence"]).lower()
        self.assertIn("check results", joined)
        self.assertIn("diff", joined)


class FocusClassifierTests(unittest.TestCase):
    def test_guardian_authority_category(self):
        focus = gr.classify_review_focus(["scripts/shiki_guardian.py"], "guardian change", "high")
        self.assertTrue(any("Guardian/MergeGate/CCA authority" in f for f in focus))

    def test_github_permissions_category(self):
        focus = gr.classify_review_focus(["scripts/shiki_github.py"], "workflow permission can_approve", "medium")
        self.assertTrue(any("permission" in f.lower() for f in focus))

    def test_runtime_adapter_category(self):
        focus = gr.classify_review_focus(["scripts/shiki_runtime_adapters.py"], "runner adapter", "medium")
        self.assertTrue(any("role boundary" in f.lower() for f in focus))

    def test_memory_loop_category(self):
        focus = gr.classify_review_focus(["scripts/shiki_memory.py"], "memory loop", "low")
        self.assertTrue(any("Memory Loop" in f for f in focus))

    def test_shiki_state_category(self):
        focus = gr.classify_review_focus([".shiki/schemas/x.schema.json"], "schema", "low")
        self.assertTrue(any("schema" in f.lower() for f in focus))

    def test_workflow_ci_category(self):
        focus = gr.classify_review_focus([".github/workflows/ci.yml"], "ci workflow", "low")
        self.assertTrue(any("CI" in f for f in focus))

    def test_docs_only_is_light_and_not_conservative(self):
        focus = gr.classify_review_focus(["docs/adr/0099-x.md", "README.md"], "", "low")
        self.assertTrue(any("Docs/ADR-only" in f for f in focus))
        self.assertFalse(any("conservative default" in f.lower() for f in focus))

    def test_unknown_category_is_conservative(self):
        focus = gr.classify_review_focus(["src/app/widget.ts"], "tweak widget", "low")
        self.assertTrue(any("Unclassified change" in f for f in focus))
        self.assertTrue(any("conservative default" in f.lower() for f in focus))

    def test_high_risk_always_conservative(self):
        focus = gr.classify_review_focus(["docs/adr/0099-x.md"], "", "critical")
        self.assertTrue(any("conservative default" in f.lower() for f in focus))

    def test_mixed_categories_are_conservative(self):
        focus = gr.classify_review_focus(["scripts/shiki_memory.py", "scripts/shiki_github.py"], "", "low")
        self.assertTrue(any("conservative default" in f.lower() for f in focus))


class PromptBuilderTests(unittest.TestCase):
    def setUp(self):
        self.packet = make_packet()
        self.prompt = gr.build_review_prompt(
            self.packet, reviewer_model="GPT-5.5 Pro", reviewer_role="external_guardian_reviewer"
        )

    def test_three_review_stages_present(self):
        self.assertIn("1. Evidence Review", self.prompt)
        self.assertIn("2. Adversarial Review", self.prompt)
        self.assertIn("3. Authority Verdict", self.prompt)

    def test_three_verdicts_present(self):
        self.assertIn("approve | request_changes | insufficient_evidence", self.prompt)

    def test_reviewer_identity_and_boundary(self):
        self.assertIn("GPT-5.5 Pro acting as external_guardian_reviewer", self.prompt)
        self.assertIn("Do not treat yourself as the implementer.", self.prompt)
        self.assertIn("Do not record your verdict as human/operator approval.", self.prompt)

    def test_connector_is_optional(self):
        self.assertIn("GitHub connector only to verify or challenge", self.prompt)

    def test_artifact_only_on_approve(self):
        self.assertIn("If and only if approve", self.prompt)
        self.assertIn("```external-ai-guardian-review", self.prompt)
        self.assertIn("autonomous_merge_permitted", self.prompt)

    def test_focus_areas_injected(self):
        self.assertIn("PR-specific review focus areas", self.prompt)
        self.assertIn("Guardian/MergeGate/CCA authority", self.prompt)


class ResponseExtractionTests(unittest.TestCase):
    def test_extract_approve_with_artifact(self):
        extracted = gr.extract_review_response(approve_response(), fence="external-ai-guardian-review")
        self.assertEqual(extracted["verdict"], "approve")
        self.assertTrue(extracted["artifact_present"])
        self.assertEqual(extracted["artifact"]["verdict"], "approve")

    def test_extract_request_changes(self):
        text = "3. Authority Verdict: request_changes\nBlocking issues:\n- missing test"
        extracted = gr.extract_review_response(text, fence="external-ai-guardian-review")
        self.assertEqual(extracted["verdict"], "request_changes")
        self.assertIsNone(extracted["artifact"])

    def test_extract_insufficient_evidence(self):
        text = "3. Authority Verdict: insufficient_evidence\nMissing evidence:\n- no checks"
        extracted = gr.extract_review_response(text, fence="external-ai-guardian-review")
        self.assertEqual(extracted["verdict"], "insufficient_evidence")


class VerifyResponseTests(unittest.TestCase):
    def setUp(self):
        self.packet = make_packet()

    def verify(self, response_text):
        return gr.verify_review_response(self.packet, response_text, policy=POLICY)

    def test_approve_with_valid_artifact_is_approved(self):
        result = self.verify(approve_response())
        self.assertTrue(result["approved"])
        self.assertEqual(result["route"], "autonomous_merge")
        self.assertEqual(result["reviewer_model"], "GPT-5.5 Pro")
        self.assertEqual(result["reasons"], [])

    def test_approve_without_artifact_is_rejected(self):
        result = self.verify(approve_response(include_artifact=False))
        self.assertFalse(result["approved"])
        self.assertTrue(result["reasons"])

    def test_wrong_head_sha_is_rejected(self):
        result = self.verify(approve_response(approval_artifact(head_sha="cccccccc")))
        self.assertFalse(result["approved"])
        self.assertTrue(any("head SHA" in r for r in result["reasons"]))

    def test_wrong_repo_is_rejected(self):
        result = self.verify(approve_response(approval_artifact(repo="evil/repo")))
        self.assertFalse(result["approved"])
        self.assertTrue(any("repository" in r for r in result["reasons"]))

    def test_wrong_pr_is_rejected(self):
        result = self.verify(approve_response(approval_artifact(pr=999)))
        self.assertFalse(result["approved"])
        self.assertTrue(any("PR" in r for r in result["reasons"]))

    def test_missing_not_operator_approval_is_rejected(self):
        artifact = approval_artifact()
        del artifact["not_operator_approval"]
        result = self.verify(approve_response(artifact))
        self.assertFalse(result["approved"])
        self.assertTrue(any("not_operator_approval" in r for r in result["reasons"]))

    def test_disallowed_model_is_rejected(self):
        result = self.verify(approve_response(approval_artifact(reviewer={"type": "ai_model", "model": "GPT-4", "role": "external_guardian_reviewer"})))
        self.assertFalse(result["approved"])
        self.assertTrue(any("model" in r for r in result["reasons"]))

    def test_disallowed_role_is_rejected(self):
        result = self.verify(approve_response(approval_artifact(reviewer={"type": "ai_model", "model": "GPT-5.5 Pro", "role": "implementer"})))
        self.assertFalse(result["approved"])
        self.assertTrue(any("role" in r for r in result["reasons"]))

    def test_request_changes_routes_to_repair(self):
        result = self.verify("3. Authority Verdict: request_changes\n- needs a test")
        self.assertFalse(result["approved"])
        self.assertEqual(result["route"], "repair_packet")

    def test_insufficient_evidence_routes_to_evidence(self):
        result = self.verify("3. Authority Verdict: insufficient_evidence\n- no check results")
        self.assertFalse(result["approved"])
        self.assertEqual(result["route"], "evidence_packet")

    def test_no_verdict_is_rejected(self):
        result = self.verify("Some prose without any authority verdict line.")
        self.assertFalse(result["approved"])
        self.assertEqual(result["route"], "rejected")


if __name__ == "__main__":
    unittest.main()
