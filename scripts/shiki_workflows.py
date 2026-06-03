"""Structured helpers for Shiki GitHub Actions workflow contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class WorkflowParseError(ValueError):
    """Raised when a workflow cannot be parsed safely by the bounded parser."""


def _strip_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
            continue
        if char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _indent_of(line: str) -> int:
    if "\t" in line:
        raise WorkflowParseError("tabs are not supported in Shiki workflow YAML")
    return len(line) - len(line.lstrip(" "))


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"|", ">"} or value.startswith("|") or value.startswith(">"):
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if value.isdigit():
        return int(value)
    if value.startswith("{") or value.startswith("["):
        raise WorkflowParseError(f"unsupported flow YAML value: {value!r}")
    return value


def _split_key_value(content: str) -> tuple[str, str | None]:
    if ":" not in content:
        raise WorkflowParseError(f"expected mapping entry, got {content!r}")
    key, value = content.split(":", 1)
    key = key.strip()
    if not key:
        raise WorkflowParseError(f"empty mapping key in {content!r}")
    value = _strip_comment(value)
    return key, value.strip() if value.strip() else None


def _next_significant(lines: list[tuple[int, str]], index: int) -> int:
    while index < len(lines):
        if lines[index][1].strip():
            return index
        index += 1
    return index


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    index = _next_significant(lines, index)
    if index >= len(lines):
        return {}, index
    current_indent, content = lines[index]
    if current_indent < indent:
        return {}, index
    if current_indent != indent:
        raise WorkflowParseError(f"unexpected indentation before {content!r}")
    if content.startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        index = _next_significant(lines, index)
        if index >= len(lines):
            break
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise WorkflowParseError(f"unexpected nested mapping line {content!r}")
        if content.startswith("- "):
            break

        key, value = _split_key_value(content)
        if value is None:
            next_index = _next_significant(lines, index + 1)
            if next_index >= len(lines) or lines[next_index][0] <= indent:
                result[key] = {}
                index += 1
                continue
            nested, index = _parse_block(lines, next_index, lines[next_index][0])
            result[key] = nested
            continue

        if value in {"|", ">"} or value.startswith("|") or value.startswith(">"):
            block_lines: list[str] = []
            index += 1
            while index < len(lines):
                block_indent, block_content = lines[index]
                if block_indent <= indent:
                    break
                block_lines.append(block_content)
                index += 1
            result[key] = "\n".join(block_lines)
            continue

        result[key] = _parse_scalar(value)
        index += 1
    return result, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        index = _next_significant(lines, index)
        if index >= len(lines):
            break
        current_indent, content = lines[index]
        if current_indent < indent:
            break
        if current_indent != indent or not content.startswith("- "):
            break
        item = content[2:].strip()
        if not item:
            next_index = _next_significant(lines, index + 1)
            if next_index >= len(lines) or lines[next_index][0] <= indent:
                result.append({})
                index += 1
                continue
            nested, index = _parse_block(lines, next_index, lines[next_index][0])
            result.append(nested)
            continue
        if ":" in item and not item.startswith(("'", '"')):
            key, value = _split_key_value(item)
            item_map: dict[str, Any] = {}
            if value is None:
                next_index = _next_significant(lines, index + 1)
                if next_index < len(lines) and lines[next_index][0] > indent:
                    nested, index = _parse_block(lines, next_index, lines[next_index][0])
                    item_map[key] = nested
                else:
                    item_map[key] = {}
                    index += 1
            else:
                item_map[key] = _parse_scalar(value)
                index += 1
            next_index = _next_significant(lines, index)
            if next_index < len(lines) and lines[next_index][0] > indent:
                extra, index = _parse_mapping(lines, next_index, lines[next_index][0])
                item_map.update(extra)
            result.append(item_map)
            continue
        result.append(_parse_scalar(_strip_comment(item)))
        index += 1
    return result, index


def load_yaml_model(path: Path) -> dict[str, Any]:
    lines: list[tuple[int, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = _strip_comment(raw_line.rstrip())
        if not stripped.strip():
            continue
        lines.append((_indent_of(stripped), stripped.strip()))
    if not lines:
        raise WorkflowParseError(f"{path}: empty YAML document")
    model, index = _parse_block(lines, 0, lines[0][0])
    if index < len(lines):
        raise WorkflowParseError(f"{path}: could not parse line {lines[index][1]!r}")
    if not isinstance(model, dict):
        raise WorkflowParseError(f"{path}: top-level YAML document must be a mapping")
    return model


def load_workflow_model(path: Path) -> dict[str, Any]:
    model = load_yaml_model(path)
    for key in ("name", "on", "permissions", "jobs"):
        if key not in model:
            raise WorkflowParseError(f"{path}: workflow missing top-level {key!r}")
    if not isinstance(model.get("jobs"), dict):
        raise WorkflowParseError(f"{path}: jobs must be a mapping")
    return model


def workflow_name(model: dict[str, Any]) -> str:
    name = model.get("name")
    return name if isinstance(name, str) else ""


def workflow_triggers(model: dict[str, Any]) -> set[str]:
    triggers = model.get("on")
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        return {trigger for trigger in triggers if isinstance(trigger, str)}
    if isinstance(triggers, dict):
        return set(triggers)
    return set()


def workflow_permissions(model: dict[str, Any]) -> dict[str, str]:
    permissions = model.get("permissions")
    if permissions == "read-all":
        return {"*": "read"}
    if permissions == "write-all":
        return {"*": "write"}
    if not isinstance(permissions, dict):
        return {}
    return {str(key): str(value) for key, value in permissions.items()}


def workflow_jobs(model: dict[str, Any]) -> dict[str, Any]:
    jobs = model.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def workflow_job_display_names(model: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for job in workflow_jobs(model).values():
        if isinstance(job, dict) and isinstance(job.get("name"), str):
            names.add(job["name"])
    return names


def workflow_job_permissions(model: dict[str, Any], job_id: str) -> dict[str, str]:
    job = workflow_jobs(model).get(job_id)
    if not isinstance(job, dict):
        return {}
    permissions = job.get("permissions")
    if permissions == "read-all":
        return {"*": "read"}
    if permissions == "write-all":
        return {"*": "write"}
    if not isinstance(permissions, dict):
        return {}
    return {str(key): str(value) for key, value in permissions.items()}


def workflow_uses_actions(model: dict[str, Any]) -> list[str]:
    uses: list[str] = []
    for job in workflow_jobs(model).values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("uses"), str):
                uses.append(step["uses"])
    return uses


def workflow_step_runs(model: dict[str, Any]) -> list[str]:
    runs: list[str] = []
    for job in workflow_jobs(model).values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                runs.append(step["run"])
    return runs


def workflow_top_env(model: dict[str, Any]) -> dict[str, str]:
    env = model.get("env")
    if not isinstance(env, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in env.items():
        if isinstance(value, bool):
            normalized[str(key)] = "true" if value else "false"
        else:
            normalized[str(key)] = str(value)
    return normalized
