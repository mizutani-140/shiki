#!/usr/bin/env python3
"""Claude Code Hook: PostToolUse (Edit|MultiEdit|Write)

Runs lightweight formatting / linting after a file edit.
Also validates contract/task file edits against JSON schemas.
This is best-effort: if tools are not installed, we only warn.

Expected input (stdin): JSON
Output: JSON with optional 'warning'
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def _run(cmd: list[str], cwd: Optional[str] = None) -> tuple[int, str, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _validate_against_schema(file_path: str, project_dir: str) -> list[str]:
    """Validate task/contract JSON against its schema."""
    warnings = []
    rel = os.path.relpath(file_path, project_dir)

    schema_map = {
        ".shiki/tasks/": ".shiki/schemas/task.schema.json",
        ".shiki/contracts/": ".shiki/schemas/contract.schema.json",
        ".shiki/dag/": ".shiki/schemas/dag.schema.json",
    }

    schema_path = None
    for prefix, s_path in schema_map.items():
        if rel.startswith(prefix) and file_path.endswith(".json"):
            schema_path = os.path.join(project_dir, s_path)
            break

    if not schema_path or not os.path.exists(schema_path):
        return warnings

    try:
        import jsonschema
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
        jsonschema.validate(data, schema)
    except ImportError:
        pass  # jsonschema not available, skip
    except json.JSONDecodeError as e:
        warnings.append(f"Invalid JSON in {rel}: {e}")
    except Exception as e:
        warnings.append(f"Schema validation failed for {rel}: {str(e)[:200]}")

    return warnings


def main() -> int:
    try:
        data: Dict[str, Any] = json.load(sys.stdin)
    except Exception:
        return 0

    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not isinstance(file_path, str) or not file_path:
        return 0

    # Use project dir if provided by Claude Code
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    warnings: list[str] = []

    # Schema validation for .shiki/ files
    warnings.extend(_validate_against_schema(file_path, project_dir))

    if file_path.endswith(".py"):
        if _which("ruff"):
            _run(["ruff", "format", file_path], cwd=project_dir)
            rc, out, err = _run(["ruff", "check", file_path], cwd=project_dir)
            if rc != 0:
                warnings.append(f"ruff check failed: {out[:200]} {err[:200]}")
        else:
            warnings.append("ruff not found. Install: pip install ruff")
    elif file_path.endswith((".ts", ".tsx", ".js", ".jsx")):
        # Best-effort: run prettier if available
        if _which("prettier"):
            rc, out, err = _run(["prettier", "--write", file_path], cwd=project_dir)
            if rc != 0:
                warnings.append(f"prettier failed: {out[:200]} {err[:200]}")
        else:
            # Many repos use npx; we avoid heavy installs in hooks.
            pass

    if warnings:
        print(json.dumps({"warning": " | ".join(warnings)}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
