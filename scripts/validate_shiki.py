#!/usr/bin/env python3
"""Validate .shiki/ artifacts against JSON Schemas.

Validates tasks, contracts, DAGs, and session states.
Also performs DAG cycle detection and budget verification.

Usage:
  python3 scripts/validate_shiki.py

Optional dependency:
  pip install jsonschema
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path.cwd()
SHIKI = ROOT / ".shiki"
SCHEMAS = SHIKI / "schemas"


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def minimal_task_check(t: Dict[str, Any]) -> list[str]:
    errs = []
    for k in ["id", "title", "assigned_to", "status"]:
        if k not in t:
            errs.append(f"missing '{k}'")
    valid_assigned = {"claude-team", "claude-leader", "claude-member", "codex", "human"}
    if t.get("assigned_to") and t["assigned_to"] not in valid_assigned:
        errs.append(f"invalid assigned_to: {t['assigned_to']}")
    valid_status = {"pending", "in_progress", "review", "completed", "blocked", "failed"}
    if t.get("status") and t["status"] not in valid_status:
        errs.append(f"invalid status: {t['status']}")
    return errs


def minimal_contract_check(c: Dict[str, Any]) -> list[str]:
    errs = []
    for k in ["contract_id", "status", "defined_by"]:
        if k not in c:
            errs.append(f"missing '{k}'")
    return errs


def minimal_dag_check(d: Dict[str, Any]) -> list[str]:
    errs = []
    for k in ["dag_id", "nodes", "edges"]:
        if k not in d:
            errs.append(f"missing '{k}'")
    return errs


def detect_dag_cycles(dag: Dict[str, Any]) -> list[str]:
    """Detect cycles in a DAG using DFS."""
    errs = []
    nodes = dag.get("nodes", [])
    edges = dag.get("edges", [])

    node_ids = {n["node_id"] for n in nodes if "node_id" in n}
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for edge in edges:
        src = edge.get("from", "")
        dst = edge.get("to", "")
        if src in adjacency:
            adjacency[src].append(dst)

    # DFS cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in node_ids}

    def dfs(node: str) -> bool:
        color[node] = GRAY
        for neighbor in adjacency.get(node, []):
            if neighbor not in color:
                continue
            if color[neighbor] == GRAY:
                return True  # cycle found
            if color[neighbor] == WHITE and dfs(neighbor):
                return True
        color[node] = BLACK
        return False

    for nid in node_ids:
        if color[nid] == WHITE:
            if dfs(nid):
                errs.append(f"Cycle detected in DAG involving node {nid}")
                break

    return errs


def validate_budget(tasks_dir: Path, config_path: Path) -> list[str]:
    """Check budget constraints across tasks."""
    errs = []
    if not tasks_dir.exists():
        return errs

    try:
        import yaml
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return errs

    budget_config = config.get("github", {}).get("budget", {})
    max_per_task = budget_config.get("max_tokens_per_task", 0)
    max_per_session = budget_config.get("max_tokens_per_session", 0)

    total_actual = 0
    for fp in sorted(tasks_dir.glob("*.json")):
        try:
            t = load_json(fp)
        except Exception:
            continue
        budget = t.get("budget", {})
        actual = budget.get("actual_tokens", 0)
        estimated = budget.get("estimated_tokens", 0)
        task_max = budget.get("max_tokens", max_per_task)

        if task_max and actual > task_max:
            errs.append(f"Task {t.get('id', fp.stem)}: actual_tokens ({actual}) exceeds max ({task_max})")

        total_actual += actual

    if max_per_session and total_actual > max_per_session:
        errs.append(f"Total token usage ({total_actual}) exceeds session limit ({max_per_session})")

    return errs


def main() -> int:
    if not SHIKI.exists():
        print("No .shiki directory found", file=sys.stderr)
        return 1

    try:
        import jsonschema  # type: ignore
    except Exception:
        jsonschema = None  # type: ignore

    errors = 0

    # Collect files
    task_files = sorted((SHIKI / "tasks").glob("*.json"))
    contract_files = sorted((SHIKI / "contracts").glob("*.json"))
    dag_files = sorted((SHIKI / "dag").glob("*.json"))
    session_files = sorted((SHIKI / "state").glob("session-*.json"))

    # Load schemas
    schema_map = {
        "task": SCHEMAS / "task.schema.json",
        "contract": SCHEMAS / "contract.schema.json",
        "dag": SCHEMAS / "dag.schema.json",
        "session": SCHEMAS / "session.schema.json",
    }
    schemas = {}
    for name, path in schema_map.items():
        if path.exists():
            try:
                schemas[name] = load_json(path)
            except Exception:
                pass

    # Validate tasks
    for fp in task_files:
        try:
            t = load_json(fp)
        except Exception as e:
            print(f"[TASK] {fp}: Invalid JSON: {e}", file=sys.stderr)
            errors += 1
            continue
        if jsonschema and "task" in schemas:
            try:
                jsonschema.validate(t, schemas["task"])
            except Exception as e:
                print(f"[TASK] {fp}: {e}", file=sys.stderr)
                errors += 1
        else:
            errs = minimal_task_check(t)
            if errs:
                print(f"[TASK] {fp}: {', '.join(errs)}", file=sys.stderr)
                errors += 1

    # Validate contracts
    for fp in contract_files:
        try:
            c = load_json(fp)
        except Exception as e:
            print(f"[CONTRACT] {fp}: Invalid JSON: {e}", file=sys.stderr)
            errors += 1
            continue
        if jsonschema and "contract" in schemas:
            try:
                jsonschema.validate(c, schemas["contract"])
            except Exception as e:
                print(f"[CONTRACT] {fp}: {e}", file=sys.stderr)
                errors += 1
        else:
            errs = minimal_contract_check(c)
            if errs:
                print(f"[CONTRACT] {fp}: {', '.join(errs)}", file=sys.stderr)
                errors += 1

    # Validate DAGs
    for fp in dag_files:
        try:
            d = load_json(fp)
        except Exception as e:
            print(f"[DAG] {fp}: Invalid JSON: {e}", file=sys.stderr)
            errors += 1
            continue
        if jsonschema and "dag" in schemas:
            try:
                jsonschema.validate(d, schemas["dag"])
            except Exception as e:
                print(f"[DAG] {fp}: {e}", file=sys.stderr)
                errors += 1
        else:
            errs = minimal_dag_check(d)
            if errs:
                print(f"[DAG] {fp}: {', '.join(errs)}", file=sys.stderr)
                errors += 1

        # Cycle detection
        cycle_errs = detect_dag_cycles(d)
        for err in cycle_errs:
            print(f"[DAG] {fp}: {err}", file=sys.stderr)
            errors += 1

    # Validate session states
    for fp in session_files:
        try:
            s = load_json(fp)
        except Exception as e:
            print(f"[SESSION] {fp}: Invalid JSON: {e}", file=sys.stderr)
            errors += 1
            continue
        if jsonschema and "session" in schemas:
            try:
                jsonschema.validate(s, schemas["session"])
            except Exception as e:
                print(f"[SESSION] {fp}: {e}", file=sys.stderr)
                errors += 1

    # Budget validation
    config_path = SHIKI / "config.yaml"
    if config_path.exists():
        budget_errs = validate_budget(SHIKI / "tasks", config_path)
        for err in budget_errs:
            print(f"[BUDGET] {err}", file=sys.stderr)
            errors += 1

    if errors:
        print(f"Validation failed: {errors} error(s)", file=sys.stderr)
        return 1

    print("Validation OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
