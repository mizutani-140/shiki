#!/usr/bin/env python3
"""Deterministic MergeGate policy check for Shiki PRs."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path
from typing import Any

from shiki_schema import SchemaValidationError, validate_instance


TASK_ID = re.compile(r"\bT-[0-9]{4,}\b")
GOAL_ID = re.compile(r"\bG-[0-9]{4,}\b")
DEFAULT_REQUIRED_CHECKS = [
    "Validate Shiki mirror",
    "CCA verdict",
    "MergeGate metadata check",
]
SELF_CHECKS = {"MergeGate policy check"}
VERDICT_CHECKS = {"CCA verdict"}
PLACEHOLDER_CHECKS = {"shiki-required-checks"}


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_schema(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object schema")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def has_heading(body: str, heading: str) -> bool:
    return re.search(rf"^#+\s+{re.escape(heading)}\s*$", body, re.IGNORECASE | re.MULTILINE) is not None


def first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def changed_files(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def lock_pattern(lock: str) -> str | None:
    if not lock.startswith("path:"):
        return None
    pattern = lock.removeprefix("path:").strip()
    return pattern or None


def path_matches_lock(path: str, lock: str) -> bool:
    pattern = lock_pattern(lock)
    if pattern is None:
        return False
    candidates = {pattern}
    if pattern.endswith("/"):
        candidates.add(pattern + "**")
    elif pattern.endswith("/*"):
        candidates.add(pattern[:-1] + "**")
    for candidate in candidates:
        if path == candidate or fnmatch.fnmatch(path, candidate):
            return True
    return False


def ledger_entry_text(entry: dict[str, Any]) -> str:
    parts: list[str] = [
        str(entry.get("type") or ""),
        str(entry.get("summary") or ""),
        " ".join(str(value) for value in entry.get("evidence") or []),
        " ".join(str(value) for value in entry.get("links") or []),
    ]
    return "\n".join(parts).lower()


def load_task(target: Path, task_id: str) -> dict[str, Any] | None:
    return load_json(target / ".shiki" / "tasks" / f"{task_id}.json")


def load_goal(target: Path, goal_id: str) -> dict[str, Any] | None:
    return load_json(target / ".shiki" / "goals" / f"{goal_id}.json")


def blocking_checklist_failures(verdict: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for item in verdict.get("checklist") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if item.get("blocking") is True and status in {"fail", "insufficient_evidence"}:
            failures.append(str(item.get("id") or "<unknown>"))
    return failures


def validate_cca_contract(target: Path, cca: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        schema = load_schema(target / ".shiki" / "schemas" / "cca-verdict.schema.json")
        validate_instance(cca, schema)
    except (OSError, ValueError, SchemaValidationError) as error:
        errors.append(str(error))

    repair_packet = cca.get("repair_packet")
    if repair_packet is not None:
        try:
            repair_schema = load_schema(target / ".shiki" / "schemas" / "repair-packet.schema.json")
            validate_instance(repair_packet, repair_schema, path="$.repair_packet")
        except (OSError, ValueError, SchemaValidationError) as error:
            errors.append(str(error))

    return errors


def configured_required_checks(target: Path) -> list[str]:
    """Read mergegate.required_checks from .shiki/config.yaml without a YAML dependency."""
    config_path = target / ".shiki" / "config.yaml"
    if not config_path.exists():
        return DEFAULT_REQUIRED_CHECKS

    checks: list[str] = []
    in_mergegate = False
    in_required_checks = False
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if indent == 0:
            in_mergegate = stripped == "mergegate:"
            in_required_checks = False
            continue
        if in_mergegate and indent == 2:
            in_required_checks = stripped == "required_checks:"
            continue
        if in_mergegate and in_required_checks:
            if indent <= 2:
                in_required_checks = False
                continue
            if stripped.startswith("- "):
                check = stripped[2:].strip().strip("\"'")
                if check:
                    checks.append(check)
    return checks or DEFAULT_REQUIRED_CHECKS


def pr_label_names(pr: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for label in pr.get("labels") or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = label
        if name:
            names.add(str(name).strip().lower())
    return names


def status_checks(pr: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    for check in pr.get("statusCheckRollup") or []:
        if not isinstance(check, dict):
            continue
        name = check.get("name") or check.get("workflowName") or check.get("context")
        if name:
            checks[str(name)] = check
    return checks


def check_head_sha(check: dict[str, Any]) -> str | None:
    value = check.get("headSha") or check.get("head_sha") or check.get("sha")
    if value:
        return str(value)
    commit = check.get("commit")
    if isinstance(commit, dict):
        oid = commit.get("oid") or commit.get("sha")
        if oid:
            return str(oid)
    return None


def enforce_required_checks(pr: dict[str, Any], target: Path, blocking: list[str], warnings: list[str]) -> None:
    checks = status_checks(pr)
    head_sha = pr.get("headRefOid")
    required = [
        check
        for check in configured_required_checks(target)
        if check not in SELF_CHECKS and check not in VERDICT_CHECKS and check not in PLACEHOLDER_CHECKS
    ]
    for name in required:
        check = checks.get(name)
        if check is None:
            blocking.append(f"Required check {name} is missing from PR statusCheckRollup")
            continue
        status = str(check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        if status != "COMPLETED":
            warnings.append(f"Required check {name} is not completed in prepared rollup: status={status or 'UNKNOWN'}; relying on branch protection freshness")
        elif conclusion != "SUCCESS":
            blocking.append(f"Required check {name} is not successful: conclusion={conclusion or 'UNKNOWN'}")
        check_sha = check_head_sha(check)
        if head_sha and check_sha and check_sha != head_sha:
            blocking.append(f"Required check {name} head SHA {check_sha} does not match PR headRefOid {head_sha}")
        elif head_sha and not check_sha:
            warnings.append(f"Required check {name} did not expose a head SHA; relying on GitHub rollup freshness")


def review_approved(pr: dict[str, Any]) -> bool:
    if str(pr.get("reviewDecision") or "").upper() == "APPROVED":
        return True
    for review in pr.get("reviews") or []:
        if isinstance(review, dict) and str(review.get("state") or "").upper() == "APPROVED":
            return True
    checks = status_checks(pr)
    claude_review = checks.get("Claude review")
    if claude_review:
        status = str(claude_review.get("status") or "").upper()
        conclusion = str(claude_review.get("conclusion") or "").upper()
        return status == "COMPLETED" and conclusion == "SUCCESS"
    return False


def enforce_review_policy(pr: dict[str, Any], blocking: list[str]) -> None:
    review_decision = str(pr.get("reviewDecision") or "").upper()
    if review_decision == "CHANGES_REQUESTED":
        blocking.append("PR review requested changes")
    for review in pr.get("reviews") or []:
        if isinstance(review, dict) and str(review.get("state") or "").upper() == "CHANGES_REQUESTED":
            author = review.get("author") or {}
            login = author.get("login") if isinstance(author, dict) else author
            suffix = f" by {login}" if login else ""
            blocking.append(f"PR review requested changes{suffix}")
    for thread in pr.get("reviewThreads") or []:
        if isinstance(thread, dict) and thread.get("isResolved") is False:
            blocking.append("PR has unresolved review findings")

    labels = pr_label_names(pr)
    explicit_review_required = bool(labels.intersection({"review:required", "requires-review", "needs-review"}))
    if (explicit_review_required or review_decision == "REVIEW_REQUIRED") and not review_approved(pr):
        blocking.append("Required review is missing")


def guardian_approved(pr: dict[str, Any], ledger_entries: list[dict[str, Any]]) -> bool:
    labels = pr_label_names(pr)
    if labels.intersection({"guardian:approved", "approval:guardian", "risk:approved"}):
        return True
    ledger_text = "\n".join(ledger_entry_text(entry) for entry in ledger_entries)
    return "guardian approved" in ledger_text or "guardian approval" in ledger_text


def active_lock_conflicts(target: Path, task_id: str, locks: list[str], files: list[str]) -> list[str]:
    conflicts: list[str] = []
    directory = target / ".shiki" / "locks"
    if not directory.exists():
        return conflicts
    for path in sorted(directory.glob("*.json")):
        record = load_json(path)
        if not record or record.get("state") != "active" or record.get("task_id") == task_id:
            continue
        owner_task = record.get("task_id") or path.stem
        for other_lock in [str(lock) for lock in record.get("locks") or []]:
            if other_lock in locks:
                conflicts.append(f"Lock conflict: {other_lock} held by {owner_task}")
                continue
            for changed_file in files:
                if path_matches_lock(changed_file, other_lock) and any(path_matches_lock(changed_file, lock) for lock in locks):
                    conflicts.append(f"Lock conflict: {other_lock} held by {owner_task} overlaps {changed_file}")
                    break
    return conflicts


def load_ledger_entries(target: Path, task: dict[str, Any], warnings: list[str], blocking: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for ledger_id in task.get("ledger_evidence") or []:
        if not isinstance(ledger_id, str):
            blocking.append("Task ledger_evidence contains a non-string entry")
            continue
        path = target / ".shiki" / "ledger" / f"{ledger_id}.json"
        entry = load_json(path)
        if entry is None:
            blocking.append(f"Task ledger evidence {ledger_id} is missing at {path}")
            continue
        entries.append(entry)
    if not entries:
        blocking.append("Task has no ledger evidence entries")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Shiki MergeGate readiness")
    parser.add_argument("--target", default=".", help="Target repository path")
    parser.add_argument("--pr-json", default=".shiki/gha/pr.json")
    parser.add_argument("--cca-verdict", default=".shiki/gha/cca-verdict.json")
    parser.add_argument("--changed-files", default=".shiki/gha/changed-files.txt")
    parser.add_argument("--result-file", default=".shiki/gha/mergegate-result.json")
    parser.add_argument("--allow-missing-cca", action="store_true")
    args = parser.parse_args()

    target = Path(args.target).resolve()
    blocking: list[str] = []
    warnings: list[str] = []
    resolved_task_id: str | None = None
    resolved_goal_id: str | None = None

    pr = load_json(Path(args.pr_json))
    body = ""
    if pr:
        body = str(pr.get("body") or "")
        resolved_task_id = first_match(TASK_ID, body)
        resolved_goal_id = first_match(GOAL_ID, body)
        if not resolved_task_id:
            blocking.append("PR body does not contain a Shiki task id like T-0001")
        if not resolved_goal_id:
            blocking.append("PR body does not contain a Shiki goal id like G-0001")
        for heading in ["Scope", "Acceptance", "Evidence", "MergeGate"]:
            if heading.lower() not in body.lower() and not has_heading(body, heading):
                blocking.append(f"PR body is missing {heading} section")
    else:
        warnings.append(f"PR JSON not found at {args.pr_json}; skipping PR metadata checks")

    task: dict[str, Any] | None = None
    ledger_entries: list[dict[str, Any]] = []
    if resolved_task_id:
        task = load_task(target, resolved_task_id)
        if task is None:
            blocking.append(f"Task file not found for {resolved_task_id}")
        else:
            task_goal_id = str(task.get("goal_id") or "")
            if resolved_goal_id and task_goal_id != resolved_goal_id:
                blocking.append(f"PR goal id {resolved_goal_id} does not match task goal_id {task_goal_id}")
            if not load_goal(target, task_goal_id):
                blocking.append(f"Goal file not found for {task_goal_id}")

            if pr:
                pr_number = pr.get("number")
                expected_pr = task.get("expected_pr")
                if expected_pr is not None and pr_number is not None and expected_pr != pr_number:
                    blocking.append(f"Task expected_pr {expected_pr} does not match PR #{pr_number}")
                elif expected_pr is None:
                    warnings.append(f"Task {resolved_task_id} has no expected_pr recorded")

                head_ref = pr.get("headRefName")
                expected_branch = task.get("expected_branch")
                if expected_branch and head_ref and expected_branch != head_ref:
                    blocking.append(f"Task expected_branch {expected_branch!r} does not match PR head {head_ref!r}")

            for dependency_id in task.get("dependencies") or []:
                dependency = load_task(target, str(dependency_id))
                if dependency is None:
                    blocking.append(f"Task dependency {dependency_id} has no task file")
                elif dependency.get("status") != "done":
                    blocking.append(f"Task dependency {dependency_id} is not done: {dependency.get('status')!r}")

            files = changed_files(Path(args.changed_files))
            locks = [str(lock) for lock in task.get("locks") or []]
            if files and not locks:
                blocking.append(f"Task {resolved_task_id} has no locks but PR changes files")
            for path in files:
                if not any(path_matches_lock(path, lock) for lock in locks):
                    blocking.append(f"Changed file {path} is outside declared task locks")
            blocking.extend(active_lock_conflicts(target, resolved_task_id, locks, files))

            ledger_entries = load_ledger_entries(target, task, warnings, blocking)
            ledger_text = "\n".join(ledger_entry_text(entry) for entry in ledger_entries)
            for skill in task.get("required_skills") or []:
                skill_name = str(skill).strip().lower()
                if skill_name and skill_name != "none" and skill_name not in ledger_text:
                    blocking.append(f"Required skill {skill} has no matching ledger evidence")

    cca = load_json(Path(args.cca_verdict))
    if cca:
        for error in validate_cca_contract(target, cca):
            blocking.append(f"CCA verdict schema violation: {error}")
        if cca.get("verdict") != "complete":
            blocking.append(f"CCA verdict is not complete: {cca.get('verdict')!r}")
        if resolved_task_id and cca.get("task_id") != resolved_task_id:
            blocking.append(f"CCA task_id {cca.get('task_id')!r} does not match PR task id {resolved_task_id}")
        if resolved_goal_id and cca.get("goal_id") != resolved_goal_id:
            blocking.append(f"CCA goal_id {cca.get('goal_id')!r} does not match PR goal id {resolved_goal_id}")
        if task and cca.get("goal_id") != task.get("goal_id"):
            blocking.append(f"CCA goal_id {cca.get('goal_id')!r} does not match task goal_id {task.get('goal_id')!r}")
        if pr:
            if cca.get("pr") != pr.get("number"):
                blocking.append(f"CCA pr {cca.get('pr')!r} does not match PR #{pr.get('number')}")
            if not cca.get("head_sha"):
                blocking.append("CCA head_sha is missing")
            elif pr.get("headRefOid") and cca.get("head_sha") != pr.get("headRefOid"):
                blocking.append("CCA head_sha does not match the current PR headRefOid")
        if cca.get("can_merge") is not True:
            warnings.append("CCA verdict did not set can_merge=true; MergeGate will rely on required checks and policy inputs")
        failures = blocking_checklist_failures(cca)
        if failures:
            blocking.append("CCA verdict contains blocking failed checklist items: " + ", ".join(failures))
        acceptance = cca.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            blocking.append("CCA verdict acceptance evidence is empty")
        if task and task.get("status") not in {"review", "done"}:
            blocking.append(f"Task status must be review or done after CCA verdict; got {task.get('status')!r}")
        if pr and ledger_entries:
            pr_number = pr.get("number")
            pr_token = f"/pull/{pr_number}"
            pr_text = "\n".join(ledger_entry_text(entry) for entry in ledger_entries)
            if pr_number is not None and pr_token not in pr_text and f"pr #{pr_number}" not in pr_text:
                blocking.append(f"Task ledger evidence does not reference PR #{pr_number}")
        if pr:
            enforce_required_checks(pr, target, blocking, warnings)
            enforce_review_policy(pr, blocking)
            if task and task.get("risk_level") in {"high", "critical"} and not guardian_approved(pr, ledger_entries):
                blocking.append(f"Guardian approval is required for {task.get('risk_level')} risk task {task.get('id')}")
    elif not args.allow_missing_cca:
        blocking.append(f"CCA verdict file not found at {args.cca_verdict}")

    changed_files_path = Path(args.changed_files)
    if changed_files_path.exists():
        files = changed_files(changed_files_path)
        if any(path.startswith(".github/workflows/") for path in files):
            warnings.append("workflow files changed; ci:infra or Guardian review may be required")
        if any("auth" in path.lower() or "secret" in path.lower() for path in files):
            warnings.append("auth/secret-like paths changed; ci:security or Guardian review may be required")

    result = {
        "mergegate": "blocked" if blocking else "ready",
        "task_id": resolved_task_id,
        "goal_id": resolved_goal_id,
        "blocking_reasons": blocking,
        "warnings": warnings,
    }
    write_json(Path(args.result_file), result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
