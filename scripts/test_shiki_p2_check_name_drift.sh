#!/usr/bin/env bash
# P2.3.7 — Required check name drift between config.yaml and workflow job names.
#
# mergegate_check.enforce_required_check_definitions compares each configured
# required check against the actual workflow job display names. A check that no
# job provides must be flagged so a renamed/removed job cannot silently pass
# MergeGate.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

from mergegate_check import enforce_required_check_definitions, workflow_job_names


def build_target(required_checks: list[str], job_name: str) -> Path:
    target = Path(tempfile.mkdtemp())
    (target / ".shiki").mkdir(parents=True)
    config = ["mergegate:", "  required_checks:"]
    config += [f"    - {check}" for check in required_checks]
    (target / ".shiki" / "config.yaml").write_text("\n".join(config) + "\n", encoding="utf-8")

    workflow_dir = target / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "w.yml").write_text(
        "name: W\n"
        "on:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  build:\n"
        f"    name: {job_name}\n"
        "    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    return target


# Drift: config requires "Validate Shiki mirror" but the only job is named
# "Renamed Job" -> must be flagged.
drifted = build_target(["Validate Shiki mirror"], "Renamed Job")
assert "Renamed Job" in workflow_job_names(drifted)
blocking: list[str] = []
enforce_required_check_definitions(drifted, blocking)
assert any("Validate Shiki mirror" in reason for reason in blocking), blocking

# Aligned: the required check matches a real job name -> clean.
aligned = build_target(["Validate Shiki mirror"], "Validate Shiki mirror")
clean: list[str] = []
enforce_required_check_definitions(aligned, clean)
assert clean == [], clean

# No workflows at all -> the validator must fail closed.
empty = Path(tempfile.mkdtemp())
(empty / ".shiki").mkdir(parents=True)
(empty / ".shiki" / "config.yaml").write_text(
    "mergegate:\n  required_checks:\n    - Validate Shiki mirror\n", encoding="utf-8"
)
empty_blocking: list[str] = []
enforce_required_check_definitions(empty, empty_blocking)
assert empty_blocking, "missing workflow job names must fail closed"

print("P2.3.7 required check name drift detection passed")
PY
