#!/usr/bin/env python3
"""Dependency-free Shiki repository readiness diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Literal

from shiki_config import (
    configured_required_checks,
    configured_required_code_owner_review,
    configured_required_review,
)
from shiki_contracts import DEFAULT_REQUIRED_CHECKS
from shiki_git import current_branch, existing_origin_url, is_git_repo
from shiki_evidence import CCA_EVIDENCE_MANIFEST_PATH
from shiki_installer import (
    INSTALL_STAMP_PATH,
    commit_is_ancestor,
    platform_commit,
    read_install_stamp,
)
from shiki_guardian import GUARDIAN_POLICY_PATH, GuardianPolicyError, load_guardian_policy, validate_guardian_policy
from shiki_manifest import ManifestError, load_manifest, manifest_required_directories, manifest_required_files, manifest_runtime_directories
from shiki_migrations import MIGRATION_STATE_PATH, migration_status
from shiki_state_classes import (
    class_policy,
    manifest_state_classes,
    state_class_summary,
    unknown_tracked_shiki_paths,
)
from shiki_process import ROOT, first_line, print_json, run
from shiki_provider import ProviderConfig, ProviderConfigError, github_env, provider_from_repo_json, remote_matches_provider
from shiki_runtime import claude_auth_status, codex_auth_status, github_auth_status, shiki_entrypoints_status
from shiki_runtime_registry import CONFIG_RUNTIME_ROLES, RuntimeRegistryError, get_runtime, runtime_names, runtime_registry_as_json, validate_runtime_role_assignment
from shiki_workflows import WorkflowParseError, load_yaml_model

DoctorStatus = Literal["pass", "warn", "fail", "skip"]


@dataclass(frozen=True)
class DoctorFinding:
    id: str
    status: DoctorStatus
    title: str
    summary: str
    remediation: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _finding(
    finding_id: str,
    status: DoctorStatus,
    title: str,
    summary: str,
    remediation: str = "",
    details: dict[str, Any] | None = None,
) -> DoctorFinding:
    return DoctorFinding(finding_id, status, title, summary, remediation, details or {})


def _summary(findings: list[DoctorFinding]) -> dict[str, int]:
    return {
        status: sum(1 for finding in findings if finding.status == status)
        for status in ("pass", "warn", "fail", "skip")
    }


def _overall_status(findings: list[DoctorFinding]) -> DoctorStatus:
    summary = _summary(findings)
    if summary["fail"]:
        return "fail"
    if summary["warn"]:
        return "warn"
    return "pass"


def _config_model(target: Path) -> dict[str, Any]:
    path = target / ".shiki" / "config.yaml"
    try:
        return load_yaml_model(path)
    except FileNotFoundError as error:
        raise WorkflowParseError(f"{path}: missing config") from error


def _repo_config(target: Path) -> tuple[ProviderConfig | None, DoctorFinding | None]:
    path = target / ".shiki" / "repo.json"
    if not path.exists():
        return None, _finding(
            "doctor.provider.repo_json",
            "warn",
            "Repository provider config",
            ".shiki/repo.json is missing; this target may be a legacy Shiki repository or not fully initialized.",
            "Run `shiki init TARGET --repo OWNER/NAME --execute` or record provider metadata for this repository.",
            {"path": str(path)},
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ProviderConfigError("repo.json must be a JSON object")
        config = provider_from_repo_json(data)
    except (OSError, json.JSONDecodeError, ProviderConfigError) as error:
        return None, _finding(
            "doctor.provider.repo_json",
            "fail",
            "Repository provider config",
            f".shiki/repo.json is invalid: {error}",
            "Repair .shiki/repo.json or rerun `shiki init --execute` with the intended provider options.",
            {"path": str(path)},
        )
    return config, _finding(
        "doctor.provider.repo_json",
        "pass",
        "Repository provider config",
        f"Provider config is valid for {config.repo}.",
        details={
            "provider": config.provider,
            "repo": config.repo,
            "host": config.host,
            "remote_protocol": config.protocol,
            "api_base_url": config.api_base_url,
        },
    )


def _entrypoint_findings(config: dict[str, Any]) -> list[DoctorFinding]:
    status = shiki_entrypoints_status()
    findings = [
        _finding(
            "doctor.entrypoint.cli",
            "pass" if status["entrypoints"]["cli"]["ready"] else "warn",
            "Shiki CLI entrypoint",
            "Shiki CLI entrypoint is available." if status["entrypoints"]["cli"]["ready"] else "Shiki CLI entrypoint is not installed on PATH.",
            status["entrypoints"]["cli"].get("remediation", ""),
            {"path": status["entrypoints"]["cli"].get("path")},
        )
    ]
    runtimes = config.get("runtimes") if isinstance(config.get("runtimes"), dict) else {}
    configured = set(runtimes.values()) if isinstance(runtimes, dict) else set()
    if {"codex", "codex-front"} & configured:
        codex = codex_auth_status()
        findings.append(
            _finding(
                "doctor.auth.codex",
                "pass" if codex["ready"] else "warn",
                "Codex CLI auth",
                "Codex CLI is installed and authenticated." if codex["ready"] else "Codex CLI is unavailable or unauthenticated.",
                codex.get("remediation", ""),
                {"installed": codex["installed"], "logged_in": codex["logged_in"]},
            )
        )
    else:
        findings.append(_finding("doctor.auth.codex", "skip", "Codex CLI auth", "Codex runtime is not configured for this target."))
    if "claude-code" in configured:
        claude = claude_auth_status()
        findings.append(
            _finding(
                "doctor.auth.claude",
                "pass" if claude["ready"] else "warn",
                "Claude CLI auth",
                "Claude CLI is installed and authenticated." if claude["ready"] else "Claude CLI is unavailable or unauthenticated.",
                claude.get("remediation", ""),
                {"installed": claude["installed"], "logged_in": claude["logged_in"]},
            )
        )
    else:
        findings.append(_finding("doctor.auth.claude", "skip", "Claude CLI auth", "Claude local runtime is not configured for this target."))
    github = github_auth_status()
    findings.append(
        _finding(
            "doctor.auth.github",
            "pass" if github["ready"] else "warn",
            "GitHub CLI auth",
            "GitHub CLI is installed and authenticated." if github["ready"] else "GitHub CLI is unavailable or unauthenticated.",
            github.get("remediation", ""),
            {"installed": github["installed"], "logged_in": github["logged_in"]},
        )
    )
    return findings


def _provider_findings(target: Path, provider_config: ProviderConfig | None) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    if provider_config is None:
        return findings
    findings.append(
        _finding(
            "doctor.provider.kind",
            "pass" if provider_config.provider == "github" else "fail",
            "Provider kind",
            f"Provider is {provider_config.provider}.",
            "Only provider=github is currently supported.",
        )
    )
    findings.append(
        _finding(
            "doctor.provider.remote_protocol",
            "pass" if provider_config.protocol in {"https", "ssh"} else "fail",
            "Provider remote protocol",
            f"Remote protocol is {provider_config.protocol}.",
            "Use `--remote-protocol https` or `--remote-protocol ssh`.",
        )
    )
    return findings


def _git_findings(target: Path, provider_config: ProviderConfig | None) -> list[DoctorFinding]:
    if not is_git_repo(target):
        return [
            _finding(
                "doctor.git.repository",
                "warn",
                "Git repository",
                "Target is not a git repository.",
                "Run `git init` or `shiki init TARGET --repo OWNER/NAME --execute`.",
            )
        ]
    findings = [_finding("doctor.git.repository", "pass", "Git repository", "Target is a git repository.")]
    branch = current_branch(target)
    findings.append(
        _finding(
            "doctor.git.current_branch",
            "pass" if branch else "warn",
            "Current branch",
            f"Current branch is {branch}." if branch else "Current branch could not be determined.",
            "Checkout a named branch before running Shiki operations." if not branch else "",
            {"branch": branch},
        )
    )
    origin = existing_origin_url(target)
    if not origin:
        findings.append(
            _finding(
                "doctor.git.origin",
                "warn",
                "Origin remote",
                "Origin remote is not configured.",
                "Run `git remote add origin ...` or rerun `shiki init --execute`.",
            )
        )
    elif provider_config is None:
        findings.append(
            _finding(
                "doctor.git.origin",
                "warn",
                "Origin remote",
                "Origin remote exists, but provider config is missing so it cannot be checked.",
                "Add .shiki/repo.json provider metadata.",
                {"origin": origin},
            )
        )
    else:
        matches = remote_matches_provider(origin, provider_config)
        findings.append(
            _finding(
                "doctor.git.origin",
                "pass" if matches else "fail",
                "Origin remote",
                "Origin remote matches provider config." if matches else "Origin remote does not match provider config.",
                "Use `--adopt-existing-repo` only when intentionally adopting this origin." if not matches else "",
                {"origin": origin, "repo": provider_config.repo, "host": provider_config.host},
            )
        )
    findings.append(_worktree_registry_finding(target))
    return findings


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _worktree_registry_finding(target: Path) -> DoctorFinding:
    """Unregistered *Shiki task* worktrees are a non-negotiable MergeGate block.

    Shiki allocates every task worktree under ``<main-checkout-parent>/.worktrees``
    and records it in ``.shiki/worktrees``. Such a worktree with no record is a
    real governance orphan and fails.

    A git worktree that shares this gitdir but is NOT under the Shiki worktree
    root carries no Shiki task — e.g. the coordinator's own session worktree,
    which every dispatched task worktree sees via the shared gitdir, or an
    unrelated developer worktree. It is reported for information only, never as a
    failure: a dispatched implementer cannot register it and must not be blocked
    by it.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(target),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return _finding("doctor.worktrees.unregistered", "warn", "Worktree registry", "Could not list git worktrees.")
    physical = [
        Path(line.split(" ", 1)[1]).resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    ]
    if not physical:
        return _finding("doctor.worktrees.unregistered", "pass", "Worktree registry", "No git worktrees to check.")
    # `git worktree list` always emits the main working tree first; Shiki
    # allocates task worktrees as siblings of it under `.worktrees`.
    main_checkout = physical[0]
    shiki_worktree_root = (main_checkout.parent / ".worktrees").resolve()
    registered = {main_checkout, target.resolve()}
    registry = target / ".shiki" / "worktrees"
    if registry.exists():
        for record_path in registry.glob("*.json"):
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            recorded = record.get("path")
            if recorded:
                registered.add(Path(recorded).expanduser().resolve())
    orphaned: list[str] = []
    informational: list[str] = []
    for path in physical[1:]:
        if path in registered:
            continue
        if _is_within(path, shiki_worktree_root):
            orphaned.append(str(path))
        else:
            informational.append(str(path))
    orphaned.sort()
    informational.sort()
    if orphaned:
        summary = f"Unregistered Shiki task worktrees: {', '.join(orphaned)}."
    else:
        summary = "All Shiki task worktrees are registered in .shiki/worktrees."
        if informational:
            summary += f" Ignoring {len(informational)} non-Shiki worktree(s) that carry no Shiki task."
    return _finding(
        "doctor.worktrees.unregistered",
        "fail" if orphaned else "pass",
        "Worktree registry",
        summary,
        "Remove the worktree (`git worktree remove`) or register it with `shiki worktree allocate`." if orphaned else "",
        {"unregistered_task_worktrees": orphaned, "informational_worktrees": informational},
    )


def _workflow_findings(target: Path) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    workflow_dir = target / ".github" / "workflows"
    required_files = [
        "shiki-validate.yml",
        "shiki-cca-completion.yml",
        "shiki-mergegate.yml",
        "shiki-claude-review.yml",
        "shiki-orchestrator.yml",
    ]
    missing = [filename for filename in required_files if not (workflow_dir / filename).is_file()]
    findings.append(
        _finding(
            "doctor.workflows.required_files",
            "pass" if not missing else "fail",
            "Required workflow files",
            "Required workflow files are present." if not missing else f"Missing required workflow files: {', '.join(missing)}",
            "Install or restore Shiki workflow files." if missing else "",
            {"missing": missing},
        )
    )
    job_names: set[str] = set()
    parse_errors: list[str] = []
    for path in workflow_dir.glob("*.yml"):
        try:
            model = load_yaml_model(path)
        except (OSError, WorkflowParseError) as error:
            parse_errors.append(str(error))
            continue
        jobs = model.get("jobs")
        if isinstance(jobs, dict):
            for job in jobs.values():
                if isinstance(job, dict) and isinstance(job.get("name"), str):
                    job_names.add(job["name"])
    required_checks = configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
    missing_checks = [check for check in required_checks if check not in job_names]
    findings.append(
        _finding(
            "doctor.checks.required_checks",
            # required_checks can no longer be empty: configured_required_checks
            # falls back to DEFAULT_REQUIRED_CHECKS, so the old "empty means fail"
            # arm is unreachable and has been dropped rather than left to mislead.
            "pass" if not missing_checks else "fail",
            "Required check names",
            "Required checks match workflow job display names." if not missing_checks else "Required checks do not match workflow job display names.",
            "Align .shiki/config.yaml mergegate.required_checks with workflow job names." if missing_checks else "",
            {"required_checks": required_checks, "missing": missing_checks, "workflow_parse_errors": parse_errors},
        )
    )
    findings.append(
        _finding(
            "doctor.workflows.node24_policy",
            "pass" if not parse_errors else "warn",
            "Workflow structural parse",
            "Workflow files parse with the dependency-free parser." if not parse_errors else "Some workflow files could not be parsed.",
            "Run `python3 scripts/validate_shiki.py` for the full workflow contract error." if parse_errors else "",
            {"errors": parse_errors},
        )
    )
    return findings


def _codeowners_findings(target: Path) -> list[DoctorFinding]:
    try:
        from shiki_contracts import (
            CODEOWNERS_CRITICAL_PATHS,
            CODEOWNERS_PATH,
            codeowners_required_owner,
        )
    except Exception as error:  # pragma: no cover - import contract failure is surfaced as a finding.
        return [_finding("doctor.codeowners.coverage", "fail", "CODEOWNERS governance", f"Could not import CODEOWNERS contract: {error}")]
    # Resolve the required owner per target (falls back to the documented
    # CODEOWNERS_REQUIRED_OWNER constant when no owner can be resolved), so a
    # foreign target is judged against its own owner rather than this maintainer.
    required_owner = codeowners_required_owner(target)
    path = target / CODEOWNERS_PATH
    if not path.exists():
        return [
            _finding(
                "doctor.codeowners.coverage",
                "fail",
                "CODEOWNERS governance",
                f"{CODEOWNERS_PATH} is missing.",
                "Restore CODEOWNERS governance for critical Shiki paths.",
            )
        ]
    coverage: dict[str, set[str]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            coverage.setdefault(parts[0], set()).update(parts[1:])
    missing = []
    wrong_owner = []
    for critical_path in CODEOWNERS_CRITICAL_PATHS:
        owners = coverage.get(critical_path)
        if not owners:
            missing.append(critical_path)
        elif required_owner not in owners:
            wrong_owner.append(critical_path)
    ok = not missing and not wrong_owner
    return [
        _finding(
            "doctor.codeowners.coverage",
            "pass" if ok else "fail",
            "CODEOWNERS governance",
            "CODEOWNERS covers critical Shiki paths." if ok else "CODEOWNERS coverage is incomplete for critical Shiki paths.",
            f"Ensure each critical path is owned by {required_owner}." if not ok else "",
            {"missing": missing, "wrong_owner": wrong_owner, "required_owner": required_owner},
        )
    ]


def _manifest_findings(target: Path) -> list[DoctorFinding]:
    try:
        manifest = load_manifest(target)
    except ManifestError as error:
        return [_finding("doctor.manifest.layout", "fail", "Shiki manifest layout", str(error), "Restore .shiki/manifest.json.")]
    missing_dirs = [relative for relative in manifest_required_directories(manifest) if not (target / relative).is_dir()]
    missing_files = [relative for relative in manifest_required_files(manifest) if not (target / relative).is_file()]
    tracked_runtime = []
    for relative in manifest_runtime_directories(manifest):
        if not (target / relative).exists():
            continue
        tracked = subprocess.run(["git", "ls-files", "--", relative], cwd=str(target), text=True, capture_output=True, check=False)
        if tracked.returncode == 0 and tracked.stdout.strip():
            tracked_runtime.append(relative)
    ok = not missing_dirs and not missing_files and not tracked_runtime
    return [
        _finding(
            "doctor.manifest.layout",
            "pass" if ok else "fail",
            "Shiki manifest layout",
            "Manifest-required directories/files are present and runtime-only evidence is not committed."
            if ok
            else "Manifest-required layout is incomplete or runtime-only evidence is present.",
            "Run `python3 scripts/validate_shiki.py` for exact manifest drift and repair the listed files." if not ok else "",
            {"missing_directories": missing_dirs, "missing_files": missing_files, "runtime_directories_with_files": tracked_runtime},
        )
    ]


def _state_class_findings(target: Path) -> list[DoctorFinding]:
    try:
        manifest = load_manifest(target)
    except ManifestError as error:
        return [_finding("doctor.state_classes.manifest", "fail", "Shiki state classes", str(error), "Restore .shiki/manifest.json.")]

    state_classes = manifest_state_classes(manifest)
    summary = state_class_summary(manifest)
    missing_required = [
        state_class
        for state_class in ("append-only-evidence", "governance-policy", "migration-state", "workflow-runtime-evidence")
        if state_class not in state_classes
    ]
    manifest_ok = bool(state_classes) and not missing_required
    findings = [
        _finding(
            "doctor.state_classes.manifest",
            "pass" if manifest_ok else "fail",
            "Shiki state class manifest",
            "State classes are loaded and required trust classes exist." if manifest_ok else "State class manifest is incomplete.",
            "Run `python3 scripts/validate_shiki.py` and repair .shiki/manifest.json." if not manifest_ok else "",
            {"state_classes": sorted(state_classes), "missing_required": missing_required, "summary": summary},
        )
    ]

    tracked = subprocess.run(["git", "ls-files", "-z", "--", ".shiki"], cwd=str(target), text=True, capture_output=True, check=False)
    tracked_paths = [path for path in tracked.stdout.split("\0") if path] if tracked.returncode == 0 else []
    unknown = unknown_tracked_shiki_paths(target, manifest, tracked_paths)
    findings.append(
        _finding(
            "doctor.state_classes.unknown_paths",
            "pass" if not unknown else "fail",
            "Unknown tracked .shiki paths",
            "Tracked .shiki paths are represented by the manifest." if not unknown else "Tracked .shiki paths are missing manifest state classes.",
            "Add manifest entries or remove untrusted tracked paths." if unknown else "",
            {"unknown_paths": unknown},
        )
    )

    runtime_classes = {"workflow-runtime-evidence", "cache", "local-only"}
    runtime_failures = [
        state_class
        for state_class in runtime_classes
        if class_policy(state_class, manifest).get("tracked") is True
    ]
    runtime_committed: list[str] = []
    for relative in manifest_runtime_directories(manifest):
        result = subprocess.run(["git", "ls-files", "--", relative], cwd=str(target), text=True, capture_output=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            runtime_committed.append(relative)
    runtime_ok = not runtime_failures and not runtime_committed
    findings.append(
        _finding(
            "doctor.state_classes.runtime_only",
            "pass" if runtime_ok else "fail",
            "Runtime-only state classes",
            "Runtime-only/cache/local-only classes are not committed." if runtime_ok else "Runtime-only state class policy is violated.",
            "Remove committed runtime/cache/local-only files and repair manifest policies." if not runtime_ok else "",
            {"runtime_policy_failures": runtime_failures, "runtime_directories_with_files": runtime_committed},
        )
    )

    append_ok = "append-only-evidence" in state_classes and ".shiki/ledger" in summary.get("append-only-evidence", [])
    findings.append(
        _finding(
            "doctor.state_classes.append_only",
            "pass" if append_ok else "fail",
            "Append-only evidence state class",
            ".shiki/ledger is classified as append-only-evidence." if append_ok else ".shiki/ledger append-only classification is missing.",
            "Repair .shiki/manifest.json state_class for .shiki/ledger." if not append_ok else "",
            {"append_only_paths": summary.get("append-only-evidence", [])},
        )
    )
    return findings


def _migration_findings(target: Path) -> list[DoctorFinding]:
    status = migration_status(target)
    registry_errors = [error for error in status["errors"] if "registry" in error or "dependency" in error or "duplicate migration id" in error]
    findings = [
        _finding(
            "doctor.migrations.registry",
            "pass" if not registry_errors else "fail",
            "Migration registry",
            "Migration registry imports and dependencies are valid." if not registry_errors else "Migration registry validation failed.",
            "Repair scripts/shiki_migrations.py registry IDs and dependencies." if registry_errors else "",
            {"registry_ids": status["registry_ids"], "errors": registry_errors},
        )
    ]
    state_errors = [error for error in status["errors"] if error not in registry_errors]
    findings.append(
        _finding(
            "doctor.migrations.state",
            "pass" if status["state_exists"] and not state_errors and not status["unknown_applied"] else "fail",
            "Migration state",
            f"{MIGRATION_STATE_PATH} is present and valid."
            if status["state_exists"] and not state_errors and not status["unknown_applied"]
            else f"{MIGRATION_STATE_PATH} is missing or invalid.",
            "Run `shiki migrate status --target .` and repair migration state before relying on repository-local migration evidence."
            if state_errors or status["unknown_applied"] or not status["state_exists"]
            else "",
            {
                "state_path": status["state_path"],
                "state_exists": status["state_exists"],
                "unknown_applied": status["unknown_applied"],
                "errors": state_errors,
            },
        )
    )
    findings.append(
        _finding(
            "doctor.migrations.pending",
            "pass" if status["pending_count"] == 0 else "warn",
            "Pending migrations",
            "No pending migrations." if status["pending_count"] == 0 else f"{status['pending_count']} migration(s) are pending.",
            "Run `shiki migrate plan --target .` and apply with `--execute` when the plan is accepted." if status["pending_count"] else "",
            {"pending": status["pending"], "pending_count": status["pending_count"]},
        )
    )
    return findings


def _guardian_findings(target: Path) -> list[DoctorFinding]:
    try:
        policy = load_guardian_policy(target)
    except GuardianPolicyError as error:
        return [
            _finding(
                "doctor.guardian.policy",
                "fail",
                "Guardian policy",
                str(error),
                f"Restore {GUARDIAN_POLICY_PATH} before relying on Guardian approval checks.",
            )
        ]
    errors = validate_guardian_policy(policy)
    findings = [
        _finding(
            "doctor.guardian.policy",
            "pass" if not errors else "fail",
            "Guardian policy",
            "Guardian policy parses and validates." if not errors else "Guardian policy is invalid.",
            "Run `python3 scripts/validate_shiki.py` and repair Guardian policy drift." if errors else "",
            {"path": GUARDIAN_POLICY_PATH, "errors": errors, "applies_to_risk": list(policy.applies_to_risk)},
        )
    ]
    approver_errors: list[str] = []
    if not policy.users and not policy.teams:
        approver_errors.append("no Guardian users or teams configured")
    findings.append(
        _finding(
            "doctor.guardian.approvers",
            "pass" if not approver_errors else "fail",
            "Guardian approvers",
            "Guardian users/teams are configured." if not approver_errors else "Guardian approvers are incomplete.",
            f"Add at least one user or team to {GUARDIAN_POLICY_PATH}." if approver_errors else "",
            {"users": list(policy.users), "teams": list(policy.teams), "errors": approver_errors},
        )
    )
    solo_ok = (not policy.solo_maintainer_enabled) or (policy.allow_pr_author_as_guardian and bool(policy.solo_maintainer_rationale))
    findings.append(
        _finding(
            "doctor.guardian.solo_maintainer",
            "pass" if solo_ok else "fail",
            "Guardian solo maintainer policy",
            "Solo maintainer policy is explicit." if solo_ok else "Solo maintainer policy is incomplete.",
            "Set explicit allow_pr_author_as_guardian and rationale, or disable solo maintainer mode." if not solo_ok else "",
            {
                "enabled": policy.solo_maintainer_enabled,
                "allow_pr_author_as_guardian": policy.allow_pr_author_as_guardian,
                "has_rationale": bool(policy.solo_maintainer_rationale),
                "review_bridge_counts_as_guardian": policy.github_actions_review_bridge_counts_as_guardian,
                "claude_counts_as_guardian": policy.advisory_claude_review_counts_as_guardian,
            },
        )
    )
    return findings


def _evidence_integrity_findings(target: Path) -> list[DoctorFinding]:
    schema = target / ".shiki" / "schemas" / "cca-evidence-manifest.schema.json"
    workflow = target / ".github" / "workflows" / "shiki-cca-completion.yml"
    mergegate = target / "scripts" / "mergegate_check.py"
    workflow_text = workflow.read_text(encoding="utf-8") if workflow.is_file() else ""
    mergegate_text = mergegate.read_text(encoding="utf-8") if mergegate.is_file() else ""
    failures = []
    if not schema.is_file():
        failures.append("CCA evidence manifest schema is missing")
    if "Build CCA evidence manifest" not in workflow_text or CCA_EVIDENCE_MANIFEST_PATH not in workflow_text:
        failures.append("CCA workflow does not build the evidence manifest before artifact upload")
    if "--cca-evidence-manifest" not in workflow_text or "--expected-repository" not in workflow_text:
        failures.append("CCA workflow does not pass manifest inputs to MergeGate")
    if "validate_cca_evidence_manifest" not in mergegate_text or "--cca-evidence-manifest" not in mergegate_text:
        failures.append("MergeGate does not validate the CCA evidence manifest")
    return [
        _finding(
            "doctor.evidence_integrity.manifest",
            "pass" if not failures else "fail",
            "CCA evidence manifest",
            "CCA evidence manifest schema and workflow/MergeGate wiring are present."
            if not failures
            else "CCA evidence manifest wiring is incomplete.",
            "Run `python3 scripts/validate_shiki.py` and repair evidence-integrity wiring." if failures else "",
            {"manifest_path": CCA_EVIDENCE_MANIFEST_PATH, "failures": failures},
        )
    ]


def _runtime_findings(target: Path, config: dict[str, Any]) -> list[DoctorFinding]:
    findings = [
        _finding(
            "doctor.runtime.registry",
            "pass",
            "Runtime registry",
            "Runtime registry imports and declares supported runtimes.",
            details={"runtimes": list(runtime_names())},
        )
    ]
    runtimes = config.get("runtimes")
    errors: list[str] = []
    if not isinstance(runtimes, dict):
        errors.append(".shiki/config.yaml runtimes must be a mapping")
    else:
        for role in CONFIG_RUNTIME_ROLES:
            runtime_name = runtimes.get(role)
            if not isinstance(runtime_name, str):
                errors.append(f"runtimes.{role} must be a runtime name")
                continue
            try:
                validate_runtime_role_assignment(role, runtime_name)
            except RuntimeRegistryError as error:
                errors.append(f"runtimes.{role}: {error}")
            try:
                descriptor = get_runtime(runtime_name)
            except RuntimeRegistryError:
                continue
            if descriptor.requires_rationale and f"{role}_rationale" not in runtimes:
                errors.append(f"runtimes.{role} uses {runtime_name!r} without explicit rationale")
    for task_path in sorted((target / ".shiki" / "tasks").glob("*.json")):
        try:
            data = json.loads(task_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            errors.append(f"{task_path}: invalid JSON: {error}")
            continue
        runtime_name = data.get("assigned_runtime")
        if isinstance(runtime_name, str):
            try:
                get_runtime(runtime_name)
            except RuntimeRegistryError as error:
                errors.append(f"{task_path}: assigned_runtime: {error}")
    findings.append(
        _finding(
            "doctor.runtime.assignments",
            "pass" if not errors else "fail",
            "Runtime assignments",
            "Config and task runtime assignments are valid." if not errors else "Runtime assignment validation failed.",
            "Use a supported runtime from the registry or add explicit rationale for `other`." if errors else "",
            {"errors": errors},
        )
    )
    return findings


def _contract_finding(target: Path) -> DoctorFinding:
    script = target / "scripts" / "validate_shiki.py"
    if not script.exists():
        return _finding(
            "doctor.contract.validate_shiki",
            "skip",
            "Shiki contract validation",
            "scripts/validate_shiki.py is not present in this target.",
            "Install Shiki validation files before running contract diagnostics.",
        )
    result = subprocess.run(["python3", str(script)], cwd=str(target), text=True, capture_output=True, check=False)
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    return _finding(
        "doctor.contract.validate_shiki",
        "pass" if result.returncode == 0 else "fail",
        "Shiki contract validation",
        "validate_shiki.py passed." if result.returncode == 0 else "validate_shiki.py failed.",
        "Run `python3 scripts/validate_shiki.py` and repair the first reported contract drift." if result.returncode != 0 else "",
        {"returncode": result.returncode, "output": output[:4000]},
    )


def _gh(args: list[str], config: ProviderConfig) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(github_env(config))
    return subprocess.run(["gh", *args], text=True, capture_output=True, check=False, env=env)


def _branch_protection_read_failure(result: subprocess.CompletedProcess[str]) -> DoctorFinding:
    """Classify a failed branch-protection read into warn vs fail.

    A 403 (or any explicit permission/forbidden failure) means Shiki could not
    READ the protection state, not that the branch is unprotected -> warn. A 404
    / "branch not protected" / not-found means the branch genuinely has no
    protection, so MergeGate is toothless -> fail. Any other non-zero outcome
    fails closed too: doctor must never imply protection exists when it could not
    confirm it, and a not-protected default branch is the exact silent gap this
    check exists to catch.
    """
    text = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
    lowered = text.lower()
    match = re.search(r"http[ /]?(\d{3})", lowered)
    code = match.group(1) if match else None
    permission = (
        code == "403"
        or "permission" in lowered
        or "forbidden" in lowered
        or "not accessible" in lowered
    )
    if permission:
        return _finding(
            "doctor.github.branch_protection",
            "warn",
            "Branch protection",
            "Could not read branch protection (insufficient permission).",
            "Grant branch protection read permission, then rerun doctor.",
            {"error": first_line(result.stderr), "http_status": code},
        )
    return _finding(
        "doctor.github.branch_protection",
        "fail",
        "Branch protection",
        "Default branch is not protected or protection could not be confirmed.",
        "Configure branch protection through Shiki init/start (`--protect`) or repair repository rules.",
        {"error": first_line(result.stderr), "http_status": code},
    )


def _online_findings(config: ProviderConfig | None, target: Path) -> list[DoctorFinding]:
    if config is None:
        return [
            _finding(
                "doctor.github.online",
                "skip",
                "GitHub online checks",
                "Online checks were skipped because provider config is unavailable.",
                "Add .shiki/repo.json provider metadata.",
            )
        ]
    if shutil.which("gh") is None:
        return [_finding("doctor.github.online", "warn", "GitHub online checks", "GitHub CLI is not installed.", "Install GitHub CLI and run `gh auth login`.")]
    findings: list[DoctorFinding] = []
    auth = _gh(["auth", "status"], config)
    findings.append(
        _finding(
            "doctor.github.auth_status",
            "pass" if auth.returncode == 0 else "warn",
            "GitHub CLI online auth",
            "GitHub CLI auth check passed." if auth.returncode == 0 else "GitHub CLI auth check failed.",
            f"Run `gh auth login -h {config.host}`." if auth.returncode != 0 else "",
            {"host": config.host, "error": first_line(auth.stderr) or first_line(auth.stdout)},
        )
    )
    repo = _gh(["repo", "view", config.repo, "--json", "name,defaultBranchRef"], config)
    default_branch = "main"
    if repo.returncode == 0:
        try:
            data = json.loads(repo.stdout or "{}")
            branch = data.get("defaultBranchRef")
            if isinstance(branch, dict) and isinstance(branch.get("name"), str):
                default_branch = branch["name"]
        except json.JSONDecodeError:
            pass
    findings.append(
        _finding(
            "doctor.github.repo_exists",
            "pass" if repo.returncode == 0 else "fail",
            "GitHub repository",
            f"Repository {config.repo} exists." if repo.returncode == 0 else f"Repository {config.repo} could not be read.",
            "Create the repository or check GitHub permissions." if repo.returncode != 0 else "",
            {"repo": config.repo, "default_branch": default_branch, "error": first_line(repo.stderr)},
        )
    )
    secrets = _gh(["secret", "list", "--repo", config.repo], config)
    secret_names = {line.split()[0] for line in secrets.stdout.splitlines() if line.strip()} if secrets.returncode == 0 else set()
    secret_name = "CLAUDE_CODE_OAUTH_TOKEN"
    findings.append(
        _finding(
            "doctor.secrets.claude_code_oauth_token",
            "pass" if secrets.returncode == 0 and secret_name in secret_names else ("warn" if secrets.returncode != 0 else "fail"),
            "Claude Code OAuth secret",
            "Required Claude Code OAuth secret exists."
            if secrets.returncode == 0 and secret_name in secret_names
            else ("Could not determine whether required secrets exist." if secrets.returncode != 0 else "Required Claude Code OAuth secret is missing."),
            "Grant secret metadata access or set CLAUDE_CODE_OAUTH_TOKEN with `gh secret set`." if secrets.returncode != 0 or secret_name not in secret_names else "",
            {"checked": secrets.returncode == 0, "secret": secret_name, "configured": secret_name in secret_names if secrets.returncode == 0 else None},
        )
    )
    protection = _gh(["api", f"repos/{config.repo}/branches/{default_branch}/protection"], config)
    # Read policy through shiki_config, never restate it. These are the SAME
    # values that configure protection (branch_protection_review_count ->
    # protect_branch) and gate the merge (mergegate_check), so doctor cannot
    # judge a repository against a policy nobody else applies.
    required_checks = configured_required_checks(target, DEFAULT_REQUIRED_CHECKS)
    required_review = configured_required_review(target)
    required_code_owner = configured_required_code_owner_review(target)
    if protection.returncode != 0:
        findings.append(_branch_protection_read_failure(protection))
    else:
        try:
            data = json.loads(protection.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        contexts = data.get("required_status_checks", {}).get("contexts", []) if isinstance(data, dict) else []
        missing = [check for check in required_checks if check not in contexts]
        reviews = data.get("required_pull_request_reviews", {}) if isinstance(data, dict) else {}
        review_count = reviews.get("required_approving_review_count") if isinstance(reviews, dict) else None
        code_owner = reviews.get("require_code_owner_reviews") if isinstance(reviews, dict) else None
        failures = []
        if missing:
            failures.append(f"missing required checks: {', '.join(missing)}")
        if required_review and not isinstance(review_count, int):
            failures.append("required review count is unavailable")
        elif required_review and review_count < 1:
            failures.append("required review count is less than 1")
        # Code-owner review is a THREE-way comparison, not a one-way requirement
        # (SADR-0021). Under-enforcement fails: the operator asked for path-owner
        # governance and GitHub is not applying it. Over-enforcement only WARNS:
        # doctor cannot tell a deadlocked solo repository from a healthy
        # multi-maintainer one, because that turns on whether a code owner other
        # than the PR author exists — which is not in the protection payload.
        code_owner_warnings: list[str] = []
        if required_code_owner and code_owner is not True:
            failures.append("code-owner review is required by config but not enforced")
        elif not required_code_owner and code_owner is True:
            code_owner_warnings.append(
                "GitHub requires code-owner review but .shiki/config.yaml does not; "
                "if the only code owner is the PR author, every PR touching a CODEOWNERS "
                "path is unmergeable"
            )
        if failures:
            status = "fail"
        elif code_owner_warnings:
            status = "warn"
        else:
            status = "pass"
        if failures:
            remediation = "Rerun Shiki branch protection setup or repair repository rules."
        elif code_owner_warnings:
            remediation = (
                "Set defaults.required_code_owner_review: true to keep it, or rerun "
                "`shiki init --protect` to reconcile GitHub with the configured value."
            )
        else:
            remediation = ""
        findings.append(
            _finding(
                "doctor.github.branch_protection",
                status,
                "Branch protection",
                "Branch protection matches Shiki required checks/review policy." if status == "pass" else "Branch protection does not match Shiki policy.",
                remediation,
                {
                    "required_checks": required_checks,
                    "contexts": contexts,
                    "review_count": review_count,
                    "require_code_owner_reviews": code_owner,
                    "required_code_owner_review": required_code_owner,
                    "failures": failures,
                    "warnings": code_owner_warnings,
                },
            )
        )
    permissions = _gh(["api", f"repos/{config.repo}/actions/permissions/workflow"], config)
    if permissions.returncode != 0:
        findings.append(
            _finding(
                "doctor.github.workflow_permissions",
                "warn",
                "GitHub workflow permissions",
                "Could not read repository workflow permissions.",
                "Grant Actions permission read access or inspect repository Actions settings manually.",
                {"error": first_line(permissions.stderr)},
            )
        )
    else:
        try:
            data = json.loads(permissions.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        default_perm = data.get("default_workflow_permissions")
        can_approve = data.get("can_approve_pull_request_reviews")
        ok = default_perm == "read" and can_approve is True
        findings.append(
            _finding(
                "doctor.github.workflow_permissions",
                "pass" if ok else "fail",
                "GitHub workflow permissions",
                "Workflow permissions support read default and CCA Review Bridge approvals." if ok else "Workflow permissions do not satisfy Shiki CCA Review Bridge policy.",
                "Set default workflow permissions to read and allow GitHub Actions to approve pull requests." if not ok else "",
                {"default_workflow_permissions": default_perm, "can_approve_pull_request_reviews": can_approve},
            )
        )
    comments = _gh(["api", f"repos/{config.repo}/issues/comments?per_page=1"], config)
    events = _gh(["api", f"repos/{config.repo}/issues/events?per_page=1"], config)
    guardian_events_ok = comments.returncode == 0 and events.returncode == 0
    findings.append(
        _finding(
            "doctor.guardian.github_events",
            "pass" if guardian_events_ok else "warn",
            "Guardian GitHub evidence APIs",
            "GitHub issue comments/events APIs are readable."
            if guardian_events_ok
            else "Could not verify GitHub issue comments/events API access for Guardian evidence.",
            "Grant issue metadata read access or verify that workflows can call issue comments/events APIs."
            if not guardian_events_ok
            else "",
            {
                "comments_status": comments.returncode,
                "events_status": events.returncode,
                "comments_error": first_line(comments.stderr),
                "events_error": first_line(events.stderr),
            },
        )
    )
    return findings


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _install_stamp_content_finding(target: Path, stamp: dict[str, Any]) -> DoctorFinding:
    digests = stamp.get("digests")
    if not isinstance(digests, dict):
        return _finding(
            "doctor.install_stamp.content",
            "fail",
            "Shipped content drift",
            "Install stamp is missing its per-path content digests, so drift cannot be checked.",
            "Regenerate the install stamp by reinstalling with `--force`.",
            {"path": INSTALL_STAMP_PATH},
        )
    drifted: list[str] = []
    missing: list[str] = []
    for relative, expected in sorted(digests.items()):
        if not isinstance(relative, str) or not isinstance(expected, str):
            continue
        path = target / relative
        if not path.is_file():
            missing.append(relative)
        elif _sha256_file(path) != expected:
            drifted.append(relative)
    problems = sorted(drifted + missing)
    ok = not problems
    return _finding(
        "doctor.install_stamp.content",
        "pass" if ok else "warn",
        "Shipped content drift",
        "All stamped shipped paths match their recorded content digests."
        if ok
        else f"Shipped paths differ from their stamped content digests: {', '.join(problems)}.",
        "Re-run the install/upgrade to restore shipped content, or reconcile the local edits." if not ok else "",
        {"drifted": drifted, "missing": missing},
    )


def _install_stamp_commit_finding(target: Path, stamp: dict[str, Any]) -> DoctorFinding:
    commit = stamp.get("platform_commit")
    if not isinstance(commit, str) or not commit:
        return _finding(
            "doctor.install_stamp.platform_commit",
            "skip",
            "Platform commit lineage",
            "Install stamp records no platform commit, so lineage cannot be checked.",
            details={"path": INSTALL_STAMP_PATH},
        )
    head = platform_commit(ROOT)
    if head is None:
        return _finding(
            "doctor.install_stamp.platform_commit",
            "skip",
            "Platform commit lineage",
            "The running platform HEAD could not be resolved (not a git repository), so lineage cannot be checked.",
            details={"platform_commit": commit},
        )
    if commit == head:
        return _finding(
            "doctor.install_stamp.platform_commit",
            "pass",
            "Platform commit lineage",
            "Stamped platform commit matches the running platform HEAD.",
            details={"platform_commit": commit, "platform_head": head},
        )
    if commit_is_ancestor(ROOT, commit, head) is True:
        return _finding(
            "doctor.install_stamp.platform_commit",
            "pass",
            "Platform commit lineage",
            "Stamped platform commit is an ancestor of the running platform HEAD (an upgrade is available).",
            details={"platform_commit": commit, "platform_head": head},
        )
    return _finding(
        "doctor.install_stamp.platform_commit",
        "warn",
        "Platform commit lineage",
        f"Stamped platform commit {commit} is not an ancestor of the running platform HEAD {head}; "
        "the target is ahead of, or from a different lineage than, the running platform.",
        "Confirm you are running the platform version that produced this target, or reconcile the lineage.",
        {"platform_commit": commit, "platform_head": head},
    )


def _install_stamp_findings(target: Path) -> list[DoctorFinding]:
    """Report drift from the target's install stamp (absent stamp is never a pass)."""
    stamp_path = target / INSTALL_STAMP_PATH
    if not stamp_path.exists():
        return [
            _finding(
                "doctor.install_stamp.present",
                "warn",
                "Install version stamp",
                "No install version stamp is present; this target predates install stamping, "
                "so its provenance and shipped-content drift cannot be verified.",
                f"Refresh the install with `shiki install-target {target} --local-only --force` to write a stamp.",
                {"path": INSTALL_STAMP_PATH},
            ),
            _finding(
                "doctor.install_stamp.content",
                "skip",
                "Shipped content drift",
                "No install stamp, so shipped-content digests cannot be checked.",
                details={"path": INSTALL_STAMP_PATH},
            ),
            _finding(
                "doctor.install_stamp.platform_commit",
                "skip",
                "Platform commit lineage",
                "No install stamp, so the platform commit lineage cannot be checked.",
                details={"path": INSTALL_STAMP_PATH},
            ),
        ]
    stamp = read_install_stamp(target)
    if stamp is None:
        return [
            _finding(
                "doctor.install_stamp.present",
                "fail",
                "Install version stamp",
                "Install stamp is present but unreadable or not a JSON object.",
                "Regenerate the install stamp by reinstalling with `--force`.",
                {"path": INSTALL_STAMP_PATH},
            )
        ]
    findings = [
        _finding(
            "doctor.install_stamp.present",
            "pass",
            "Install version stamp",
            f"Install stamp records platform commit {stamp.get('platform_commit') or 'unknown'} "
            f"installed at {stamp.get('installed_at') or 'unknown'}.",
            details={
                "path": INSTALL_STAMP_PATH,
                "platform_commit": stamp.get("platform_commit"),
                "installed_at": stamp.get("installed_at"),
            },
        ),
        _install_stamp_content_finding(target, stamp),
        _install_stamp_commit_finding(target, stamp),
    ]
    return findings


def doctor_findings(target: Path, *, online: bool = False) -> list[DoctorFinding]:
    target = target.expanduser().resolve()
    findings: list[DoctorFinding] = []
    try:
        config = _config_model(target)
    except WorkflowParseError as error:
        config = {}
        findings.append(
            _finding(
                "doctor.config.shiki",
                "fail",
                "Shiki config",
                str(error),
                "Restore .shiki/config.yaml before running repository diagnostics.",
            )
        )
    else:
        findings.append(_finding("doctor.config.shiki", "pass", "Shiki config", ".shiki/config.yaml parses successfully."))
    findings.extend(_entrypoint_findings(config))
    provider_config, provider_finding = _repo_config(target)
    if provider_finding:
        findings.append(provider_finding)
    findings.extend(_provider_findings(target, provider_config))
    findings.extend(_git_findings(target, provider_config))
    findings.extend(_workflow_findings(target))
    findings.extend(_codeowners_findings(target))
    findings.extend(_manifest_findings(target))
    findings.extend(_state_class_findings(target))
    findings.extend(_migration_findings(target))
    findings.extend(_install_stamp_findings(target))
    findings.extend(_guardian_findings(target))
    findings.extend(_evidence_integrity_findings(target))
    findings.extend(_runtime_findings(target, config))
    findings.append(_contract_finding(target))
    if online:
        findings.extend(_online_findings(provider_config, target))
    else:
        findings.append(_finding("doctor.github.online", "skip", "GitHub online checks", "Online GitHub checks were skipped; pass --online to enable them."))
    return findings


def run_doctor(target: Path, *, json_output: bool = False, online: bool = False) -> dict[str, Any]:
    entrypoint_status = shiki_entrypoints_status()
    findings = doctor_findings(target, online=online)
    report = {
        "status": _overall_status(findings),
        "target": str(target.expanduser().resolve()),
        "summary": _summary(findings),
        "findings": [asdict(finding) for finding in findings],
        "root": entrypoint_status["root"],
        "config": entrypoint_status["config"],
        "entrypoints": entrypoint_status["entrypoints"],
        "runtimes": entrypoint_status["runtimes"],
        "runtime_registry": runtime_registry_as_json(),
        "usable_entrypoints": entrypoint_status["usable_entrypoints"],
        "blocking_reasons": entrypoint_status["blocking_reasons"],
        "note": entrypoint_status["note"],
    }
    return report


def print_doctor_report(report: dict[str, Any]) -> None:
    print("Shiki doctor")
    print(f"target: {report['target']}")
    print(f"status: {report['status']}")
    print(f"usable entrypoints: {', '.join(report['usable_entrypoints']) or 'none'}")
    print("")
    current_section = ""
    for finding in report["findings"]:
        section = finding["id"].split(".")[1]
        if section != current_section:
            current_section = section
            print(f"{section}:")
        print(f"- [{finding['status']}] {finding['title']}: {finding['summary']}")
        if finding.get("remediation"):
            print(f"  remediation: {finding['remediation']}")
    if report["blocking_reasons"]:
        print("")
        print("blocking reasons:")
        for reason in report["blocking_reasons"]:
            print(f"- {reason}")
    print("")
    print(report["note"])


def cmd_doctor(args: argparse.Namespace) -> int:
    target = Path(getattr(args, "target", ".")).expanduser().resolve()
    report = run_doctor(target, json_output=args.json, online=args.online)
    if args.json:
        print_json(report)
    else:
        print_doctor_report(report)
    if report["status"] == "fail":
        return 1
    if args.strict and report["status"] == "warn":
        return 1
    return 0
