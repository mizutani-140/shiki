#!/usr/bin/env bash
# P2.3.4 — Fork PR limitation handling.
#
# Shiki deliberately uses `pull_request` (NOT `pull_request_target`) for the
# CCA and MergeGate workflows. On fork PRs, GitHub gives `pull_request` runs a
# read-only GITHUB_TOKEN and no access to repository secrets, so the CCA review
# bridge and secret-backed steps cannot run from an untrusted fork. This test
# pins that security posture: the workflows must not opt into the
# pull_request_target escalation, and AGENTS.md must keep the warning.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

from shiki_workflows import load_workflow_model, workflow_triggers

workflow_dir = Path.cwd() / ".github" / "workflows"

for filename in ("shiki-cca-completion.yml", "shiki-mergegate.yml", "shiki-validate.yml"):
    path = workflow_dir / filename
    model = load_workflow_model(path)
    triggers = workflow_triggers(model)
    assert "pull_request" in triggers, f"{filename}: must run on pull_request, got {sorted(triggers)}"
    assert "pull_request_target" not in triggers, (
        f"{filename}: must NOT use pull_request_target (fork secret-exfil risk)"
    )

# The Claude runtime contract must keep the explicit pull_request_target
# caution so the fork limitation is not silently relaxed.
claude_md = (Path.cwd() / "CLAUDE.md").read_text(encoding="utf-8")
assert "pull_request_target" in claude_md, "CLAUDE.md must document the pull_request_target tradeoff"

print("P2.3.4 fork PR limitation handling passed")
PY
