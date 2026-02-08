#!/usr/bin/env python3
"""Claude Code Hook: Stop

Best-effort helper: when Claude session stops, create a draft report
and persist session state for recovery.

This is intentionally conservative:
- Only writes to .shiki/reports/ and .shiki/state/
- Never commits or pushes

If git is unavailable, it does nothing.
"""

import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _run(cmd: List[str], cwd: Path) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _detect_mode(config: Dict[str, Any]) -> str:
    mode = config.get("mode", "auto")
    if mode != "auto":
        return mode
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github"
    return "cli"


def _collect_active_tasks(tasks_dir: Path) -> List[Dict[str, Any]]:
    tasks = []
    if not tasks_dir.exists():
        return tasks
    for fp in sorted(tasks_dir.glob("*.json")):
        t = _read_json(fp)
        if t.get("status") in ("pending", "in_progress", "review", "blocked", "failed"):
            tasks.append({
                "task_id": t.get("id", fp.stem),
                "status": t.get("status"),
                "assigned_to": t.get("assigned_to"),
                "claimed_by": t.get("claimed_by"),
            })
    return tasks


def main() -> int:
    # Read input but ignore; hook protocol requires JSON stdin sometimes.
    try:
        _ = json.load(sys.stdin)
    except Exception:
        pass

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    shiki = project_dir / ".shiki"
    shiki_reports = shiki / "reports"
    shiki_state = shiki / "state"

    if not shiki_reports.exists():
        return 0

    # Ensure state directory
    shiki_state.mkdir(parents=True, exist_ok=True)

    now = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    # Read config for mode detection
    config_path = shiki / "config.yaml"
    config = _read_yaml(config_path) if config_path.exists() else {}
    mode = _detect_mode(config)

    # If git repo, capture changed files (staged+unstaged vs HEAD)
    changed = []
    branch = ""
    commit_sha = ""
    if (project_dir / ".git").exists():
        rc, out, err = _run(["git", "diff", "--name-only", "HEAD"], cwd=project_dir)
        if rc == 0 and out:
            changed = [line.strip() for line in out.splitlines() if line.strip()]

        rc2, branch_out, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project_dir)
        if rc2 == 0:
            branch = branch_out

        rc3, sha_out, _ = _run(["git", "rev-parse", "--short", "HEAD"], cwd=project_dir)
        if rc3 == 0:
            commit_sha = sha_out

    # Create session state for recovery
    active_tasks = _collect_active_tasks(shiki / "tasks")

    session_state = {
        "session_id": f"session-{now}",
        "mode": mode,
        "timestamp": _dt.datetime.now().isoformat(),
        "active_tasks": active_tasks,
        "git_state": {
            "branch": branch,
            "commit_sha": commit_sha,
            "dirty_files": changed[:50],
        },
    }

    # Detect theta phase from latest task
    for t in active_tasks:
        task_file = shiki / "tasks" / f"{t['task_id']}.json"
        if task_file.exists():
            task_data = _read_json(task_file)
            theta = task_data.get("theta_phase")
            if theta:
                session_state["theta_phase"] = theta
                break

    # Write session state
    try:
        state_path = shiki_state / f"session-{now}.json"
        state_path.write_text(
            json.dumps(session_state, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    # Create report if there are changes
    if changed:
        report_path = shiki_reports / f"SESSION-{now}.md"
        body = [
            f"# Session Report: {now}",
            f"",
            f"## Mode: {mode}",
            f"",
            f"## Changed files (git diff vs HEAD)",
            *[f"- {p}" for p in changed],
            "",
            f"## Active Tasks: {len(active_tasks)}",
            *[f"- [{t['status']}] {t['task_id']} -> {t.get('assigned_to', '?')}" for t in active_tasks[:20]],
            "",
            "## Notes",
            "- (Fill in what was intended / what remains)",
        ]
        try:
            report_path.write_text("\n".join(body) + "\n", encoding="utf-8")
        except Exception:
            pass

    # Optionally inform Claude by printing a warning (non-blocking)
    warnings = []
    if changed:
        warnings.append(f"Draft report: .shiki/reports/SESSION-{now}.md")
    warnings.append(f"Session state saved: .shiki/state/session-{now}.json")
    print(json.dumps({"warning": " | ".join(warnings)}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
