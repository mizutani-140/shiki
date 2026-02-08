#!/usr/bin/env python3
"""Log-Driven Development (LDD) logger hook.

Records Claude Code actions to .ai/logs/YYYY-MM-DD.md in LDD format.
Triggered on SessionStart and Stop events.

LDD format:
  - intent: What are we trying to achieve?
  - plan: Step-by-step approach
  - implementation: What was changed
  - verification: How we confirmed it works
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path


def get_project_root():
    """Find the project root by looking for .shiki/ directory."""
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".shiki").is_dir():
            return parent
    return cwd


def get_log_path(root: Path) -> Path:
    """Get today's log file path."""
    log_dir = root / ".ai" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    return log_dir / f"{today}.md"


def create_log_header(log_path: Path, root: Path):
    """Create log file with header if it doesn't exist."""
    if log_path.exists():
        return

    today = datetime.now().strftime("%Y-%m-%d")
    device = os.environ.get("DEVICE_IDENTIFIER", "unknown")
    project = root.name

    header = f"""# Log-Driven Development Log - {today}

**Device**: {device}
**Project**: {project}
**Date**: {today}

---

## codex_prompt_chain

**intent**:
**plan**:
**implementation**:
**verification**:

---

## tool_invocations

"""
    log_path.write_text(header, encoding="utf-8")


def append_log_entry(log_path: Path, entry_type: str, content: str):
    """Append a log entry to the log file."""
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"""### [{timestamp}]
- **type**: {entry_type}
- **content**: {content}

"""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


def on_session_start(root: Path):
    """Log session start event."""
    log_path = get_log_path(root)
    create_log_header(log_path, root)

    # Detect mode
    mode = "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "cli"

    # Check for active tasks
    tasks_dir = root / ".shiki" / "tasks"
    active_tasks = []
    if tasks_dir.is_dir():
        for f in tasks_dir.glob("*.json"):
            if f.name == ".keep":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("status") in ("in_progress", "pending"):
                    active_tasks.append(f"{data.get('id', '?')}: {data.get('title', '?')}")
            except (json.JSONDecodeError, KeyError):
                pass

    content = f"Session started (mode={mode})"
    if active_tasks:
        content += f", active tasks: {', '.join(active_tasks[:5])}"

    append_log_entry(log_path, "session_start", content)


def on_session_stop(root: Path):
    """Log session stop event."""
    log_path = get_log_path(root)
    if not log_path.exists():
        create_log_header(log_path, root)

    append_log_entry(log_path, "session_stop", "Session ended")


def on_command(root: Path, command: str):
    """Log a command execution."""
    log_path = get_log_path(root)
    if not log_path.exists():
        create_log_header(log_path, root)

    append_log_entry(log_path, "command", command)


def main():
    root = get_project_root()

    # Determine event type from args or environment
    event_type = sys.argv[1] if len(sys.argv) > 1 else "command"

    if event_type == "session_start":
        on_session_start(root)
    elif event_type == "session_stop":
        on_session_stop(root)
    elif event_type == "command":
        cmd = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "unknown"
        on_command(root, cmd)
    else:
        # Default: log as generic event
        content = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "unknown event"
        log_path = get_log_path(root)
        if not log_path.exists():
            create_log_header(log_path, root)
        append_log_entry(log_path, "event", content)


if __name__ == "__main__":
    main()
