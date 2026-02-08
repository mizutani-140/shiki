#!/usr/bin/env python3
"""Claude Code Hook: PostToolUse (TaskCreate|TaskUpdate)

Syncs Agent Teams task state to .shiki/tasks/*.json files.
This ensures the file-based bridge layer stays in sync with
the native Agent Teams task system.

Expected input (stdin): JSON
Output: JSON with optional 'warning'
"""

import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def main() -> int:
    try:
        data: Dict[str, Any] = json.load(sys.stdin)
    except Exception:
        return 0

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    tasks_dir = project_dir / ".shiki" / "tasks"

    if not tasks_dir.exists():
        tasks_dir.mkdir(parents=True, exist_ok=True)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    tool_result = data.get("tool_result") or {}

    # Extract task info from tool result
    result_text = ""
    if isinstance(tool_result, str):
        result_text = tool_result
    elif isinstance(tool_result, dict):
        result_text = json.dumps(tool_result)

    warnings = []

    if tool_name == "TaskCreate":
        # Try to extract task ID from result
        task_id_match = re.search(r"#(\d+)", result_text)
        if task_id_match:
            task_num = task_id_match.group(1)
            subject = tool_input.get("subject", "")
            description = tool_input.get("description", "")

            # Create a bridge task file
            bridge_task = {
                "id": f"AT-{task_num.zfill(4)}",
                "title": subject,
                "assigned_to": "claude-team",
                "status": "pending",
                "mode_origin": "cli",
                "notes": description,
                "created_at": _dt.datetime.now().isoformat(),
                "updated_at": _dt.datetime.now().isoformat(),
            }

            task_file = tasks_dir / f"AT-{task_num.zfill(4)}.json"
            try:
                task_file.write_text(
                    json.dumps(bridge_task, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except Exception as e:
                warnings.append(f"Failed to sync task: {e}")

    elif tool_name == "TaskUpdate":
        task_id = tool_input.get("taskId", "")
        if task_id:
            task_file = tasks_dir / f"AT-{task_id.zfill(4)}.json"
            if task_file.exists():
                try:
                    existing = json.loads(task_file.read_text(encoding="utf-8"))
                    # Update fields that were changed
                    if "status" in tool_input:
                        existing["status"] = tool_input["status"]
                    if "subject" in tool_input:
                        existing["title"] = tool_input["subject"]
                    if "description" in tool_input:
                        existing["notes"] = tool_input["description"]
                    if "owner" in tool_input:
                        existing["claimed_by"] = tool_input["owner"]
                    existing["updated_at"] = _dt.datetime.now().isoformat()
                    task_file.write_text(
                        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                except Exception as e:
                    warnings.append(f"Failed to sync task update: {e}")

    if warnings:
        print(json.dumps({"warning": " | ".join(warnings)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
