#!/usr/bin/env python3
"""Claude Code Hook: SessionStart

Injects a short summary of current Shiki state into Claude context.
Supports dual-mode detection, session recovery, DAG status, and budget summary.

Output JSON:
  { "additionalContext": "..." }

If nothing to add, exit 0 with no output.
"""

import glob
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


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


def main() -> int:
    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    shiki = project_dir / ".shiki"
    if not shiki.exists():
        return 0

    parts: List[str] = []

    # Read config
    config_path = shiki / "config.yaml"
    config = _read_yaml(config_path) if config_path.exists() else {}

    # Mode detection
    mode = _detect_mode(config)
    parts.append(f"## Mode: {mode.upper()}")

    # Recovery detection
    state_dir = shiki / "state"
    if state_dir.exists():
        session_files = sorted(state_dir.glob("session-*.json"))
        if session_files:
            latest = _read_json(session_files[-1])
            if latest:
                active = latest.get("active_tasks", [])
                incomplete = [t for t in active if t.get("status") not in ("completed",)]
                if incomplete:
                    parts.append(f"## Recovery Available")
                    parts.append(f"- Previous session: {session_files[-1].name}")
                    parts.append(f"- Incomplete tasks: {len(incomplete)}")
                    theta = latest.get("theta_phase", "unknown")
                    parts.append(f"- Last theta phase: {theta}")
                    budget = latest.get("budget", {})
                    if budget:
                        remaining = budget.get("tokens_remaining", "?")
                        parts.append(f"- Budget remaining: {remaining} tokens")

    # Active plan (latest)
    plans_dir = shiki / "plans"
    if plans_dir.exists():
        plan_files = sorted(plans_dir.glob("*.md"))
        if plan_files:
            parts.append(f"## Latest PLAN\n- {plan_files[-1].name}")

    # Active tasks
    tasks_dir = shiki / "tasks"
    active: List[str] = []
    if tasks_dir.exists():
        for fp in sorted(tasks_dir.glob("*.json")):
            t = _read_json(fp)
            status = t.get("status")
            if status in ("pending", "in_progress", "review", "blocked", "failed"):
                theta = t.get("theta_phase", "")
                theta_tag = f" [θ:{theta}]" if theta else ""
                active.append(
                    f"- [{status}]{theta_tag} {t.get('id','?')}: "
                    f"{t.get('title','')}"
                    f" -> {t.get('assigned_to','?')}"
                )
    if active:
        parts.append("## Active Tasks\n" + "\n".join(active[:25]))
        if len(active) > 25:
            parts.append(f"(and {len(active)-25} more...)")

    # DAG status
    dag_dir = shiki / "dag"
    if dag_dir.exists():
        dag_files = sorted(dag_dir.glob("*.json"))
        if dag_files:
            latest_dag = _read_json(dag_files[-1])
            if latest_dag:
                dag_status = latest_dag.get("status", "unknown")
                nodes = latest_dag.get("nodes", [])
                completed = sum(1 for n in nodes if n.get("status") == "completed")
                total = len(nodes)
                parts.append(
                    f"## DAG Status\n"
                    f"- {dag_files[-1].name}: {dag_status} ({completed}/{total} nodes done)"
                )

    # Budget summary
    budget_config = config.get("github", {}).get("budget", {})
    if budget_config:
        max_session = budget_config.get("max_tokens_per_session", 0)
        if max_session:
            parts.append(f"## Budget\n- Session limit: {max_session:,} tokens")

    # Latest report
    reports_dir = shiki / "reports"
    if reports_dir.exists():
        rep_files = sorted(reports_dir.glob("*.md"))
        if rep_files:
            parts.append(f"## Latest Report\n- {rep_files[-1].name}")

    if not parts:
        return 0

    out = {"additionalContext": "\n\n".join(parts)}
    print(json.dumps(out, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
