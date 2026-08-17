"""Workflow contracts that prevent redundant CCA and MergeGate runs."""

from __future__ import annotations

import unittest
from pathlib import Path

import shiki_test_support  # noqa: F401  (path bootstrap)

from shiki_workflows import load_workflow_model, workflow_jobs


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SUPERSEDED_RUN_WORKFLOWS = (
    "shiki-cca-completion.yml",
    "shiki-mergegate.yml",
    "shiki-validate.yml",
    "shiki-claude-review.yml",
    "shiki-orchestrator.yml",
)
LABEL_FILTERED_JOBS = {
    "shiki-cca-completion.yml": "cca",
    "shiki-mergegate.yml": "mergegate",
}
MERGEGATE_MODE_LABELS = (
    "mergegate:amendment",
    "mergegate:contract",
    "mergegate:goal_reconcile",
    "mergegate:migration",
    "mergegate:post_merge_reconcile",
)


def _job_if_admits_label(expression: str, label: str, *, action: str = "labeled") -> bool:
    """Evaluate the bounded GitHub-expression subset used by these two jobs."""
    translated = expression
    values = {
        "github.event.pull_request.draft": False,
        "github.event.label.name": label,
        "github.event.action": action,
        "github.event_name": "pull_request",
    }
    for variable, value in values.items():
        translated = translated.replace(variable, repr(value))
    translated = translated.replace("startsWith", "starts_with")
    translated = translated.replace(" false", " False")
    translated = translated.replace(" true", " True")
    translated = translated.replace("&&", " and ").replace("||", " or ")
    return bool(eval(translated, {"__builtins__": {}, "starts_with": str.startswith}, {}))


class WorkflowConcurrencyTests(unittest.TestCase):
    def test_each_workflow_cancels_superseded_runs_per_pull_request(self) -> None:
        for filename in SUPERSEDED_RUN_WORKFLOWS:
            with self.subTest(workflow=filename):
                model = load_workflow_model(WORKFLOWS / filename)
                concurrency = model.get("concurrency")
                self.assertIsInstance(concurrency, dict)
                self.assertIs(concurrency.get("cancel-in-progress"), True)
                group = concurrency.get("group")
                self.assertIsInstance(group, str)
                self.assertIn("github.workflow", group)
                self.assertIn("github.event.pull_request.number", group)


class LabelFilteringTests(unittest.TestCase):
    def test_only_verdict_changing_labels_admit_label_events(self) -> None:
        for filename, job_id in LABEL_FILTERED_JOBS.items():
            model = load_workflow_model(WORKFLOWS / filename)
            job = workflow_jobs(model)[job_id]
            expression = job.get("if")
            self.assertIsInstance(expression, str)

            for action in ("labeled", "unlabeled"):
                with self.subTest(workflow=filename, action=action, label="guardian:approved"):
                    self.assertTrue(_job_if_admits_label(expression, "guardian:approved", action=action))
                for label in MERGEGATE_MODE_LABELS:
                    with self.subTest(workflow=filename, action=action, label=label):
                        self.assertTrue(_job_if_admits_label(expression, label, action=action))
                with self.subTest(workflow=filename, action=action, label="documentation"):
                    self.assertFalse(_job_if_admits_label(expression, "documentation", action=action))


class PostingStepSafetyTests(unittest.TestCase):
    def test_steps_that_post_reviews_comments_or_labels_never_run_always(self) -> None:
        mutation_markers = (
            "gh pr comment",
            "--add-label",
            "--remove-label",
            "claude-code-action@",
        )
        for filename in SUPERSEDED_RUN_WORKFLOWS:
            model = load_workflow_model(WORKFLOWS / filename)
            for job_id, job in workflow_jobs(model).items():
                for step in job.get("steps", []):
                    run = str(step.get("run", ""))
                    uses = str(step.get("uses", ""))
                    posts_review = (
                        "/pulls/${PR_NUMBER}/reviews" in run
                        and ("-X POST" in run or "--method POST" in run)
                    )
                    patches_comment = "/issues/comments/" in run and "--method PATCH" in run
                    posts = posts_review or patches_comment or any(
                        marker in run or marker in uses for marker in mutation_markers
                    )
                    if posts:
                        with self.subTest(workflow=filename, job=job_id, step=step.get("name")):
                            condition = str(step.get("if", "")).replace("${{", "").replace("}}", "").strip()
                            self.assertNotEqual(condition, "always()")


class SettledCheckGateTests(unittest.TestCase):
    def test_required_checks_settle_before_the_cca_judge_runs(self) -> None:
        model = load_workflow_model(WORKFLOWS / "shiki-cca-completion.yml")
        steps = workflow_jobs(model)["cca"]["steps"]
        names = [step.get("name") for step in steps]
        wait_index = names.index("Wait for required checks to settle")
        judge_index = names.index("Run CCA")
        self.assertLess(wait_index, judge_index)

        script = steps[wait_index]["run"]
        self.assertIn("config_required_checks", script)
        self.assertIn('"CCA verdict"', script)
        self.assertIn('"MergeGate policy check"', script)
        self.assertIn("WAIT_TIMEOUT_SECONDS", script)
        self.assertIn("while true", script)
        self.assertIn("COMPLETED|SUCCESS|FAILURE|ERROR", script)
        self.assertIn("sleep", script)
        self.assertIn("::error::Timed out after", script)
        self.assertIn("headRefOid", script)
        self.assertIn("does not match expected head", script)
        self.assertIn("statusCheckRollup > .shiki/gha/pr.json", script)


if __name__ == "__main__":
    unittest.main()
