#!/usr/bin/env python3
"""Dependency-free validation for Shiki mirror artifacts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SHIKI = ROOT / ".shiki"

TASK_ID = re.compile(r"^T-[0-9]{4,}$")
GOAL_ID = re.compile(r"^G-[0-9]{4,}$")
LEDGER_ID = re.compile(r"^L-[0-9]{4,}$")
PLAN_ID = re.compile(r"^P-[0-9]{4,}$")
RUN_ID = re.compile(r"^RUN-[0-9]{4,}$")
INBOX_ID = re.compile(r"^INBOX-[0-9]{4,}$")
EXEC_ID = re.compile(r"^EXEC-[0-9]{4,}$")
SMOKE_ID = re.compile(r"^SMOKE-[0-9]{4,}$")
START_ID = re.compile(r"^START-[0-9]{4,}$")

TASK_REQUIRED = {
    "id",
    "goal_id",
    "title",
    "scope",
    "non_goals",
    "dependencies",
    "locks",
    "assigned_runtime",
    "risk_level",
    "acceptance_checks",
    "expected_branch",
    "ledger_evidence",
}

GOAL_REQUIRED = {
    "id",
    "title",
    "outcome",
    "completion_conditions",
    "non_goals",
    "risk_level",
    "required_skills",
    "acceptance_evidence",
}
DAG_REQUIRED = {"goal_id", "nodes", "edges"}
LEDGER_REQUIRED = {"id", "timestamp", "goal_id", "type", "actor", "summary", "evidence"}
PLAN_REQUIRED = {"id", "title", "outcome", "grill_with_docs", "tasks"}
RUN_REQUIRED = {
    "id",
    "plan_id",
    "goal_id",
    "task_ids",
    "dispatchable_task_ids",
    "blocked_task_ids",
    "dag",
    "worktrees",
    "created_at",
}
RUNNER_REQUIRED = {"id", "task_id", "goal_id", "command", "returncode", "stdout", "stderr", "created_at"}
SMOKE_REQUIRED = {"id", "repo", "dry_run", "execute_github", "created_at"}
START_REQUIRED = {
    "id",
    "repo",
    "project_name",
    "skills_dir",
    "questions",
    "plan_id",
    "goal_id",
    "run_id",
    "dispatchable_task_ids",
    "issues",
    "handoffs",
    "created_at",
}

RUNTIMES = {
    "codex",
    "codex-front",
    "claude-code",
    "claude-code-action",
    "github-cca",
    "github-actions",
    "hermes-runner",
    "human",
    "other",
}
RISK_LEVELS = {"low", "medium", "high", "critical"}
GOAL_STATUSES = {"planned", "ready", "blocked", "complete"}
TASK_STATUSES = {"planned", "ready", "running", "blocked", "review", "repair-needed", "done"}
LEDGER_TYPES = {
    "goal-created",
    "context-impact",
    "task-registered",
    "lock",
    "check",
    "review",
    "cca-verdict",
    "repair",
    "mergegate",
    "completion",
    "handoff",
}
KNOWN_SKILLS = {
    "setup-matt-pocock-skills",
    "grill-with-docs",
    "zoom-out",
    "to-prd",
    "to-issues",
    "triage",
    "tdd",
    "diagnose",
    "improve-codebase-architecture",
    "prototype",
    "evidence-only",
    "none",
    "shiki",
}


class ValidationError(Exception):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValidationError(f"{path}: invalid JSON: {error}") from error


def require_keys(path: Path, data: dict[str, Any], keys: set[str]) -> None:
    missing = sorted(keys - set(data))
    if missing:
        raise ValidationError(f"{path}: missing required keys: {', '.join(missing)}")


def require_list(path: Path, data: dict[str, Any], key: str, *, non_empty: bool = False) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValidationError(f"{path}: {key} must be a list")
    if non_empty and not value:
        raise ValidationError(f"{path}: {key} must not be empty")
    return value


def require_string(path: Path, data: dict[str, Any], key: str, *, non_empty: bool = True) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValidationError(f"{path}: {key} must be a string")
    if non_empty and not value.strip():
        raise ValidationError(f"{path}: {key} must not be empty")
    return value


def validate_skill_names(path: Path, skills: list[Any], *, key: str) -> None:
    for skill in skills:
        if not isinstance(skill, str) or not skill.strip():
            raise ValidationError(f"{path}: {key} must contain non-empty strings")
        if skill not in KNOWN_SKILLS:
            raise ValidationError(f"{path}: unknown required skill {skill!r}")


def validate_goal(path: Path, data: dict[str, Any]) -> str:
    require_keys(path, data, GOAL_REQUIRED)

    goal_id = require_string(path, data, "id")
    if not GOAL_ID.match(goal_id):
        raise ValidationError(f"{path}: id must match G-0001 style")

    require_string(path, data, "title")
    require_string(path, data, "outcome")
    require_list(path, data, "completion_conditions", non_empty=True)
    require_list(path, data, "non_goals")
    skills = require_list(path, data, "required_skills")
    validate_skill_names(path, skills, key="required_skills")
    require_list(path, data, "acceptance_evidence", non_empty=True)

    risk_level = require_string(path, data, "risk_level")
    if risk_level not in RISK_LEVELS:
        raise ValidationError(f"{path}: risk_level must be one of {sorted(RISK_LEVELS)}")

    status = data.get("status")
    if status is not None and status not in GOAL_STATUSES:
        raise ValidationError(f"{path}: status must be one of {sorted(GOAL_STATUSES)}")

    return goal_id


def validate_task(path: Path, data: dict[str, Any]) -> tuple[str, list[str]]:
    require_keys(path, data, TASK_REQUIRED)

    task_id = require_string(path, data, "id")
    if not TASK_ID.match(task_id):
        raise ValidationError(f"{path}: id must match T-0001 style")

    goal_id = require_string(path, data, "goal_id")
    if not GOAL_ID.match(goal_id):
        raise ValidationError(f"{path}: goal_id must match G-0001 style")

    require_string(path, data, "title")
    require_string(path, data, "scope")
    require_string(path, data, "expected_branch")

    require_list(path, data, "non_goals")
    dependencies = require_list(path, data, "dependencies")
    require_list(path, data, "locks")
    require_list(path, data, "acceptance_checks", non_empty=True)
    require_list(path, data, "ledger_evidence", non_empty=True)

    runtime = require_string(path, data, "assigned_runtime")
    if runtime not in RUNTIMES:
        raise ValidationError(f"{path}: assigned_runtime must be one of {sorted(RUNTIMES)}")

    risk_level = require_string(path, data, "risk_level")
    if risk_level not in RISK_LEVELS:
        raise ValidationError(f"{path}: risk_level must be one of {sorted(RISK_LEVELS)}")

    status = data.get("status")
    if status is not None and status not in TASK_STATUSES:
        raise ValidationError(f"{path}: status must be one of {sorted(TASK_STATUSES)}")

    for dependency in dependencies:
        if not isinstance(dependency, str) or not TASK_ID.match(dependency):
            raise ValidationError(f"{path}: dependencies must contain T-0001 style ids")

    validate_skill_names(path, require_list(path, data, "required_skills"), key="required_skills")

    return task_id, dependencies


def validate_dag(path: Path, data: dict[str, Any], known_tasks: set[str]) -> None:
    require_keys(path, data, DAG_REQUIRED)
    goal_id = require_string(path, data, "goal_id")
    if not GOAL_ID.match(goal_id):
        raise ValidationError(f"{path}: goal_id must match G-0001 style")

    nodes = require_list(path, data, "nodes", non_empty=True)
    edges = require_list(path, data, "edges")

    for node in nodes:
        if not isinstance(node, str) or not TASK_ID.match(node):
            raise ValidationError(f"{path}: nodes must contain T-0001 style ids")
        if known_tasks and node not in known_tasks:
            raise ValidationError(f"{path}: node {node} has no matching task file")

    node_set = set(nodes)
    adjacency: dict[str, list[str]] = {node: [] for node in node_set}

    for edge in edges:
        if not isinstance(edge, dict):
            raise ValidationError(f"{path}: edges must contain objects")
        from_id = edge.get("from")
        to_id = edge.get("to")
        if from_id not in node_set or to_id not in node_set:
            raise ValidationError(f"{path}: edge {from_id!r}->{to_id!r} references unknown node")
        adjacency[from_id].append(to_id)

    detect_cycles(path, adjacency)


def detect_cycles(path: Path, adjacency: dict[str, list[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in visiting:
            cycle = " -> ".join(stack + [node])
            raise ValidationError(f"{path}: DAG cycle detected: {cycle}")
        if node in visited:
            return
        visiting.add(node)
        for child in adjacency.get(node, []):
            visit(child, stack + [node])
        visiting.remove(node)
        visited.add(node)

    for node in adjacency:
        visit(node, [])


def validate_ledger(path: Path, data: dict[str, Any], known_tasks: set[str], known_goals: set[str]) -> None:
    require_keys(path, data, LEDGER_REQUIRED)

    ledger_id = require_string(path, data, "id")
    if not LEDGER_ID.match(ledger_id):
        raise ValidationError(f"{path}: id must match L-0001 style")

    goal_id = require_string(path, data, "goal_id")
    if not GOAL_ID.match(goal_id):
        raise ValidationError(f"{path}: goal_id must match G-0001 style")
    if known_goals and goal_id not in known_goals:
        raise ValidationError(f"{path}: goal_id {goal_id} has no matching goal file")

    task_id = data.get("task_id")
    if task_id is not None:
        if not isinstance(task_id, str) or not TASK_ID.match(task_id):
            raise ValidationError(f"{path}: task_id must match T-0001 style or null")
        if known_tasks and task_id not in known_tasks:
            raise ValidationError(f"{path}: task_id {task_id} has no matching task file")

    ledger_type = require_string(path, data, "type")
    if ledger_type not in LEDGER_TYPES:
        raise ValidationError(f"{path}: type must be one of {sorted(LEDGER_TYPES)}")

    require_string(path, data, "timestamp")
    require_string(path, data, "actor")
    require_string(path, data, "summary")
    require_list(path, data, "evidence", non_empty=True)


def validate_worktree(path: Path, data: dict[str, Any], known_tasks: set[str]) -> None:
    required = {
        "task_id",
        "goal_id",
        "branch",
        "path",
        "runtime",
        "state",
        "locks",
        "created_by",
        "created_at",
        "pr",
    }
    require_keys(path, data, required)

    task_id = require_string(path, data, "task_id")
    if not TASK_ID.match(task_id):
        raise ValidationError(f"{path}: task_id must match T-0001 style")
    if known_tasks and task_id not in known_tasks:
        raise ValidationError(f"{path}: task_id {task_id} has no matching task file")

    goal_id = require_string(path, data, "goal_id")
    if not GOAL_ID.match(goal_id):
        raise ValidationError(f"{path}: goal_id must match G-0001 style")

    require_string(path, data, "branch")
    require_string(path, data, "path")
    require_string(path, data, "runtime")
    require_string(path, data, "state")
    require_string(path, data, "created_by")
    require_string(path, data, "created_at")
    require_list(path, data, "locks")


def validate_plan(path: Path, data: dict[str, Any]) -> None:
    require_keys(path, data, PLAN_REQUIRED)
    plan_id = require_string(path, data, "id")
    if not PLAN_ID.match(plan_id):
        raise ValidationError(f"{path}: id must match P-0001 style")
    require_string(path, data, "title")
    require_string(path, data, "outcome")

    grill = data.get("grill_with_docs")
    if not isinstance(grill, dict):
        raise ValidationError(f"{path}: grill_with_docs must be an object")
    if grill.get("status") != "complete":
        raise ValidationError(f"{path}: grill_with_docs.status must be complete")

    tasks = require_list(path, data, "tasks", non_empty=True)
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise ValidationError(f"{path}: tasks[{index}] must be an object")
        for key in ("title", "scope", "acceptance_checks"):
            if key not in task:
                raise ValidationError(f"{path}: tasks[{index}] missing {key}")
        if not isinstance(task.get("acceptance_checks"), list) or not task["acceptance_checks"]:
            raise ValidationError(f"{path}: tasks[{index}].acceptance_checks must not be empty")


def validate_run(path: Path, data: dict[str, Any], known_tasks: set[str]) -> None:
    require_keys(path, data, RUN_REQUIRED)
    run_id = require_string(path, data, "id")
    if not RUN_ID.match(run_id):
        raise ValidationError(f"{path}: id must match RUN-0001 style")
    plan_id = require_string(path, data, "plan_id")
    if not PLAN_ID.match(plan_id):
        raise ValidationError(f"{path}: plan_id must match P-0001 style")
    goal_id = require_string(path, data, "goal_id")
    if not GOAL_ID.match(goal_id):
        raise ValidationError(f"{path}: goal_id must match G-0001 style")
    for key in ("task_ids", "dispatchable_task_ids"):
        for task_id in require_list(path, data, key):
            if not isinstance(task_id, str) or not TASK_ID.match(task_id):
                raise ValidationError(f"{path}: {key} must contain T-0001 style ids")
            if known_tasks and task_id not in known_tasks:
                raise ValidationError(f"{path}: {key} references unknown task {task_id}")
    if not isinstance(data.get("blocked_task_ids"), dict):
        raise ValidationError(f"{path}: blocked_task_ids must be an object")
    require_string(path, data, "dag")
    require_string(path, data, "created_at")
    require_list(path, data, "worktrees")


def validate_runner_record(path: Path, data: dict[str, Any], known_tasks: set[str]) -> None:
    require_keys(path, data, RUNNER_REQUIRED)
    exec_id = require_string(path, data, "id")
    if not EXEC_ID.match(exec_id):
        raise ValidationError(f"{path}: id must match EXEC-0001 style")
    task_id = require_string(path, data, "task_id")
    if not TASK_ID.match(task_id):
        raise ValidationError(f"{path}: task_id must match T-0001 style")
    if known_tasks and task_id not in known_tasks:
        raise ValidationError(f"{path}: task_id {task_id} has no matching task file")
    goal_id = require_string(path, data, "goal_id")
    if not GOAL_ID.match(goal_id):
        raise ValidationError(f"{path}: goal_id must match G-0001 style")
    require_string(path, data, "command")
    if not isinstance(data.get("returncode"), int):
        raise ValidationError(f"{path}: returncode must be an integer")
    require_string(path, data, "stdout", non_empty=False)
    require_string(path, data, "stderr", non_empty=False)
    require_string(path, data, "created_at")


def validate_smoke(path: Path, data: dict[str, Any]) -> None:
    require_keys(path, data, SMOKE_REQUIRED)
    smoke_id = require_string(path, data, "id")
    if not SMOKE_ID.match(smoke_id):
        raise ValidationError(f"{path}: id must match SMOKE-0001 style")
    require_string(path, data, "repo")
    if not isinstance(data.get("dry_run"), bool):
        raise ValidationError(f"{path}: dry_run must be a boolean")
    if not isinstance(data.get("execute_github"), bool):
        raise ValidationError(f"{path}: execute_github must be a boolean")
    require_string(path, data, "created_at")


def validate_start(path: Path, data: dict[str, Any], known_tasks: set[str]) -> None:
    require_keys(path, data, START_REQUIRED)
    start_id = require_string(path, data, "id")
    if not START_ID.match(start_id):
        raise ValidationError(f"{path}: id must match START-0001 style")
    require_string(path, data, "repo")
    require_string(path, data, "project_name")
    require_string(path, data, "skills_dir")
    questions = require_list(path, data, "questions")
    if not questions or not all(isinstance(question, str) and question for question in questions):
        raise ValidationError(f"{path}: questions must be a non-empty list of strings")
    plan_id = require_string(path, data, "plan_id")
    if not PLAN_ID.match(plan_id):
        raise ValidationError(f"{path}: plan_id must match P-0001 style")
    goal_id = require_string(path, data, "goal_id")
    if not GOAL_ID.match(goal_id):
        raise ValidationError(f"{path}: goal_id must match G-0001 style")
    run_id = require_string(path, data, "run_id")
    if not RUN_ID.match(run_id):
        raise ValidationError(f"{path}: run_id must match RUN-0001 style")
    for task_id in require_list(path, data, "dispatchable_task_ids"):
        if not isinstance(task_id, str) or not TASK_ID.match(task_id):
            raise ValidationError(f"{path}: dispatchable_task_ids must contain T-0001 style ids")
        if known_tasks and task_id not in known_tasks:
            raise ValidationError(f"{path}: dispatchable task {task_id} has no matching task file")
    require_list(path, data, "issues")
    require_list(path, data, "handoffs")
    require_string(path, data, "created_at")


def json_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def require_contiguous_ids(paths: list[Path], prefix: str) -> None:
    if not paths:
        return
    numbers: list[int] = []
    for path in paths:
        match = re.match(rf"^{re.escape(prefix)}-([0-9]{{4,}})\.json$", path.name)
        if match:
            numbers.append(int(match.group(1)))
    if not numbers:
        return
    expected = list(range(1, max(numbers) + 1))
    if sorted(numbers) != expected:
        missing = sorted(set(expected) - set(numbers))
        raise ValidationError(f"{paths[0].parent}: missing contiguous {prefix} ids: {missing}")


def allowed_labels_from_docs() -> set[str]:
    labels_path = ROOT / "docs" / "agents" / "triage-labels.md"
    labels: set[str] = set()
    if not labels_path.exists():
        raise ValidationError(f"{labels_path}: label vocabulary file is missing")
    for match in re.finditer(r"`([^`]+)`", labels_path.read_text(encoding="utf-8")):
        label = match.group(1).strip()
        if ":" in label:
            labels.add(label)
    return labels


def issue_form_labels(path: Path, text: str) -> list[str]:
    labels: list[str] = []
    lines = text.splitlines()
    for index, raw_line in enumerate(lines):
        if not raw_line.startswith("labels:"):
            continue
        value = raw_line.split(":", 1)[1].strip()
        if value.startswith("[") and value.endswith("]"):
            return [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]
        for follow in lines[index + 1 :]:
            if follow.startswith("  - "):
                labels.append(follow.strip()[2:].strip().strip("\"'"))
                continue
            if follow and not follow.startswith(" "):
                break
        return labels
    raise ValidationError(f"{path}: labels field is missing")


def validate_issue_forms() -> None:
    allowed_labels = allowed_labels_from_docs()
    for path in sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^about:", text, re.MULTILINE):
            raise ValidationError(f"{path}: GitHub Issue Forms use description:, not about:")
        for key in ("name:", "description:", "title:", "labels:", "body:"):
            if not re.search(rf"^{re.escape(key)}", text, re.MULTILINE):
                raise ValidationError(f"{path}: missing top-level {key}")
        for label in issue_form_labels(path, text):
            if label not in allowed_labels:
                raise ValidationError(f"{path}: label {label!r} is not declared in docs/agents/triage-labels.md")


def validate_prompt_contract_paths() -> None:
    wrong_path = ".shiki/templates/cca-verdict.schema.json"
    for base in (ROOT / ".github", ROOT / "docs"):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".yml", ".yaml"}:
                if wrong_path in path.read_text(encoding="utf-8"):
                    raise ValidationError(f"{path}: references obsolete CCA schema path {wrong_path}")


def validate_orchestrator_security() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "shiki-orchestrator.yml"
    text = workflow_path.read_text(encoding="utf-8")
    if "issue_comment:" not in text:
        return
    if "author_association" not in text:
        raise ValidationError(f"{workflow_path}: issue_comment trigger must gate /shiki by commenter author_association")
    for association in ("OWNER", "MEMBER", "COLLABORATOR"):
        if association not in text:
            raise ValidationError(f"{workflow_path}: missing trusted author_association {association}")


def validate_contract_schema_consistency() -> None:
    cca_schema_path = SHIKI / "schemas" / "cca-verdict.schema.json"
    cca_schema = load_json(cca_schema_path)
    if not isinstance(cca_schema, dict):
        raise ValidationError(f"{cca_schema_path}: schema must be a JSON object")
    cca_required = set(cca_schema.get("required", []))
    for field in ("checklist", "acceptance", "mergegate"):
        if field not in cca_required:
            raise ValidationError(f"{cca_schema_path}: {field} must be required by the CCA verdict contract")
    repair_packet = cca_schema.get("properties", {}).get("repair_packet", {})
    allowed_repair_types = repair_packet.get("type")
    if isinstance(allowed_repair_types, str):
        allowed_repair_types = [allowed_repair_types]
    if not isinstance(allowed_repair_types, list) or not {"object", "null"}.issubset(set(allowed_repair_types)):
        raise ValidationError(f"{cca_schema_path}: repair_packet must allow object and null")

    repair_schema_path = SHIKI / "schemas" / "repair-packet.schema.json"
    repair_schema = load_json(repair_schema_path)
    if not isinstance(repair_schema, dict):
        raise ValidationError(f"{repair_schema_path}: schema must be a JSON object")
    skill_enum = repair_schema.get("properties", {}).get("required_skill", {}).get("enum", [])
    if "evidence-only" not in skill_enum:
        raise ValidationError(f"{repair_schema_path}: required_skill enum must include evidence-only")


def main() -> int:
    errors: list[str] = []
    task_dependencies: dict[str, list[str]] = {}
    known_goals: set[str] = set()

    try:
        for schema in json_files(SHIKI / "schemas"):
            load_json(schema)
        validate_contract_schema_consistency()
        validate_prompt_contract_paths()
        validate_issue_forms()
        validate_orchestrator_security()

        goal_paths = json_files(SHIKI / "goals")
        task_paths = json_files(SHIKI / "tasks")
        ledger_paths = json_files(SHIKI / "ledger")
        dag_paths = json_files(SHIKI / "dag")
        report_paths = json_files(SHIKI / "reports")

        require_contiguous_ids(goal_paths, "G")
        require_contiguous_ids(task_paths, "T")
        require_contiguous_ids(ledger_paths, "L")
        require_contiguous_ids(dag_paths, "G")
        require_contiguous_ids(report_paths, "R")

        for goal_path in goal_paths:
            data = load_json(goal_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{goal_path}: goal must be a JSON object")
            goal_id = validate_goal(goal_path, data)
            if goal_id in known_goals:
                raise ValidationError(f"{goal_path}: duplicate goal id {goal_id}")
            if goal_path.name != f"{goal_id}.json":
                raise ValidationError(f"{goal_path}: file name must match goal id {goal_id}")
            known_goals.add(goal_id)

        for task_path in task_paths:
            data = load_json(task_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{task_path}: task must be a JSON object")
            task_id, dependencies = validate_task(task_path, data)
            if task_id in task_dependencies:
                raise ValidationError(f"{task_path}: duplicate task id {task_id}")
            if task_path.name != f"{task_id}.json":
                raise ValidationError(f"{task_path}: file name must match task id {task_id}")
            goal_id = str(data.get("goal_id") or "")
            if goal_id not in known_goals:
                raise ValidationError(f"{task_path}: goal_id {goal_id} has no matching goal file")
            task_dependencies[task_id] = dependencies

        known_tasks = set(task_dependencies)
        for task_id, dependencies in task_dependencies.items():
            for dependency in dependencies:
                if dependency not in known_tasks:
                    raise ValidationError(f".shiki/tasks: {task_id} depends on unknown {dependency}")

        detect_cycles(Path(".shiki/tasks"), {task_id: deps for task_id, deps in task_dependencies.items()})

        for dag_path in dag_paths:
            data = load_json(dag_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{dag_path}: DAG must be a JSON object")
            validate_dag(dag_path, data, known_tasks)
            goal_id = str(data.get("goal_id") or "")
            if goal_id not in known_goals:
                raise ValidationError(f"{dag_path}: goal_id {goal_id} has no matching goal file")
            if dag_path.name != f"{goal_id}.json":
                raise ValidationError(f"{dag_path}: file name must match DAG goal_id {goal_id}")

        for plan_path in json_files(SHIKI / "plans"):
            data = load_json(plan_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{plan_path}: plan must be a JSON object")
            validate_plan(plan_path, data)

        for ledger_path in ledger_paths:
            data = load_json(ledger_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{ledger_path}: ledger entry must be a JSON object")
            validate_ledger(ledger_path, data, known_tasks, known_goals)

        for worktree_path in json_files(SHIKI / "worktrees"):
            data = load_json(worktree_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{worktree_path}: worktree record must be a JSON object")
            validate_worktree(worktree_path, data, known_tasks)

        for run_path in json_files(SHIKI / "runs"):
            data = load_json(run_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{run_path}: run must be a JSON object")
            validate_run(run_path, data, known_tasks)

        for runner_path in json_files(SHIKI / "runner"):
            data = load_json(runner_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{runner_path}: runner record must be a JSON object")
            validate_runner_record(runner_path, data, known_tasks)

        for smoke_path in json_files(SHIKI / "smoke"):
            data = load_json(smoke_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{smoke_path}: smoke record must be a JSON object")
            validate_smoke(smoke_path, data)

        for start_path in json_files(SHIKI / "starts"):
            data = load_json(start_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{start_path}: start record must be a JSON object")
            validate_start(start_path, data, known_tasks)

    except ValidationError as error:
        errors.append(str(error))

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Shiki validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
