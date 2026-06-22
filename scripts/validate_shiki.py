#!/usr/bin/env python3
"""Dependency-free validation for Shiki mirror artifacts."""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from shiki_contracts import (
    CANONICAL_CCA_VERDICT_SCHEMA_PATH,
    CANONICAL_REPAIR_PACKET_SCHEMA_PATH,
    CODEOWNERS_CRITICAL_PATHS,
    CODEOWNERS_PATH,
    CODEOWNERS_REQUIRED_OWNER,
    CONTRACT_SCHEMA_SCAN_PATHS,
    CONTRACT_SOURCE_OF_TRUTH_FILES,
    OBSOLETE_CCA_VERDICT_SCHEMA_PATH,
    OBSOLETE_REPAIR_PACKET_SCHEMA_PATH,
    RUNTIME_NAMES,
    TARGET_STATE_DIRECTORIES,
    canonical_source_of_truth_markdown,
)
from shiki_evidence import (
    CCA_EVIDENCE_ARTIFACT_NAME,
    CCA_EVIDENCE_MANIFEST_PATH,
    REQUIRED_CCA_EVIDENCE_FILES,
    ledger_entry_digest,
    validate_ledger_integrity,
)
from shiki_locks import known_shiki_semantic_locks, normalize_lock
from shiki_jsonschema import UnsupportedJsonSchemaError, assert_supported_schema, validate_json_schema
from shiki_memory import memory_entry_errors
from shiki_installer import TEMPLATE_PATHS
from shiki_guardian import GUARDIAN_POLICY_PATH, GuardianPolicyError, load_guardian_policy, validate_guardian_policy
from shiki_manifest import (
    MANIFEST_PATH,
    README_LAYOUT_END,
    README_LAYOUT_START,
    ManifestError,
    load_manifest,
    manifest_create_directories,
    manifest_directories,
    manifest_exclude_from_commit,
    manifest_install_include,
    manifest_required_directories,
    manifest_required_files,
    manifest_runtime_directories,
    render_manifest_layout,
)
from shiki_migrations import (
    BASELINE_MIGRATION_ID,
    MIGRATION_STATE_PATH,
    STATE_CLASSES_MIGRATION_ID,
    load_migration_state,
    migration_status,
    validate_migration_registry,
    validate_migration_state_data,
)
from shiki_state_classes import (
    UNKNOWN_STATE_CLASS,
    class_policy,
    classify_shiki_path,
    manifest_state_class_policies,
    manifest_state_classes,
    state_class_summary,
    unknown_tracked_shiki_paths,
)
from shiki_provider import (
    DEFAULT_GITHUB_HOST,
    ProviderConfigError,
    api_base_url_for_host,
    canonical_remote_url,
    provider_from_repo_json,
    provider_from_values,
)
from shiki_runtime_registry import (
    CONFIG_RUNTIME_ROLES,
    RuntimeRegistryError,
    TASK_RUNTIME_ROLES,
    get_runtime,
    runtime_names,
    runtime_registry,
    validate_runtime_role_assignment,
)
from shiki_workflows import (
    WorkflowParseError,
    load_workflow_model,
    load_yaml_model,
    workflow_job_display_names,
    workflow_job_permissions,
    workflow_jobs,
    workflow_name,
    workflow_permissions,
    workflow_step_runs,
    workflow_top_env,
    workflow_triggers,
    workflow_uses_actions,
)


ROOT = Path(__file__).resolve().parents[1]
SHIKI = ROOT / ".shiki"

ID_SUFFIX = r"(?:[0-9]{4,}|[0-9]{8}T[0-9]{12}Z-[0-9a-f]{8})"


def control_id_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}-{ID_SUFFIX}$")


TASK_ID = control_id_pattern("T")
GOAL_ID = control_id_pattern("G")
LEDGER_ID = control_id_pattern("L")
PLAN_ID = control_id_pattern("P")
RUN_ID = control_id_pattern("RUN")
INBOX_ID = control_id_pattern("INBOX")
EXEC_ID = control_id_pattern("EXEC")
SMOKE_ID = control_id_pattern("SMOKE")
START_ID = control_id_pattern("START")
REPORT_ID = control_id_pattern("R")
MEMORY_ID = control_id_pattern("MEM")

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
    "status",
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

RUNTIMES = set(runtime_names())
RISK_LEVELS = {"low", "medium", "high", "critical"}
GOAL_STATUSES = {"planned", "ready", "blocked", "complete", "archived", "historical"}
TASK_STATUSES = {"planned", "ready", "running", "blocked", "review", "repair-needed", "done"}
# A DAG node is "terminal" when its work is finished. A goal whose every DAG node
# is terminal must be marked complete; while any node is non-terminal
# (planned/in-progress/blocked or not yet registered) the goal is active. Only
# "done" is a valid task status today (see TASK_STATUSES); cancelled/superseded
# are not in the task schema, so adding them here would be dead code — they
# would be introduced together with the schema in a separate change.
TERMINAL_TASK_STATUSES = {"done"}
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
    "memory-transition",
}
KNOWN_SKILLS = {
    "setup-matt-pocock-skills",
    "grill-with-docs",
    "zoom-out",
    "to-prd",
    "to-issues",
    "triage",
    "tdd",
    "code-review",
    "diagnose",
    "improve-codebase-architecture",
    "prototype",
    "evidence-only",
    "none",
    "shiki",
}
CCA_ITEM_STATUSES = {"pass", "fail", "insufficient_evidence", "not_applicable"}

WORKFLOW_CONTRACTS = {
    "shiki-validate.yml": {
        "name": "Shiki Validate",
        "triggers": {"pull_request", "push", "workflow_dispatch"},
        "permissions": {"contents": "read"},
        "jobs": {"validate": "Validate Shiki mirror"},
    },
    "shiki-cca-completion.yml": {
        "name": "Shiki CCA Completion",
        "triggers": {"pull_request", "workflow_dispatch"},
        "permissions": {
            "contents": "read",
            "pull-requests": "write",
            "issues": "write",
            "checks": "write",
            "actions": "read",
            "id-token": "write",
        },
        "jobs": {"cca": "CCA verdict", "mergegate": "MergeGate policy check"},
    },
    "shiki-mergegate.yml": {
        "name": "Shiki MergeGate",
        "triggers": {"pull_request", "workflow_dispatch"},
        "permissions": {
            "contents": "read",
            "pull-requests": "read",
            "issues": "read",
            "checks": "read",
            "actions": "read",
        },
        "jobs": {"mergegate": "MergeGate metadata check"},
    },
    "shiki-claude-review.yml": {
        "name": "Shiki Claude Review",
        "triggers": {"pull_request", "workflow_dispatch"},
        "permissions": {
            "contents": "read",
            "pull-requests": "write",
            "issues": "write",
            "id-token": "write",
        },
        "jobs": {"review": "Claude review"},
    },
    "shiki-orchestrator.yml": {
        "name": "Shiki Orchestrator",
        "triggers": {"workflow_dispatch", "issue_comment"},
        "permissions": {"contents": "read", "issues": "read", "pull-requests": "read"},
        "jobs": {"shiki-run": "Shiki orchestrator run", "commit-evidence": "Commit Shiki evidence PR"},
        "job_permissions": {
            "shiki-run": {"contents": "read", "issues": "read", "pull-requests": "read"},
            "commit-evidence": {"contents": "write", "issues": "write", "pull-requests": "write"},
        },
    },
}

NODE24_OFFICIAL_ACTIONS = {
    "actions/checkout": {"v5", "v6"},
    "actions/upload-artifact": {"v6", "v7"},
    "actions/download-artifact": {"v7", "v8"},
}

NODE24_DEFERRED_ACTIONS = {
    ("shiki-cca-completion.yml", "actions/checkout", "v4"),
    ("shiki-cca-completion.yml", "actions/upload-artifact", "v4"),
    ("shiki-cca-completion.yml", "actions/download-artifact", "v4"),
    ("shiki-cca-completion.yml", "anthropics/claude-code-action", "v1"),
    ("shiki-claude-review.yml", "actions/checkout", "v4"),
    ("shiki-claude-review.yml", "anthropics/claude-code-action", "v1"),
}

NODE24_FORCE_WORKFLOWS = {
    "shiki-mergegate.yml",
    "shiki-orchestrator.yml",
    "shiki-validate.yml",
}

SHIKI_CLI_MODULE_FILES = (
    "scripts/shiki.py",
    "scripts/shiki_bootstrap.py",
    "scripts/shiki_cli.py",
    "scripts/shiki_config.py",
    "scripts/shiki_doctor.py",
    "scripts/shiki_git.py",
    "scripts/shiki_github.py",
    "scripts/shiki_guardian.py",
    "scripts/shiki_guardian_review.py",
    "scripts/shiki_installer.py",
    "scripts/shiki_loop.py",
    "scripts/shiki_memory.py",
    "scripts/shiki_migrations.py",
    "scripts/shiki_provider.py",
    "scripts/shiki_process.py",
    "scripts/shiki_runtime.py",
    "scripts/shiki_runtime_adapters.py",
    "scripts/shiki_runtime_registry.py",
    "scripts/shiki_state_classes.py",
    "scripts/shiki_tasks.py",
)
SHIKI_CLI_MODULE_NAMES = tuple(path.removeprefix("scripts/").removesuffix(".py") for path in SHIKI_CLI_MODULE_FILES)


class ValidationError(Exception):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValidationError(f"{path}: invalid JSON: {error}") from error


def require_manifest_path(relative: str, *, field: str) -> None:
    if not relative.startswith(".shiki/"):
        raise ValidationError(f"{MANIFEST_PATH}: {field} path {relative!r} must start with .shiki/")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValidationError(f"{MANIFEST_PATH}: {field} path {relative!r} must stay inside .shiki/")


def git_tracked_paths(root: Path, relative: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", relative],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [path for path in result.stdout.split("\0") if path]


def validate_readme_manifest_layout(root: Path, manifest: dict[str, Any]) -> None:
    readme_path = root / ".shiki" / "README.md"
    text = readme_path.read_text(encoding="utf-8")
    start = text.find(README_LAYOUT_START)
    end = text.find(README_LAYOUT_END)
    if start == -1 or end == -1 or end < start:
        raise ValidationError(f"{readme_path}: missing Shiki manifest layout markers")
    end += len(README_LAYOUT_END)
    actual = text[start:end].strip()
    expected = render_manifest_layout(manifest).strip()
    if actual != expected:
        raise ValidationError(f"{readme_path}: manifest layout block is out of sync with {MANIFEST_PATH}")


def validate_shiki_state_classes(root: Path, manifest: dict[str, Any], directories: dict[str, dict[str, Any]]) -> None:
    state_classes = manifest_state_classes(manifest)
    if not state_classes:
        raise ValidationError(f"{MANIFEST_PATH}: state_classes must be defined")
    policies = manifest_state_class_policies(manifest)
    if not policies:
        raise ValidationError(f"{MANIFEST_PATH}: state_class_policies must be defined")

    for state_class, metadata in state_classes.items():
        if not isinstance(metadata, dict) or not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
            raise ValidationError(f"{MANIFEST_PATH}: state_classes.{state_class} must declare description")
        if state_class not in policies:
            raise ValidationError(f"{MANIFEST_PATH}: state_class {state_class} must have a policy")
        policy = class_policy(state_class, manifest)
        if not isinstance(policy.get("tracked"), bool):
            raise ValidationError(f"{MANIFEST_PATH}: state_class_policies.{state_class}.tracked must be a boolean")
        for field in ("pr_mutation", "trusted_authority"):
            if not isinstance(policy.get(field), str) or not policy[field].strip():
                raise ValidationError(f"{MANIFEST_PATH}: state_class_policies.{state_class}.{field} must be a non-empty string")

    for state_class in policies:
        if state_class not in state_classes:
            raise ValidationError(f"{MANIFEST_PATH}: state_class_policies.{state_class} has no matching state_classes entry")

    for section_name, entries in (("directory", directories), ("file", manifest.get("files") or {})):
        for relative, metadata in entries.items():
            if not isinstance(metadata, dict):
                continue
            state_class = metadata.get("state_class")
            if not isinstance(state_class, str) or not state_class.strip():
                raise ValidationError(f"{MANIFEST_PATH}: {section_name} {relative} must declare state_class")
            if state_class not in state_classes:
                raise ValidationError(f"{MANIFEST_PATH}: {section_name} {relative} uses unknown state_class {state_class!r}")
            policy = class_policy(state_class, manifest)
            if metadata.get("tracked") is True and policy.get("tracked") is False:
                raise ValidationError(f"{MANIFEST_PATH}: {relative} state_class {state_class} must not be tracked")

    expected = {
        ".shiki/gha": "workflow-runtime-evidence",
        ".shiki/ledger": "append-only-evidence",
        ".shiki/goals": "mirror",
        ".shiki/tasks": "mirror",
        ".shiki/locks": "mirror",
        ".shiki/repairs": "mirror",
        ".shiki/reports": "mirror",
        ".shiki/runs": "mirror",
        ".shiki/worktrees": "mirror",
        ".shiki/guardian-policy.json": "governance-policy",
        ".shiki/migrations/state.json": "migration-state",
    }
    for relative, expected_class in expected.items():
        actual = classify_shiki_path(relative, manifest)
        if actual != expected_class:
            raise ValidationError(f"{MANIFEST_PATH}: {relative} must classify as {expected_class}, got {actual}")

    if UNKNOWN_STATE_CLASS in state_class_summary(manifest):
        raise ValidationError(f"{MANIFEST_PATH}: manifest entries must not classify as {UNKNOWN_STATE_CLASS}")

    tracked = git_tracked_paths(root, ".shiki")
    unknown = unknown_tracked_shiki_paths(root, manifest, tracked)
    if unknown:
        raise ValidationError(f"{MANIFEST_PATH}: unknown tracked .shiki paths: {', '.join(unknown)}")

    docs = {
        ".shiki/README.md": list(state_classes),
        "docs/agents/state-classes.md": list(state_classes) + ["workflow-runtime-evidence", "append-only-evidence", "MergeGate"],
        "docs/agents/decision-control.md": ["state classes", "workflow-runtime-evidence"],
        "docs/agents/checklists.md": ["state classes", "append-only-evidence"],
        "docs/agents/shiki-doctor.md": ["doctor.state_classes.manifest", "doctor.state_classes.unknown_paths"],
        "docs/agents/evidence-integrity.md": ["state classes", "append-only-evidence"],
        "docs/agents/shiki-migrations.md": ["M-20260605-0002-state-classes", "state classes"],
    }
    for relative, needles in docs.items():
        path = root / relative
        if not path.is_file():
            raise ValidationError(f"{relative}: state class documentation is missing")
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle not in text:
                raise ValidationError(f"{relative}: missing state class documentation marker {needle!r}")


def validate_shiki_manifest(root: Path = ROOT) -> None:
    try:
        manifest = load_manifest(root)
    except ManifestError as error:
        raise ValidationError(str(error)) from error

    if manifest.get("version") != 1:
        raise ValidationError(f"{MANIFEST_PATH}: version must be 1")

    directories = manifest_directories(manifest)
    if not directories:
        raise ValidationError(f"{MANIFEST_PATH}: directories must not be empty")

    for relative, metadata in directories.items():
        require_manifest_path(relative, field="directory")
        if not isinstance(metadata.get("kind"), str) or not metadata["kind"]:
            raise ValidationError(f"{MANIFEST_PATH}: {relative} must declare kind")
        if not isinstance(metadata.get("tracked"), bool):
            raise ValidationError(f"{MANIFEST_PATH}: {relative}.tracked must be a boolean")
        if not isinstance(metadata.get("required"), bool):
            raise ValidationError(f"{MANIFEST_PATH}: {relative}.required must be a boolean")

    for relative in manifest_required_directories(manifest):
        path = root / relative
        metadata = directories[relative]
        if not path.is_dir():
            raise ValidationError(f"{MANIFEST_PATH}: required directory {relative} is missing")
        if metadata.get("tracked") is True and not any(path.iterdir()):
            raise ValidationError(f"{MANIFEST_PATH}: required tracked directory {relative} must contain .gitkeep or tracked files")

    for relative in manifest_runtime_directories(manifest):
        metadata = directories[relative]
        if metadata.get("tracked") is True:
            raise ValidationError(f"{MANIFEST_PATH}: runtime directory {relative} must not be tracked")
        tracked = git_tracked_paths(root, relative)
        if tracked:
            raise ValidationError(f"{MANIFEST_PATH}: runtime-only directory {relative} has tracked files: {', '.join(tracked)}")

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValidationError(f"{MANIFEST_PATH}: files must be an object")
    for relative, metadata in files.items():
        require_manifest_path(relative, field="file")
        if not isinstance(metadata, dict):
            raise ValidationError(f"{MANIFEST_PATH}: file entry {relative} must be an object")
        if not isinstance(metadata.get("tracked"), bool):
            raise ValidationError(f"{MANIFEST_PATH}: {relative}.tracked must be a boolean")
        if not isinstance(metadata.get("required"), bool):
            raise ValidationError(f"{MANIFEST_PATH}: {relative}.required must be a boolean")
    for relative in manifest_required_files(manifest):
        if not (root / relative).is_file():
            raise ValidationError(f"{MANIFEST_PATH}: required file {relative} is missing")

    for required_file in (".shiki/config.yaml", GUARDIAN_POLICY_PATH, ".shiki/policy.example.yaml", ".shiki/README.md", MANIFEST_PATH):
        if required_file not in manifest_required_files(manifest):
            raise ValidationError(f"{MANIFEST_PATH}: {required_file} must be listed as a required file")

    for required_directory in (".shiki/schemas", ".shiki/templates"):
        if required_directory not in directories:
            raise ValidationError(f"{MANIFEST_PATH}: {required_directory} must be represented")

    create_directories = manifest_create_directories(manifest)
    if tuple(create_directories) != TARGET_STATE_DIRECTORIES:
        raise ValidationError(f"{MANIFEST_PATH}: install.create_directories must match TARGET_STATE_DIRECTORIES")
    for relative in create_directories:
        require_manifest_path(relative, field="install.create_directories")
        if relative not in directories:
            raise ValidationError(f"{MANIFEST_PATH}: create directory {relative} has no directory entry")

    install_include = manifest_install_include(manifest)
    for relative in install_include:
        require_manifest_path(relative.removesuffix("/**"), field="install.include")
        if relative.endswith("/**"):
            base = relative[:-3]
            if not (root / base).is_dir():
                raise ValidationError(f"{MANIFEST_PATH}: install include base {base} is missing")
            continue
        if not (root / relative).exists():
            raise ValidationError(f"{MANIFEST_PATH}: install include {relative} is missing")
    for required_file in manifest_required_files(manifest):
        if required_file not in install_include:
            raise ValidationError(f"{MANIFEST_PATH}: required file {required_file} must be included for install")
    for required_include in (".shiki/schemas/**", ".shiki/templates/**"):
        if required_include not in install_include:
            raise ValidationError(f"{MANIFEST_PATH}: {required_include} must be included for install")

    excluded = manifest_exclude_from_commit(manifest)
    if ".shiki/gha/**" not in excluded:
        raise ValidationError(f"{MANIFEST_PATH}: .shiki/gha/** must be excluded from committed state")
    for relative in excluded:
        require_manifest_path(relative.removesuffix("/**"), field="install.exclude_from_commit")

    validate_shiki_state_classes(root, manifest, directories)
    validate_readme_manifest_layout(root, manifest)


def validate_shiki_cli_module_boundaries(root: Path = ROOT) -> None:
    for relative in SHIKI_CLI_MODULE_FILES:
        path = root / relative
        if not path.is_file():
            raise ValidationError(f"{relative}: expected Shiki CLI module file is missing")

    shim = root / "scripts/shiki.py"
    shim_text = shim.read_text(encoding="utf-8")
    if not shim_text.startswith("#!/usr/bin/env python3"):
        raise ValidationError("scripts/shiki.py: executable shim must keep the Python shebang")
    if "from shiki_cli import build_parser, main" not in shim_text:
        raise ValidationError("scripts/shiki.py: must delegate parser/main behavior to shiki_cli.py")
    if len(shim_text.splitlines()) > 350:
        raise ValidationError("scripts/shiki.py: shim must remain under 350 lines")

    template_paths = set(TEMPLATE_PATHS)
    for relative in SHIKI_CLI_MODULE_FILES:
        if relative not in template_paths:
            raise ValidationError(f"scripts/shiki_installer.py: TEMPLATE_PATHS must include {relative}")
    if "scripts/test_shiki_module_boundaries.sh" not in template_paths:
        raise ValidationError("scripts/shiki_installer.py: TEMPLATE_PATHS must include scripts/test_shiki_module_boundaries.sh")
    if "scripts/test_shiki_migrations.sh" not in template_paths:
        raise ValidationError("scripts/shiki_installer.py: TEMPLATE_PATHS must include scripts/test_shiki_migrations.sh")
    if "scripts/test_shiki_state_classes.sh" not in template_paths:
        raise ValidationError("scripts/shiki_installer.py: TEMPLATE_PATHS must include scripts/test_shiki_state_classes.sh")
    if "scripts/test_shiki_guardian_policy.sh" not in template_paths:
        raise ValidationError("scripts/shiki_installer.py: TEMPLATE_PATHS must include scripts/test_shiki_guardian_policy.sh")
    if not (root / "scripts/test_shiki_module_boundaries.sh").is_file():
        raise ValidationError("scripts/test_shiki_module_boundaries.sh: module boundary regression test is missing")
    if not (root / "scripts/test_shiki_migrations.sh").is_file():
        raise ValidationError("scripts/test_shiki_migrations.sh: migration regression test is missing")
    if not (root / "scripts/test_shiki_state_classes.sh").is_file():
        raise ValidationError("scripts/test_shiki_state_classes.sh: state class regression test is missing")
    if "scripts/test_shiki_provider_config.sh" not in template_paths:
        raise ValidationError("scripts/shiki_installer.py: TEMPLATE_PATHS must include scripts/test_shiki_provider_config.sh")
    if not (root / "scripts/test_shiki_provider_config.sh").is_file():
        raise ValidationError("scripts/test_shiki_provider_config.sh: provider config regression test is missing")
    for relative in (
        "scripts/shiki_evidence.py",
        "scripts/build_cca_evidence_manifest.py",
        "scripts/test_shiki_evidence_integrity.sh",
        "scripts/test_shiki_governance_evidence.sh",
    ):
        if relative not in template_paths:
            raise ValidationError(f"scripts/shiki_installer.py: TEMPLATE_PATHS must include {relative}")
        if not (root / relative).is_file():
            raise ValidationError(f"{relative}: evidence integrity file is missing")

    scripts_path = str(root / "scripts")
    inserted = False
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
        inserted = True
    try:
        for module_name in SHIKI_CLI_MODULE_NAMES:
            importlib.import_module(module_name)
    except Exception as error:
        raise ValidationError(f"Shiki CLI module import failed: {error}") from error
    finally:
        if inserted:
            try:
                sys.path.remove(scripts_path)
            except ValueError:
                pass


def validate_shiki_migrations(root: Path = ROOT) -> None:
    registry_errors = validate_migration_registry()
    if registry_errors:
        raise ValidationError(f"scripts/shiki_migrations.py: {'; '.join(registry_errors)}")
    state_path = root / MIGRATION_STATE_PATH
    if not state_path.is_file():
        raise ValidationError(f"{MIGRATION_STATE_PATH}: migration state file is missing")
    try:
        state = load_migration_state(root)
    except Exception as error:
        raise ValidationError(str(error)) from error
    state_errors = validate_migration_state_data(state)
    if state_errors:
        raise ValidationError(f"{MIGRATION_STATE_PATH}: {'; '.join(state_errors)}")
    status = migration_status(root)
    if status["pending"]:
        raise ValidationError(f"{MIGRATION_STATE_PATH}: committed repository must not have pending migrations: {', '.join(status['pending'])}")
    applied_ids = {record.get("id") for record in state.get("applied", []) if isinstance(record, dict)}
    if BASELINE_MIGRATION_ID not in applied_ids:
        raise ValidationError(f"{MIGRATION_STATE_PATH}: baseline migration {BASELINE_MIGRATION_ID} must be applied")
    if STATE_CLASSES_MIGRATION_ID not in applied_ids:
        raise ValidationError(f"{MIGRATION_STATE_PATH}: state class migration {STATE_CLASSES_MIGRATION_ID} must be applied")

    manifest = load_manifest(root)
    directories = manifest_directories(manifest)
    if ".shiki/migrations" not in directories:
        raise ValidationError(f"{MANIFEST_PATH}: .shiki/migrations must be represented")
    migration_dir = directories[".shiki/migrations"]
    if migration_dir.get("kind") != "migration-state" or migration_dir.get("tracked") is not True or migration_dir.get("required") is not True:
        raise ValidationError(f"{MANIFEST_PATH}: .shiki/migrations must be tracked required migration-state")
    if MIGRATION_STATE_PATH not in manifest_required_files(manifest):
        raise ValidationError(f"{MANIFEST_PATH}: {MIGRATION_STATE_PATH} must be a required file")
    if MIGRATION_STATE_PATH not in manifest_install_include(manifest):
        raise ValidationError(f"{MANIFEST_PATH}: {MIGRATION_STATE_PATH} must be included for install")

    docs = {
        "docs/agents/shiki-migrations.md": ["M-20260604-0001-baseline", STATE_CLASSES_MIGRATION_ID, MIGRATION_STATE_PATH, "shiki migrate apply"],
        "docs/agents/shiki-doctor.md": ["doctor.migrations.state", "doctor.migrations.pending"],
        "docs/agents/shiki-cli-architecture.md": ["scripts/shiki_migrations.py"],
        "docs/agents/decision-control.md": [MIGRATION_STATE_PATH],
        "docs/agents/checklists.md": ["migration registry", MIGRATION_STATE_PATH],
    }
    for relative, needles in docs.items():
        path = root / relative
        if not path.is_file():
            raise ValidationError(f"{relative}: migration documentation is missing")
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle not in text:
                raise ValidationError(f"{relative}: missing migration documentation reference {needle!r}")


def validate_guardian_policy_contracts(root: Path = ROOT) -> None:
    policy_path = root / GUARDIAN_POLICY_PATH
    if not policy_path.is_file():
        raise ValidationError(f"{GUARDIAN_POLICY_PATH}: Guardian policy file is missing")
    try:
        policy = load_guardian_policy(root)
    except GuardianPolicyError as error:
        raise ValidationError(str(error)) from error
    policy_errors = validate_guardian_policy(policy)
    if policy_errors:
        raise ValidationError(f"{GUARDIAN_POLICY_PATH}: {'; '.join(policy_errors)}")
    if not {"high", "critical"}.issubset(set(policy.applies_to_risk)):
        raise ValidationError(f"{GUARDIAN_POLICY_PATH}: high and critical risks must require Guardian approval")
    if policy.label != "guardian:approved":
        raise ValidationError(f"{GUARDIAN_POLICY_PATH}: guardian label must be guardian:approved")
    if not policy.require_label_actor:
        raise ValidationError(f"{GUARDIAN_POLICY_PATH}: guardian label actor must be required")
    if not policy.guardian_comment_enabled or not policy.require_head_sha:
        raise ValidationError(f"{GUARDIAN_POLICY_PATH}: Guardian comments must require current head SHA")
    if policy.github_actions_review_bridge_counts_as_guardian:
        raise ValidationError(f"{GUARDIAN_POLICY_PATH}: CCA Review Bridge must not count as Guardian")
    if policy.advisory_claude_review_counts_as_guardian:
        raise ValidationError(f"{GUARDIAN_POLICY_PATH}: advisory Claude review must not count as Guardian")
    if policy.ai_review_enabled and not policy.ai_review_require_head_sha:
        raise ValidationError(f"{GUARDIAN_POLICY_PATH}: external_ai_guardian_review must bind to the head SHA (ADR 0010)")

    manifest = load_manifest(root)
    files = manifest.get("files")
    if not isinstance(files, dict) or GUARDIAN_POLICY_PATH not in files:
        raise ValidationError(f"{MANIFEST_PATH}: {GUARDIAN_POLICY_PATH} must be listed in files")
    guardian_file = files[GUARDIAN_POLICY_PATH]
    if not isinstance(guardian_file, dict) or guardian_file.get("kind") != "governance-policy":
        raise ValidationError(f"{MANIFEST_PATH}: {GUARDIAN_POLICY_PATH} must be kind governance-policy")
    if GUARDIAN_POLICY_PATH not in manifest_required_files(manifest):
        raise ValidationError(f"{MANIFEST_PATH}: {GUARDIAN_POLICY_PATH} must be required")
    if GUARDIAN_POLICY_PATH not in manifest_install_include(manifest):
        raise ValidationError(f"{MANIFEST_PATH}: {GUARDIAN_POLICY_PATH} must be included for install")

    template_paths = set(TEMPLATE_PATHS)
    for relative in ("scripts/shiki_guardian.py", "scripts/test_shiki_guardian_policy.sh"):
        if relative not in template_paths:
            raise ValidationError(f"scripts/shiki_installer.py: TEMPLATE_PATHS must include {relative}")

    workflow_path = root / ".github" / "workflows" / "shiki-cca-completion.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    for needle in (
        "live-guardian-comments.json",
        "live-guardian-events.json",
        "live-guardian-timeline.json",
        "--guardian-policy .shiki/guardian-policy.json",
        "--guardian-comments .shiki/gha/live-guardian-comments.json",
        "--guardian-events .shiki/gha/live-guardian-events.json",
    ):
        if needle not in workflow_text:
            raise ValidationError(f"{workflow_path}: missing Guardian evidence wiring {needle!r}")

    mergegate_text = (root / "scripts" / "mergegate_check.py").read_text(encoding="utf-8")
    for needle in (
        "evaluate_guardian_approval",
        "enforce_guardian_policy",
        "--guardian-comments",
        "--guardian-events",
    ):
        if needle not in mergegate_text:
            raise ValidationError(f"scripts/mergegate_check.py: missing Guardian policy enforcement text {needle!r}")

    docs = {
        "docs/agents/guardian-policy.md": [
            GUARDIAN_POLICY_PATH,
            "guardian:approved",
            "Guardian approval granted",
            "CCA Review Bridge is not Guardian approval",
            "advisory Claude review is not Guardian approval",
            "external_ai_guardian_review",
        ],
        "docs/agents/decision-control.md": [GUARDIAN_POLICY_PATH, "current PR head SHA", "external_ai_guardian_review"],
        "docs/agents/checklists.md": [GUARDIAN_POLICY_PATH, "policy-backed Guardian"],
        "docs/agents/completion-check-agent.md": [GUARDIAN_POLICY_PATH, "needs_guardian"],
        "docs/agents/shiki-doctor.md": ["doctor.guardian.policy", "doctor.guardian.github_events"],
        ".github/prompts/cca-completion-check.md": [GUARDIAN_POLICY_PATH, "CCA Review Bridge"],
    }
    for relative, needles in docs.items():
        path = root / relative
        if not path.is_file():
            raise ValidationError(f"{relative}: Guardian policy documentation is missing")
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle not in text:
                raise ValidationError(f"{relative}: missing Guardian policy documentation reference {needle!r}")


def validate_evidence_integrity_contracts(root: Path = ROOT) -> None:
    schema_path = root / ".shiki" / "schemas" / "cca-evidence-manifest.schema.json"
    if not schema_path.is_file():
        raise ValidationError(".shiki/schemas/cca-evidence-manifest.schema.json: schema is missing")
    schema = load_json(schema_path)
    if not isinstance(schema, dict):
        raise ValidationError(f"{schema_path}: schema must be a JSON object")
    required = set(schema.get("required", []))
    for field in ("version", "kind", "repository", "pr", "head_sha", "workflow", "artifact", "files", "verdict", "created_at"):
        if field not in required:
            raise ValidationError(f"{schema_path}: {field} must be required")
    fixture_manifest = {
        "version": 1,
        "kind": "shiki-cca-evidence-manifest",
        "repository": "OWNER/REPO",
        "pr": 1,
        "head_sha": "a" * 40,
        "workflow": {
            "name": "Shiki CCA Completion",
            "run_id": "123",
            "run_attempt": "1",
            "job": "CCA verdict",
            "event_name": "pull_request",
        },
        "artifact": {
            "name": CCA_EVIDENCE_ARTIFACT_NAME,
            "path": ".shiki/gha",
            "uploaded_by": "github-actions[bot]",
        },
        "files": [
            {"path": relative, "sha256": "0" * 64, "required": True}
            for relative in REQUIRED_CCA_EVIDENCE_FILES
        ],
        "verdict": {
            "verdict": "complete",
            "goal_id": "G-0012",
            "task_id": "T-0047",
            "pr": 1,
            "head_sha": "a" * 40,
        },
        "created_at": "2026-06-05T00:00:00Z",
    }
    try:
        validate_json_schema(fixture_manifest, schema)
    except (UnsupportedJsonSchemaError, ValueError) as error:
        raise ValidationError(f"{schema_path}: fixture validation failed: {error}") from error

    workflow_path = root / ".github" / "workflows" / "shiki-cca-completion.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    for needle in (
        "Build CCA evidence manifest",
        "scripts/build_cca_evidence_manifest.py",
        "--output .shiki/gha/cca-evidence-manifest.json",
        "name: shiki-cca-evidence",
        "--cca-evidence-manifest .shiki/gha/cca-evidence-manifest.json",
        "--expected-repository",
    ):
        if needle not in workflow_text:
            raise ValidationError(f"{workflow_path}: missing CCA evidence manifest wiring {needle!r}")

    mergegate_text = (root / "scripts" / "mergegate_check.py").read_text(encoding="utf-8")
    for needle in ("--cca-evidence-manifest", "validate_cca_evidence_manifest", "expected_repository"):
        if needle not in mergegate_text:
            raise ValidationError(f"scripts/mergegate_check.py: missing CCA evidence manifest enforcement {needle!r}")

    docs = {
        "docs/agents/evidence-integrity.md": [
            CCA_EVIDENCE_MANIFEST_PATH,
            "workflow-generated",
            "evidence_refs",
            "PR-authored `.shiki/gha` evidence is not trusted",
        ],
        "docs/agents/completion-check-agent.md": [CCA_EVIDENCE_MANIFEST_PATH],
        "docs/agents/decision-control.md": ["evidence_refs"],
        "docs/agents/checklists.md": ["CCA evidence manifest"],
        "docs/agents/shiki-doctor.md": ["doctor.evidence_integrity.manifest"],
    }
    for relative, needles in docs.items():
        path = root / relative
        if not path.is_file():
            raise ValidationError(f"{relative}: evidence integrity documentation is missing")
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle not in text:
                raise ValidationError(f"{relative}: missing evidence integrity documentation reference {needle!r}")

    digest_fixture = {
        "id": "L-20260605T000000000000Z-00000000",
        "timestamp": "2026-06-05T00:00:00Z",
        "goal_id": "G-0012",
        "task_id": "T-0047",
        "type": "check",
        "actor": "fixture",
        "summary": "fixture",
        "evidence": ["fixture"],
        "evidence_refs": [{"kind": "github-pr", "pr": 1, "head_sha": "a" * 40}],
        "ledger_integrity": {"algorithm": "sha256"},
    }
    digest_fixture["ledger_integrity"]["canonical_digest"] = ledger_entry_digest(digest_fixture)
    integrity_errors = validate_ledger_integrity(digest_fixture)
    if integrity_errors:
        raise ValidationError(f"scripts/shiki_evidence.py: valid ledger integrity fixture failed: {integrity_errors}")


def validate_governance_evidence_regression_contracts(root: Path = ROOT) -> None:
    test_path = root / "scripts" / "test_shiki_governance_evidence.sh"
    if not test_path.is_file():
        raise ValidationError("scripts/test_shiki_governance_evidence.sh: governance evidence regression test is missing")
    template_paths = set(TEMPLATE_PATHS)
    if "scripts/test_shiki_governance_evidence.sh" not in template_paths:
        raise ValidationError("scripts/shiki_installer.py: TEMPLATE_PATHS must include scripts/test_shiki_governance_evidence.sh")
    text = test_path.read_text(encoding="utf-8")
    for needle in (
        "Group A: forged Guardian evidence",
        "Group B: forged CCA verdict or manifest evidence",
        "Group C: forged ledger refs and integrity",
        "Group D: stale mirror state",
        "Group E: untrusted PR mutations",
        "Group F: missing evidence",
        "Group G: exact Guardian evidence comments",
        "Group H: workflow and static contract checks",
        "no Guardian approval evidence is present",
        "validate_cca_evidence_manifest",
        "validate_ledger_integrity",
        "workflow-runtime-evidence",
    ):
        if needle not in text:
            raise ValidationError(f"scripts/test_shiki_governance_evidence.sh: missing regression marker {needle!r}")

    docs = {
        "docs/agents/evidence-integrity.md": [
            "Adversarial Evidence Tests",
            "scripts/test_shiki_governance_evidence.sh",
            "forged",
            "stale",
            "missing",
        ],
        "docs/agents/guardian-policy.md": [
            "scripts/test_shiki_governance_evidence.sh",
            "no Guardian approval evidence is present",
            "stale-head comments",
        ],
    }
    for relative, needles in docs.items():
        path = root / relative
        if not path.is_file():
            raise ValidationError(f"{relative}: governance evidence regression documentation is missing")
        doc_text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle not in doc_text:
                raise ValidationError(f"{relative}: missing governance evidence regression reference {needle!r}")


def validate_provider_config_contracts(root: Path = ROOT) -> None:
    try:
        default = provider_from_values(repo="OWNER/REPO")
        if default.provider != "github":
            raise ValidationError("scripts/shiki_provider.py: default provider must be github")
        if default.host != DEFAULT_GITHUB_HOST:
            raise ValidationError("scripts/shiki_provider.py: default host must be github.com")
        if default.protocol != "https":
            raise ValidationError("scripts/shiki_provider.py: default remote protocol must be https")
        if default.api_base_url != "https://api.github.com":
            raise ValidationError("scripts/shiki_provider.py: github.com API URL must be https://api.github.com")
        if canonical_remote_url(default) != "https://github.com/OWNER/REPO.git":
            raise ValidationError("scripts/shiki_provider.py: default remote URL drifted")

        enterprise = provider_from_values(repo="OWNER/REPO", host="github.example.com", protocol="ssh")
        if enterprise.api_base_url != "https://github.example.com/api/v3":
            raise ValidationError("scripts/shiki_provider.py: enterprise API URL must default to https://HOST/api/v3")
        if api_base_url_for_host("github.example.com") != "https://github.example.com/api/v3":
            raise ValidationError("scripts/shiki_provider.py: api_base_url_for_host drifted")

        for kwargs in (
            {"repo": "OWNER/REPO", "provider": "gitlab"},
            {"repo": "OWNER/REPO", "protocol": "git"},
            {"repo": "not-a-slug"},
            {"repo": "OWNER/REPO", "host": "bad/host"},
        ):
            try:
                provider_from_values(**kwargs)
            except ProviderConfigError:
                continue
            raise ValidationError(f"scripts/shiki_provider.py: invalid provider config accepted: {kwargs}")
    except ProviderConfigError as error:
        raise ValidationError(f"scripts/shiki_provider.py: default provider config is invalid: {error}") from error

    repo_config = root / ".shiki" / "repo.json"
    if repo_config.exists():
        data = load_json(repo_config)
        if not isinstance(data, dict):
            raise ValidationError(f"{repo_config}: repo config must be a JSON object")
        try:
            provider_from_repo_json(data)
        except ProviderConfigError as error:
            raise ValidationError(f"{repo_config}: invalid provider fields: {error}") from error


def config_model() -> dict[str, Any]:
    config_path = ROOT / ".shiki" / "config.yaml"
    try:
        return load_yaml_model(config_path)
    except WorkflowParseError as error:
        raise ValidationError(f"{config_path}: {error}") from error


def validate_runtime_contracts(root: Path = ROOT) -> None:
    registry_names = runtime_names()
    if tuple(RUNTIME_NAMES) != registry_names:
        raise ValidationError("scripts/shiki_contracts.py: RUNTIME_NAMES must match shiki_runtime_registry.runtime_names()")

    registry = runtime_registry()
    if set(registry) != set(registry_names):
        raise ValidationError("scripts/shiki_runtime_registry.py: registry keys must match runtime_names()")

    for name in registry_names:
        descriptor = registry[name]
        if descriptor.name != name:
            raise ValidationError(f"scripts/shiki_runtime_registry.py: descriptor key {name} must match descriptor.name")
        if not descriptor.display_name:
            raise ValidationError(f"scripts/shiki_runtime_registry.py: {name} must declare display_name")
        if not descriptor.roles:
            raise ValidationError(f"scripts/shiki_runtime_registry.py: {name} must declare roles")
        if not descriptor.execution_mode:
            raise ValidationError(f"scripts/shiki_runtime_registry.py: {name} must declare execution_mode")
        if not descriptor.auth_mode:
            raise ValidationError(f"scripts/shiki_runtime_registry.py: {name} must declare auth_mode")
        for role in descriptor.roles:
            if role not in CONFIG_RUNTIME_ROLES and role not in TASK_RUNTIME_ROLES:
                raise ValidationError(f"scripts/shiki_runtime_registry.py: {name} declares unknown role {role!r}")

    validate_config_runtime_assignments()
    validate_runtime_registry_docs(root)


def validate_config_runtime_assignments() -> None:
    config_path = ROOT / ".shiki" / "config.yaml"
    model = config_model()
    runtimes = model.get("runtimes")
    if not isinstance(runtimes, dict):
        raise ValidationError(f"{config_path}: runtimes must be a mapping")

    missing = sorted(set(CONFIG_RUNTIME_ROLES) - set(runtimes))
    if missing:
        raise ValidationError(f"{config_path}: runtimes missing required roles: {', '.join(missing)}")

    for role in sorted(runtimes):
        if role.endswith("_rationale"):
            if not isinstance(runtimes[role], str) or not runtimes[role]:
                raise ValidationError(f"{config_path}: runtimes.{role} must be a non-empty rationale string")
            continue
        runtime_name = runtimes[role]
        if role not in CONFIG_RUNTIME_ROLES:
            raise ValidationError(f"{config_path}: runtimes.{role} is not a supported config runtime role")
        if not isinstance(runtime_name, str) or not runtime_name:
            raise ValidationError(f"{config_path}: runtimes.{role} must be a non-empty runtime name")
        try:
            validate_runtime_role_assignment(role, runtime_name)
        except RuntimeRegistryError as error:
            raise ValidationError(f"{config_path}: runtimes.{role}: {error}") from error
        descriptor = get_runtime(runtime_name)
        if descriptor.requires_rationale and f"{role}_rationale" not in runtimes:
            raise ValidationError(f"{config_path}: runtimes.{role} uses {runtime_name!r} and requires {role}_rationale")


def validate_task_runtime_assignment(path: Path, runtime_name: str) -> None:
    try:
        descriptor = get_runtime(runtime_name)
    except RuntimeRegistryError as error:
        raise ValidationError(f"{path}: assigned_runtime: {error}") from error
    if not set(descriptor.roles).intersection(TASK_RUNTIME_ROLES):
        raise ValidationError(
            f"{path}: assigned_runtime {runtime_name!r} must support one of {sorted(TASK_RUNTIME_ROLES)}"
        )


def validate_runtime_registry_docs(root: Path = ROOT) -> None:
    doc_path = root / "docs" / "agents" / "runtime-registry.md"
    if not doc_path.is_file():
        raise ValidationError(f"{doc_path}: runtime registry contract documentation is missing")
    text = doc_path.read_text(encoding="utf-8")
    for name in runtime_names():
        if f"`{name}`" not in text:
            raise ValidationError(f"{doc_path}: missing runtime {name!r}")
    for required in (
        "runtime identity",
        "runtime role",
        "execution mode",
        "auth mode",
        "provider abstraction",
        "doctor checks",
        "migration framework",
    ):
        if required not in text:
            raise ValidationError(f"{doc_path}: missing required topic {required!r}")


def id_format_description(prefix: str) -> str:
    return f"{prefix}-0001 or {prefix}-YYYYMMDDTHHMMSSffffffZ-<8 hex>"


def require_control_id(path: Path, value: str, pattern: re.Pattern[str], prefix: str, field: str = "id") -> None:
    if not pattern.match(value):
        raise ValidationError(f"{path}: {field} must match {id_format_description(prefix)}")


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
        if not skill_exists(skill):
            raise ValidationError(
                f"{path}: required skill {skill!r} is not backed by "
                f"skills/engineering/{skill}/SKILL.md"
            )


def skill_exists(skill: str) -> bool:
    if skill in {"none", "evidence-only"}:
        return True
    return (ROOT / "skills" / "engineering" / skill / "SKILL.md").exists()


def validate_lock_names(path: Path, locks: list[Any]) -> None:
    known_shiki_locks = known_shiki_semantic_locks()
    for lock in locks:
        if not isinstance(lock, str) or not lock.strip():
            raise ValidationError(f"{path}: locks must contain non-empty strings")
        normalized = normalize_lock(lock)
        if normalized.startswith("shiki:") and normalized not in known_shiki_locks:
            raise ValidationError(
                f"{path}: unsupported Shiki semantic lock {lock!r}; "
                f"expected one of {sorted(known_shiki_locks)}"
            )


def validate_goal(path: Path, data: dict[str, Any]) -> str:
    require_keys(path, data, GOAL_REQUIRED)

    goal_id = require_string(path, data, "id")
    require_control_id(path, goal_id, GOAL_ID, "G")

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
    require_control_id(path, task_id, TASK_ID, "T")

    goal_id = require_string(path, data, "goal_id")
    require_control_id(path, goal_id, GOAL_ID, "G", field="goal_id")

    require_string(path, data, "title")
    require_string(path, data, "scope")
    require_string(path, data, "expected_branch")

    require_list(path, data, "non_goals")
    dependencies = require_list(path, data, "dependencies")
    validate_lock_names(path, require_list(path, data, "locks"))
    require_list(path, data, "acceptance_checks", non_empty=True)
    require_list(path, data, "ledger_evidence", non_empty=True)

    runtime = require_string(path, data, "assigned_runtime")
    validate_task_runtime_assignment(path, runtime)

    risk_level = require_string(path, data, "risk_level")
    if risk_level not in RISK_LEVELS:
        raise ValidationError(f"{path}: risk_level must be one of {sorted(RISK_LEVELS)}")

    status = data.get("status")
    if status is not None and status not in TASK_STATUSES:
        raise ValidationError(f"{path}: status must be one of {sorted(TASK_STATUSES)}")

    for dependency in dependencies:
        if not isinstance(dependency, str) or not TASK_ID.match(dependency):
            raise ValidationError(f"{path}: dependencies must contain {id_format_description('T')} ids")

    validate_skill_names(path, require_list(path, data, "required_skills"), key="required_skills")

    return task_id, dependencies


def validate_dag(path: Path, data: dict[str, Any], known_tasks: set[str]) -> None:
    require_keys(path, data, DAG_REQUIRED)
    goal_id = require_string(path, data, "goal_id")
    require_control_id(path, goal_id, GOAL_ID, "G", field="goal_id")

    nodes = require_list(path, data, "nodes", non_empty=True)
    edges = require_list(path, data, "edges")

    for node in nodes:
        if not isinstance(node, str) or not TASK_ID.match(node):
            raise ValidationError(f"{path}: nodes must contain {id_format_description('T')} ids")
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
    require_control_id(path, ledger_id, LEDGER_ID, "L")

    goal_id = require_string(path, data, "goal_id")
    require_control_id(path, goal_id, GOAL_ID, "G", field="goal_id")
    if known_goals and goal_id not in known_goals:
        raise ValidationError(f"{path}: goal_id {goal_id} has no matching goal file")

    task_id = data.get("task_id")
    if task_id is not None:
        if not isinstance(task_id, str) or not TASK_ID.match(task_id):
            raise ValidationError(f"{path}: task_id must match {id_format_description('T')} or null")
        if known_tasks and task_id not in known_tasks:
            raise ValidationError(f"{path}: task_id {task_id} has no matching task file")

    ledger_type = require_string(path, data, "type")
    if ledger_type not in LEDGER_TYPES:
        raise ValidationError(f"{path}: type must be one of {sorted(LEDGER_TYPES)}")

    require_string(path, data, "timestamp")
    require_string(path, data, "actor")
    require_string(path, data, "summary")
    require_list(path, data, "evidence", non_empty=True)
    integrity_errors = validate_ledger_integrity(data)
    if integrity_errors:
        raise ValidationError(f"{path}: {'; '.join(integrity_errors)}")


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
    require_control_id(path, task_id, TASK_ID, "T", field="task_id")
    if known_tasks and task_id not in known_tasks:
        raise ValidationError(f"{path}: task_id {task_id} has no matching task file")

    goal_id = require_string(path, data, "goal_id")
    require_control_id(path, goal_id, GOAL_ID, "G", field="goal_id")

    require_string(path, data, "branch")
    require_string(path, data, "path")
    require_string(path, data, "runtime")
    require_string(path, data, "state")
    require_string(path, data, "created_by")
    require_string(path, data, "created_at")
    validate_lock_names(path, require_list(path, data, "locks"))


def validate_plan(path: Path, data: dict[str, Any]) -> None:
    require_keys(path, data, PLAN_REQUIRED)
    plan_id = require_string(path, data, "id")
    require_control_id(path, plan_id, PLAN_ID, "P")
    require_string(path, data, "title")
    require_string(path, data, "outcome")

    grill = data.get("grill_with_docs")
    if not isinstance(grill, dict):
        raise ValidationError(f"{path}: grill_with_docs must be an object")
    if grill.get("status") != "complete":
        raise ValidationError(f"{path}: grill_with_docs.status must be complete")

    freeze = data.get("spec_freeze")
    if freeze is not None:
        if not isinstance(freeze, dict):
            raise ValidationError(f"{path}: spec_freeze must be an object")
        if freeze.get("status") != "frozen":
            raise ValidationError(f"{path}: spec_freeze.status must be frozen")

    tasks = require_list(path, data, "tasks", non_empty=True)
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            raise ValidationError(f"{path}: tasks[{index}] must be an object")
        for key in ("title", "scope", "acceptance_checks"):
            if key not in task:
                raise ValidationError(f"{path}: tasks[{index}] missing {key}")
        if not isinstance(task.get("acceptance_checks"), list) or not task["acceptance_checks"]:
            raise ValidationError(f"{path}: tasks[{index}].acceptance_checks must not be empty")
        if "locks" in task:
            locks = task.get("locks")
            if not isinstance(locks, list):
                raise ValidationError(f"{path}: tasks[{index}].locks must be a list")
            validate_lock_names(path, locks)


def validate_run(path: Path, data: dict[str, Any], known_tasks: set[str]) -> None:
    require_keys(path, data, RUN_REQUIRED)
    run_id = require_string(path, data, "id")
    require_control_id(path, run_id, RUN_ID, "RUN")
    plan_id = require_string(path, data, "plan_id")
    require_control_id(path, plan_id, PLAN_ID, "P", field="plan_id")
    goal_id = require_string(path, data, "goal_id")
    require_control_id(path, goal_id, GOAL_ID, "G", field="goal_id")
    for key in ("task_ids", "dispatchable_task_ids"):
        for task_id in require_list(path, data, key):
            if not isinstance(task_id, str) or not TASK_ID.match(task_id):
                raise ValidationError(f"{path}: {key} must contain {id_format_description('T')} ids")
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
    require_control_id(path, exec_id, EXEC_ID, "EXEC")
    task_id = require_string(path, data, "task_id")
    require_control_id(path, task_id, TASK_ID, "T", field="task_id")
    if known_tasks and task_id not in known_tasks:
        raise ValidationError(f"{path}: task_id {task_id} has no matching task file")
    goal_id = require_string(path, data, "goal_id")
    require_control_id(path, goal_id, GOAL_ID, "G", field="goal_id")
    require_string(path, data, "command")
    if not isinstance(data.get("returncode"), int):
        raise ValidationError(f"{path}: returncode must be an integer")
    require_string(path, data, "stdout", non_empty=False)
    require_string(path, data, "stderr", non_empty=False)
    require_string(path, data, "created_at")


def validate_smoke(path: Path, data: dict[str, Any]) -> None:
    require_keys(path, data, SMOKE_REQUIRED)
    smoke_id = require_string(path, data, "id")
    require_control_id(path, smoke_id, SMOKE_ID, "SMOKE")
    require_string(path, data, "repo")
    if not isinstance(data.get("dry_run"), bool):
        raise ValidationError(f"{path}: dry_run must be a boolean")
    if not isinstance(data.get("execute_github"), bool):
        raise ValidationError(f"{path}: execute_github must be a boolean")
    require_string(path, data, "created_at")


def validate_start(path: Path, data: dict[str, Any], known_tasks: set[str]) -> None:
    require_keys(path, data, START_REQUIRED)
    start_id = require_string(path, data, "id")
    require_control_id(path, start_id, START_ID, "START")
    require_string(path, data, "repo")
    require_string(path, data, "project_name")
    require_string(path, data, "skills_dir")
    questions = require_list(path, data, "questions")
    if not questions or not all(isinstance(question, str) and question for question in questions):
        raise ValidationError(f"{path}: questions must be a non-empty list of strings")
    plan_id = require_string(path, data, "plan_id")
    require_control_id(path, plan_id, PLAN_ID, "P", field="plan_id")
    goal_id = require_string(path, data, "goal_id")
    require_control_id(path, goal_id, GOAL_ID, "G", field="goal_id")
    run_id = require_string(path, data, "run_id")
    require_control_id(path, run_id, RUN_ID, "RUN", field="run_id")
    for task_id in require_list(path, data, "dispatchable_task_ids"):
        if not isinstance(task_id, str) or not TASK_ID.match(task_id):
            raise ValidationError(f"{path}: dispatchable_task_ids must contain {id_format_description('T')} ids")
        if known_tasks and task_id not in known_tasks:
            raise ValidationError(f"{path}: dispatchable task {task_id} has no matching task file")
    require_list(path, data, "issues")
    require_list(path, data, "handoffs")
    require_string(path, data, "created_at")


def json_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def validate_id_collection(
    paths: list[Path],
    *,
    prefix: str,
    pattern: re.Pattern[str],
    field: str = "id",
) -> None:
    seen: dict[str, Path] = {}
    for path in paths:
        data = load_json(path)
        if not isinstance(data, dict):
            raise ValidationError(f"{path}: state record must be a JSON object")
        value = require_string(path, data, field)
        require_control_id(path, value, pattern, prefix, field=field)
        if value in seen:
            raise ValidationError(f"{path}: duplicate {field} {value} also appears in {seen[value]}")
        if path.name != f"{value}.json":
            raise ValidationError(f"{path}: file name must match {field} {value}")
        seen[value] = path


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


def validate_codeowners_governance() -> None:
    path = ROOT / CODEOWNERS_PATH
    if not path.exists():
        raise ValidationError(f"{path}: CODEOWNERS file is required for critical Shiki governance paths")

    coverage: dict[str, set[str]] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            raise ValidationError(f"{path}:{line_number}: CODEOWNERS rule must include at least one owner")
        pattern = parts[0]
        owners = set(parts[1:])
        coverage.setdefault(pattern, set()).update(owners)

    for critical_path in CODEOWNERS_CRITICAL_PATHS:
        owners = coverage.get(critical_path)
        if not owners:
            raise ValidationError(f"{path}: missing CODEOWNERS rule for {critical_path}")
        if CODEOWNERS_REQUIRED_OWNER not in owners:
            raise ValidationError(
                f"{path}: {critical_path} must be owned by {CODEOWNERS_REQUIRED_OWNER}"
            )


def validate_prompt_contract_paths() -> None:
    suffixes = {".md", ".yml", ".yaml", ".json", ".toml"}
    for relative in CONTRACT_SCHEMA_SCAN_PATHS:
        base = ROOT / relative
        if not base.exists():
            continue
        paths = [base] if base.is_file() else list(base.rglob("*"))
        for path in paths:
            if path.is_file() and path.suffix in suffixes:
                text = path.read_text(encoding="utf-8")
                if OBSOLETE_CCA_VERDICT_SCHEMA_PATH in text:
                    raise ValidationError(
                        f"{path}: references obsolete CCA schema path "
                        f"{OBSOLETE_CCA_VERDICT_SCHEMA_PATH}; use {CANONICAL_CCA_VERDICT_SCHEMA_PATH}"
                    )
                if OBSOLETE_REPAIR_PACKET_SCHEMA_PATH in text:
                    raise ValidationError(
                        f"{path}: references obsolete repair packet schema path "
                        f"{OBSOLETE_REPAIR_PACKET_SCHEMA_PATH}; use {CANONICAL_REPAIR_PACKET_SCHEMA_PATH}"
                    )


def validate_source_of_truth_contracts() -> None:
    expected = canonical_source_of_truth_markdown()
    for relative in CONTRACT_SOURCE_OF_TRUTH_FILES:
        path = ROOT / relative
        if not path.exists():
            raise ValidationError(f"{path}: contract source-of-truth file is missing")
        if expected not in path.read_text(encoding="utf-8"):
            raise ValidationError(f"{path}: missing canonical source-of-truth block")


def validate_completion_check_docs() -> None:
    docs_path = ROOT / "docs" / "agents" / "completion-check-agent.md"
    text = docs_path.read_text(encoding="utf-8")
    for field in ('"head_sha"', '"can_merge"'):
        if field not in text:
            raise ValidationError(f"{docs_path}: CCA minimum JSON example must include {field}")


def validate_validate_workflow_coverage() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "shiki-validate.yml"
    model = load_workflow_contract(workflow_path)
    run_commands = "\n".join(workflow_step_runs(model))
    if "python3 -m py_compile scripts/*.py" not in run_commands:
        raise ValidationError(f"{workflow_path}: must compile all scripts/*.py files")
    if "for script in scripts/test_shiki_*.sh" not in run_commands or 'bash "$script"' not in run_commands:
        raise ValidationError(f"{workflow_path}: must run all scripts/test_shiki_*.sh contract tests")
    if "scripts/test_shiki_workflow_lint.sh --strict" not in run_commands:
        raise ValidationError(f"{workflow_path}: must run actionlint workflow lint in CI")
    if "scripts/test_shiki_shellcheck.sh --strict" not in run_commands:
        raise ValidationError(f"{workflow_path}: must run shellcheck in CI")
    if workflow_top_env(model).get("FORCE_JAVASCRIPT_ACTIONS_TO_NODE24") != "true":
        raise ValidationError(f"{workflow_path}: must force Node 24 compatibility in the validation workflow")


def load_workflow_contract(path: Path) -> dict[str, Any]:
    try:
        return load_workflow_model(path)
    except WorkflowParseError as error:
        raise ValidationError(f"{path}: {error}") from error


def validate_workflow_contracts() -> None:
    workflow_dir = ROOT / ".github" / "workflows"
    models: dict[Path, dict[str, Any]] = {}
    all_job_names: list[tuple[str, Path]] = []

    for filename, contract in WORKFLOW_CONTRACTS.items():
        path = workflow_dir / filename
        if not path.exists():
            raise ValidationError(f"{path}: required workflow file is missing")
        model = load_workflow_contract(path)
        models[path] = model

        if workflow_name(model) != contract["name"]:
            raise ValidationError(f"{path}: workflow name must be {contract['name']!r}")

        triggers = workflow_triggers(model)
        missing_triggers = sorted(set(contract["triggers"]) - triggers)
        if missing_triggers:
            raise ValidationError(f"{path}: missing required triggers: {', '.join(missing_triggers)}")

        permissions = workflow_permissions(model)
        expected_permissions = contract["permissions"]
        if permissions != expected_permissions:
            raise ValidationError(
                f"{path}: top-level permissions must be {expected_permissions!r}, got {permissions!r}"
            )

        jobs = workflow_jobs(model)
        for job_id, expected_name in contract["jobs"].items():
            job = jobs.get(job_id)
            if not isinstance(job, dict):
                raise ValidationError(f"{path}: missing required job {job_id!r}")
            actual_name = job.get("name")
            if actual_name != expected_name:
                raise ValidationError(f"{path}: job {job_id!r} name must be {expected_name!r}, got {actual_name!r}")
            all_job_names.append((expected_name, path))

        for job_id, expected_permissions in contract.get("job_permissions", {}).items():
            actual_permissions = workflow_job_permissions(model, job_id)
            if actual_permissions != expected_permissions:
                raise ValidationError(
                    f"{path}: job {job_id!r} permissions must be "
                    f"{expected_permissions!r}, got {actual_permissions!r}"
                )

    seen: dict[str, Path] = {}
    for name, path in all_job_names:
        if name in seen:
            raise ValidationError(f"{path}: duplicate workflow job display name {name!r} also appears in {seen[name]}")
        seen[name] = path

    validate_required_check_names(models)
    validate_node24_workflow_policy(models)


def config_required_checks() -> list[str]:
    config_path = ROOT / ".shiki" / "config.yaml"
    model = config_model()
    mergegate = model.get("mergegate")
    if not isinstance(mergegate, dict):
        raise ValidationError(f"{config_path}: mergegate must be a mapping")
    checks = mergegate.get("required_checks")
    if not isinstance(checks, list) or not all(isinstance(check, str) and check for check in checks):
        raise ValidationError(f"{config_path}: mergegate.required_checks must be a non-empty list of strings")
    return checks


def validate_required_check_names(models: dict[Path, dict[str, Any]]) -> None:
    job_names: dict[str, Path] = {}
    for path, model in models.items():
        for job in workflow_jobs(model).values():
            if not isinstance(job, dict) or not isinstance(job.get("name"), str):
                continue
            job_name = job["name"]
            if job_name in job_names:
                raise ValidationError(f"{path}: duplicate workflow job display name {job_name!r} also appears in {job_names[job_name]}")
            job_names[job_name] = path

    for check in config_required_checks():
        if check not in job_names:
            raise ValidationError(
                f".shiki/config.yaml: required check {check!r} has no matching workflow job display name"
            )


def validate_node24_workflow_policy(models: dict[Path, dict[str, Any]]) -> None:
    seen_deferred: set[tuple[str, str, str]] = set()
    for path, model in models.items():
        text = path.read_text(encoding="utf-8")
        if "ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION" in text:
            raise ValidationError(f"{path}: ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION is forbidden")
        if path.name in NODE24_FORCE_WORKFLOWS and workflow_top_env(model).get("FORCE_JAVASCRIPT_ACTIONS_TO_NODE24") != "true":
            raise ValidationError(f"{path}: must set FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true")
        for action in workflow_uses_actions(model):
            if action.startswith("docker://"):
                continue
            if "@" not in action:
                raise ValidationError(f"{path}: action {action!r} must pin an explicit version")
            owner_repo, version = action.rsplit("@", 1)
            if "*" in owner_repo or "*" in version:
                raise ValidationError(f"{path}: action {action!r} must not use wildcard Node 24 defers")
            if owner_repo == "anthropics/claude-code-action" and (path.name, owner_repo, version) not in NODE24_DEFERRED_ACTIONS:
                raise ValidationError(
                    f"{path}: {action!r} must match an explicit Node 24 deferred action exception"
                )
            allowed_versions = NODE24_OFFICIAL_ACTIONS.get(owner_repo)
            if (path.name, owner_repo, version) in NODE24_DEFERRED_ACTIONS:
                seen_deferred.add((path.name, owner_repo, version))
                continue
            if allowed_versions is not None and version not in allowed_versions:
                raise ValidationError(
                    f"{path}: {action!r} must use a Node 24-compatible official action version "
                    f"from {sorted(allowed_versions)}"
                )
    model_workflows = {path.name for path in models}
    configured_for_models = {item for item in NODE24_DEFERRED_ACTIONS if item[0] in model_workflows}
    missing_deferred = sorted(configured_for_models - seen_deferred)
    if missing_deferred:
        formatted = ", ".join(f"{workflow}:{action}@{version}" for workflow, action, version in missing_deferred)
        raise ValidationError(f"Node 24 deferred action exceptions are not present in workflow inventory: {formatted}")
    if seen_deferred:
        validate_node24_deferred_action_docs(seen_deferred)


def validate_node24_deferred_action_docs(deferred_actions: set[tuple[str, str, str]]) -> None:
    docs_path = ROOT / "docs" / "agents" / "node24-workflow-compatibility.md"
    if not docs_path.exists():
        raise ValidationError(f"{docs_path}: Node 24 workflow compatibility inventory is missing")
    text = docs_path.read_text(encoding="utf-8")
    for workflow, action, version in sorted(deferred_actions):
        pinned = f"{action}@{version}"
        if workflow not in text or pinned not in text:
            raise ValidationError(f"{docs_path}: missing deferred action inventory for {workflow} {pinned}")
    for workflow, action, version in sorted(NODE24_DEFERRED_ACTIONS):
        pinned = f"{action}@{version}"
        if workflow not in text or pinned not in text:
            raise ValidationError(f"{docs_path}: missing configured deferred action inventory for {workflow} {pinned}")


def top_level_permissions_block(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == "permissions:":
            collected: list[str] = []
            for follow in lines[index + 1 :]:
                if follow and not follow.startswith(" "):
                    break
                collected.append(follow)
            return "\n".join(collected)
    return ""


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
    top_permissions = top_level_permissions_block(text)
    if "write" in top_permissions:
        raise ValidationError(f"{workflow_path}: issue_comment workflow must not grant top-level write permissions")
    for permission in ("contents: read", "issues: read", "pull-requests: read"):
        if permission not in top_permissions:
            raise ValidationError(f"{workflow_path}: top-level permissions must include {permission}")
    if "commit-evidence:" not in text:
        raise ValidationError(f"{workflow_path}: write permissions must be isolated in a commit-evidence job")
    if "github.event_name == 'workflow_dispatch' && inputs.mode == 'execute-github'" not in text:
        raise ValidationError(f"{workflow_path}: commit-evidence job must only run for workflow_dispatch execute-github")
    if "contents: write" not in text or "issues: write" not in text or "pull-requests: write" not in text:
        raise ValidationError(f"{workflow_path}: commit-evidence job must declare required write permissions explicitly")
    if re.search(r"(?m)^\s*git push\s*$", text):
        raise ValidationError(f"{workflow_path}: must not push directly to the current branch")
    if "git push -u origin \"$evidence_branch\"" not in text or "gh pr create" not in text:
        raise ValidationError(f"{workflow_path}: orchestrator evidence must land through an evidence branch PR")


def validate_contract_schema_consistency() -> None:
    goal_schema_path = SHIKI / "schemas" / "goal.schema.json"
    goal_schema = load_json(goal_schema_path)
    if not isinstance(goal_schema, dict):
        raise ValidationError(f"{goal_schema_path}: schema must be a JSON object")
    goal_required = set(goal_schema.get("required", []))
    if "status" not in goal_required:
        raise ValidationError(f"{goal_schema_path}: status must be required by the Goal contract")
    goal_status_enum = goal_schema.get("properties", {}).get("status", {}).get("enum", [])
    if set(goal_status_enum) != GOAL_STATUSES:
        raise ValidationError(f"{goal_schema_path}: status enum must match {sorted(GOAL_STATUSES)}")

    ledger_schema_path = SHIKI / "schemas" / "ledger.schema.json"
    ledger_schema = load_json(ledger_schema_path)
    if not isinstance(ledger_schema, dict):
        raise ValidationError(f"{ledger_schema_path}: schema must be a JSON object")
    ledger_type_enum = ledger_schema.get("properties", {}).get("type", {}).get("enum", [])
    if set(ledger_type_enum) != LEDGER_TYPES:
        raise ValidationError(f"{ledger_schema_path}: type enum must match {sorted(LEDGER_TYPES)}")

    cca_schema_path = SHIKI / "schemas" / "cca-verdict.schema.json"
    cca_schema = load_json(cca_schema_path)
    if not isinstance(cca_schema, dict):
        raise ValidationError(f"{cca_schema_path}: schema must be a JSON object")
    for field in ("$schema", "$id"):
        if field not in cca_schema:
            raise ValidationError(f"{cca_schema_path}: {field} is required")
    cca_required = set(cca_schema.get("required", []))
    for field in ("checklist", "acceptance", "mergegate"):
        if field not in cca_required:
            raise ValidationError(f"{cca_schema_path}: {field} must be required by the CCA verdict contract")
    properties = cca_schema.get("properties", {})
    checklist_status_enum = (
        properties.get("checklist", {})
        .get("items", {})
        .get("properties", {})
        .get("status", {})
        .get("enum", [])
    )
    if set(checklist_status_enum) != CCA_ITEM_STATUSES:
        raise ValidationError(f"{cca_schema_path}: checklist[].status enum must match {sorted(CCA_ITEM_STATUSES)}")
    acceptance_items = properties.get("acceptance", {}).get("items", {})
    acceptance_required = set(acceptance_items.get("required", []))
    if acceptance_required != {"criterion", "status", "evidence"}:
        raise ValidationError(f"{cca_schema_path}: acceptance[] must require criterion, status, and evidence")
    acceptance_properties = acceptance_items.get("properties", {})
    acceptance_status_enum = acceptance_properties.get("status", {}).get("enum", [])
    if set(acceptance_status_enum) != CCA_ITEM_STATUSES:
        raise ValidationError(f"{cca_schema_path}: acceptance[].status enum must match {sorted(CCA_ITEM_STATUSES)}")
    acceptance_evidence = acceptance_properties.get("evidence", {})
    if acceptance_evidence.get("type") != "array":
        raise ValidationError(f"{cca_schema_path}: acceptance[].evidence must be an array")
    evidence_items = acceptance_evidence.get("items", {})
    if evidence_items.get("type") != "string" or evidence_items.get("minLength") != 1:
        raise ValidationError(f"{cca_schema_path}: acceptance[].evidence items must be non-empty strings")
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


def validate_json_schema_contracts() -> None:
    for schema_path in json_files(SHIKI / "schemas"):
        schema = load_json(schema_path)
        if not isinstance(schema, dict):
            raise ValidationError(f"{schema_path}: schema must be a JSON object")
        try:
            assert_supported_schema(schema)
        except UnsupportedJsonSchemaError as error:
            raise ValidationError(f"{schema_path}: {error}") from error

    cca_schema = load_json(SHIKI / "schemas" / "cca-verdict.schema.json")
    complete_cca = {
        "verdict": "complete",
        "summary": "fixture complete",
        "goal_id": "G-0012",
        "task_id": "T-0039",
        "pr": 1,
        "head_sha": "a" * 40,
        "can_merge": True,
        "checklist": [{"id": "fixture", "status": "pass", "blocking": False}],
        "acceptance": [{"criterion": "fixture", "status": "pass", "evidence": ["fixture"]}],
        "mergegate": {},
        "confidence": 1,
    }
    try:
        validate_json_schema(complete_cca, cca_schema)
        validate_json_schema({**complete_cca, "verdict": "needs_guardian", "can_merge": False}, cca_schema)
    except (UnsupportedJsonSchemaError, ValueError) as error:
        raise ValidationError(f"{CANONICAL_CCA_VERDICT_SCHEMA_PATH}: fixture validation failed: {error}") from error

    repair_schema = load_json(SHIKI / "schemas" / "repair-packet.schema.json")
    repair_packet = {
        "repair_id": "RP-0001",
        "goal_id": "G-0012",
        "task_id": "T-0039",
        "pr": 1,
        "attempt": 1,
        "failing_checklist_items": ["fixture"],
        "failing_acceptance_criteria": ["fixture"],
        "minimal_required_changes": ["fixture"],
        "prohibited_changes": [],
        "required_skill": "evidence-only",
        "verification_commands": ["python3 scripts/validate_shiki.py"],
        "evidence_required": ["fixture"],
        "stop_condition": "fixture",
    }
    try:
        validate_json_schema(repair_packet, repair_schema)
    except (UnsupportedJsonSchemaError, ValueError) as error:
        raise ValidationError(f"{CANONICAL_REPAIR_PACKET_SCHEMA_PATH}: fixture validation failed: {error}") from error


# Goals whose tasks are no longer loop-dispatched: the warning is scoped out so
# it does not retroactively spam the pre-existing terminal/archived-goal tasks.
_INACTIVE_GOAL_STATUSES = {"complete", "archived", "historical"}


def loop_lock_warnings(
    goal_payloads: dict[str, dict[str, Any]],
    task_payloads_by_goal: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """WARN-ONLY hint for loop tasks lacking .shiki/** lock coverage.

    A loop-executed task (claude-code/codex) is dispatched into a worktree where
    the goal loop syncs the full .shiki evidence set onto the task branch, so its
    locks should cover .shiki/**. This is advisory, never an error: auto-mutating
    a registered task's locks would break goal_reconcile's frozen-plan lock-match
    and retroactively warn pre-existing tasks. The loop instead guarantees
    coverage at dispatch time (shiki_tasks.loop_guaranteed_locks). The warning is
    scoped to ACTIVE goals so terminal/archived goals are not flagged.
    """
    # Lazy import: keep validate_shiki importable without pulling the task
    # lifecycle module (and its transitive deps) at module load (T1 cycle-avoidance style).
    from shiki_tasks import is_loop_executed_runtime, locks_cover_shiki_state

    warnings: list[str] = []
    for goal_id, tasks in task_payloads_by_goal.items():
        goal = goal_payloads.get(goal_id)
        # Without a goal payload we cannot prove the goal is active; stay quiet.
        if not isinstance(goal, dict):
            continue
        if goal.get("status") in _INACTIVE_GOAL_STATUSES:
            continue
        for task in tasks:
            runtime = task.get("assigned_runtime")
            if not is_loop_executed_runtime(runtime):
                continue
            locks = task.get("locks") if isinstance(task.get("locks"), list) else []
            if locks_cover_shiki_state(locks):
                continue
            warnings.append(
                f".shiki/tasks/{task.get('id')}.json: loop-executed task "
                f"({runtime}) on active goal {goal_id} lacks a lock covering "
                f"'.shiki/**'; the goal loop syncs .shiki evidence to the task "
                f"branch and guarantees this lock at dispatch time, but the "
                f"registered locks do not declare it"
            )
    return warnings


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    task_dependencies: dict[str, list[str]] = {}
    known_goals: set[str] = set()
    goal_payloads: dict[str, dict[str, Any]] = {}
    task_payloads_by_goal: dict[str, list[dict[str, Any]]] = {}

    try:
        for schema in json_files(SHIKI / "schemas"):
            load_json(schema)
        validate_contract_schema_consistency()
        validate_json_schema_contracts()
        validate_prompt_contract_paths()
        validate_source_of_truth_contracts()
        validate_completion_check_docs()
        validate_workflow_contracts()
        validate_validate_workflow_coverage()
        validate_shiki_manifest()
        validate_shiki_cli_module_boundaries()
        validate_shiki_migrations()
        validate_guardian_policy_contracts()
        validate_evidence_integrity_contracts()
        validate_governance_evidence_regression_contracts()
        validate_provider_config_contracts()
        validate_runtime_contracts()
        validate_issue_forms()
        validate_codeowners_governance()
        validate_orchestrator_security()

        goal_paths = json_files(SHIKI / "goals")
        task_paths = json_files(SHIKI / "tasks")
        ledger_paths = json_files(SHIKI / "ledger")
        dag_paths = json_files(SHIKI / "dag")
        report_paths = json_files(SHIKI / "reports")

        validate_id_collection(goal_paths, prefix="G", pattern=GOAL_ID)
        validate_id_collection(task_paths, prefix="T", pattern=TASK_ID)
        validate_id_collection(ledger_paths, prefix="L", pattern=LEDGER_ID)
        validate_id_collection(dag_paths, prefix="G", pattern=GOAL_ID, field="goal_id")
        validate_id_collection(report_paths, prefix="R", pattern=REPORT_ID)

        memory_paths = json_files(SHIKI / "memories")
        validate_id_collection(memory_paths, prefix="MEM", pattern=MEMORY_ID)
        memory_goal_refs: list[tuple[Path, str]] = []
        for memory_path in memory_paths:
            data = load_json(memory_path)
            if not isinstance(data, dict):
                raise ValidationError(f"{memory_path}: memory entry must be a JSON object")
            memory_errors = memory_entry_errors(data, root=SHIKI.parent)
            if memory_errors:
                raise ValidationError(f"{memory_path}: {'; '.join(memory_errors)}")
            source = data.get("source")
            goal_ref = str(source.get("goal_id") or "") if isinstance(source, dict) else ""
            if goal_ref:
                memory_goal_refs.append((memory_path, goal_ref))

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
            goal_payloads[goal_id] = data

        # A memory's source.goal_id must reference an existing goal (referential
        # integrity, mirroring the task goal_id check). A directly-committed
        # memory that anchors to a non-existent goal is rejected fail-closed.
        for memory_path, goal_ref in memory_goal_refs:
            if goal_ref not in known_goals:
                raise ValidationError(f"{memory_path}: source.goal_id {goal_ref} has no matching goal file")

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
            task_payloads_by_goal.setdefault(goal_id, []).append(data)

        # Advisory (WARN-ONLY) loop task-lock hint. Collected here once the goal
        # and task payloads are known; surfaced after validation without failing.
        warnings.extend(loop_lock_warnings(goal_payloads, task_payloads_by_goal))

        known_tasks = set(task_dependencies)
        for task_id, dependencies in task_dependencies.items():
            for dependency in dependencies:
                if dependency not in known_tasks:
                    raise ValidationError(f".shiki/tasks: {task_id} depends on unknown {dependency}")

        detect_cycles(Path(".shiki/tasks"), {task_id: deps for task_id, deps in task_dependencies.items()})

        # A goal's child set is its frozen DAG node set, NOT just the task files
        # currently registered. Using registered task files deadlocks a
        # multi-task goal: trimming registration to one task makes that task look
        # like the whole goal, so completing it forces goal-complete before the
        # rest exist. The DAG is the source of truth for the child set.
        dag_nodes_by_goal: dict[str, set[str]] = {}
        for dag_path in dag_paths:
            dag_data = load_json(dag_path)
            if isinstance(dag_data, dict):
                gid = str(dag_data.get("goal_id") or "")
                nodes = dag_data.get("nodes")
                if gid and isinstance(nodes, list):
                    dag_nodes_by_goal[gid] = {str(n) for n in nodes if isinstance(n, str)}
        task_status_by_id = {
            str(task.get("id")): task.get("status")
            for tasks in task_payloads_by_goal.values()
            for task in tasks
        }
        task_goal_by_id = {
            str(task.get("id")): str(task.get("goal_id") or "")
            for tasks in task_payloads_by_goal.values()
            for task in tasks
        }

        for goal_id, tasks in task_payloads_by_goal.items():
            goal = goal_payloads[goal_id]
            status = goal.get("status")
            if status in {"archived", "historical"}:
                continue
            dag_nodes = dag_nodes_by_goal.get(goal_id)
            # A DAG node must be a task anchored to this goal: a foreign task wired
            # into the goal's DAG would otherwise mix its status into this goal's
            # completion decision (DAG poisoning). Mirrors the goal_reconcile gate.
            if dag_nodes:
                for node in dag_nodes:
                    owner = task_goal_by_id.get(node)
                    if owner is not None and owner != goal_id:
                        raise ValidationError(
                            f".shiki/dag/{goal_id}.json: node {node} is a task of goal {owner}, not {goal_id}"
                        )
            registered = {str(task.get("id")) for task in tasks}
            if dag_nodes and registered <= dag_nodes:
                # The DAG covers every registered task: the frozen DAG node set is
                # the goal's child set. All nodes terminal (done/cancelled/
                # superseded) => the goal must be complete. While any node is
                # non-terminal — including a planned node not yet registered as a
                # task file — the goal stays active. This is what lets a
                # multi-task goal complete one task without forcing goal-complete.
                node_statuses = [task_status_by_id.get(node) for node in dag_nodes]
                all_terminal = all(s in TERMINAL_TASK_STATUSES for s in node_statuses)
                if all_terminal and status != "complete":
                    raise ValidationError(
                        f".shiki/goals/{goal_id}.json: all DAG nodes are terminal but goal status is {status!r}, expected 'complete'"
                    )
            elif tasks and all(task.get("status") == "done" for task in tasks) and status != "complete":
                # Legacy goals with no DAG, or with orphan task files outside the
                # DAG, fall back to task-file completeness (unchanged behavior).
                raise ValidationError(
                    f".shiki/goals/{goal_id}.json: active goal has all child tasks done but status is {status!r}, expected 'complete'"
                )

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

    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    print("Shiki validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
