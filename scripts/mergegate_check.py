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


TASK_ID = re.compile(r"\bT-[0-9]{4,}\b")
GOAL_ID = re.compile(r"\bG-[0-9]{4,}\b")


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


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

            ledger_entries = load_ledger_entries(target, task, warnings, blocking)
            ledger_text = "\n".join(ledger_entry_text(entry) for entry in ledger_entries)
            for skill in task.get("required_skills") or []:
                skill_name = str(skill).strip().lower()
                if skill_name and skill_name != "none" and skill_name not in ledger_text:
                    blocking.append(f"Required skill {skill} has no matching ledger evidence")

    cca = load_json(Path(args.cca_verdict))
    if cca:
        if cca.get("verdict") != "complete":
            blocking.append(f"CCA verdict is not complete: {cca.get('verdict')!r}")
        if pr and cca.get("head_sha") and pr.get("headRefOid") and cca.get("head_sha") != pr.get("headRefOid"):
            blocking.append("CCA head_sha does not match the current PR headRefOid")
        if cca.get("can_merge") is not True:
            warnings.append("CCA verdict did not set can_merge=true; MergeGate will rely on required checks and policy inputs")
        if task and task.get("status") not in {"review", "done"}:
            blocking.append(f"Task status must be review or done after CCA verdict; got {task.get('status')!r}")
        if pr and ledger_entries:
            pr_number = pr.get("number")
            pr_token = f"/pull/{pr_number}"
            pr_text = "\n".join(ledger_entry_text(entry) for entry in ledger_entries)
            if pr_number is not None and pr_token not in pr_text and f"pr #{pr_number}" not in pr_text:
                blocking.append(f"Task ledger evidence does not reference PR #{pr_number}")
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
