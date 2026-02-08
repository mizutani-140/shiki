#!/usr/bin/env python3
"""Detect stack hints and suggest team roles with authority layers.

Usage:
  python3 scripts/detect_stack_and_roles.py

It prints a JSON summary to stdout.
This is heuristic-based and safe.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path.cwd()


def exists_any(paths: list[str]) -> bool:
    return any((ROOT / p).exists() for p in paths)


def glob_any(patterns: list[str]) -> bool:
    for pat in patterns:
        if list(ROOT.glob(pat)):
            return True
    return False


# Authority layer mapping
AUTHORITY_MAP = {
    "TeamLeader": "coordinator",
    "GoalClarifier": "coordinator",
    "Architect": "coordinator",
    "Implementer": "executor",
    "FrontendDev": "executor",
    "BackendDev": "executor",
    "DataEngineer": "executor",
    "MLEngineer": "executor",
    "QA": "executor",
    "SecurityReviewer": "monitor",
    "ReleaseCaptain": "coordinator",
    "DevOps": "executor",
    "APIDesigner": "coordinator",
}


def main() -> None:
    hints = []
    roles = set()

    # Always needed
    roles.update({"TeamLeader", "GoalClarifier", "Architect", "QA", "SecurityReviewer", "ReleaseCaptain"})

    if exists_any(["package.json", "pnpm-lock.yaml", "yarn.lock"]) or glob_any(["**/*.ts", "**/*.tsx"]):
        hints.append("Detected Node/TypeScript")
        roles.add("FrontendDev")

    if exists_any(["requirements.txt", "pyproject.toml", "poetry.lock"]) or glob_any(["**/*.py"]):
        hints.append("Detected Python")
        roles.add("BackendDev")

    if exists_any(["Dockerfile", "docker-compose.yml", "compose.yml"]) or glob_any(["infra/**", ".github/workflows/**"]):
        hints.append("Detected Docker/CI/Infra")
        roles.add("DevOps")

    if exists_any(["openapi.yaml", "openapi.yml"]) or glob_any(["**/openapi*.yml", "**/openapi*.yaml"]):
        hints.append("Detected OpenAPI")
        roles.add("APIDesigner")

    # Build role list with authority layers
    role_details = []
    for role in sorted(roles):
        role_details.append({
            "role": role,
            "authority_layer": AUTHORITY_MAP.get(role, "executor"),
        })

    out = {
        "hints": hints,
        "suggested_roles": role_details,
        "authority_summary": {
            "coordinator": [r["role"] for r in role_details if r["authority_layer"] == "coordinator"],
            "executor": [r["role"] for r in role_details if r["authority_layer"] == "executor"],
            "monitor": [r["role"] for r in role_details if r["authority_layer"] == "monitor"],
        },
        "note": "This is heuristic. Review and adjust based on GOAL.md.",
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
