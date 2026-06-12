#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

registry_module = importlib.import_module("shiki_runtime_registry")
contracts = importlib.import_module("shiki_contracts")
validator = importlib.import_module("validate_shiki")
installer = importlib.import_module("shiki_installer")

names = registry_module.runtime_names()
registry = registry_module.runtime_registry()
if tuple(contracts.RUNTIME_NAMES) != names:
    raise SystemExit("RUNTIME_NAMES drifted from runtime registry")
for required in (
    "codex",
    "codex-front",
    "claude-code",
    "claude-code-action",
    "github-cca",
    "github-actions",
    "hermes-runner",
    "human",
    "other",
):
    if required not in names:
        raise SystemExit(f"registry omitted {required}")
if names != tuple(sorted(names)):
    raise SystemExit("runtime_names() must be deterministic and sorted")

for name in names:
    descriptor = registry[name]
    for field in ("name", "display_name", "roles", "execution_mode", "auth_mode"):
        if not getattr(descriptor, field):
            raise SystemExit(f"{name} descriptor missing {field}")
    if registry_module.get_runtime(name).name != name:
        raise SystemExit(f"get_runtime failed for {name}")

try:
    registry_module.get_runtime("missing-runtime")
except registry_module.RuntimeRegistryError:
    pass
else:
    raise SystemExit("unknown runtime unexpectedly passed")

valid_pairs = {
    "front": "codex-front",
    "implementer": "claude-code",
    "planner": "claude-code",
    "completion_checker": "github-cca",
    "reviewer": "claude-code-action",
    "verifier": "github-actions",
}
for role, runtime in valid_pairs.items():
    registry_module.validate_runtime_role_assignment(role, runtime)

# ADR 0008: claude-code is the default implementer/runner; codex stays a
# valid optional implementer.
registry_module.validate_runtime_role_assignment("runner", "claude-code")
registry_module.validate_runtime_role_assignment("implementer", "codex-front")
registry_module.validate_runtime_role_assignment("implementer", "codex")

try:
    registry_module.validate_runtime_role_assignment("verifier", "codex")
except registry_module.RuntimeRegistryError:
    pass
else:
    raise SystemExit("unsupported runtime role pair unexpectedly passed")

template_paths = set(installer.TEMPLATE_PATHS)
for required_path in ("scripts/shiki_runtime_registry.py", "scripts/test_shiki_runtime_registry.sh"):
    if required_path not in template_paths:
        raise SystemExit(f"TEMPLATE_PATHS omitted {required_path}")
stage_paths = set(installer.manifest_stage_paths(Path.cwd()))
if "scripts/shiki_runtime_registry.py" not in stage_paths:
    raise SystemExit("manifest staging omitted scripts/shiki_runtime_registry.py")

doc = Path("docs/agents/runtime-registry.md").read_text(encoding="utf-8")
for name in names:
    if f"`{name}`" not in doc:
        raise SystemExit(f"runtime registry docs omitted {name}")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    shutil.copytree(".shiki", root / ".shiki")
    shutil.copytree("docs", root / "docs")
    shutil.copytree("scripts", root / "scripts")

    original_root = validator.ROOT
    original_shiki = validator.SHIKI
    validator.ROOT = root
    validator.SHIKI = root / ".shiki"
    try:
        validator.validate_config_runtime_assignments()
        validator.validate_task_runtime_assignment(root / ".shiki/tasks/fixture.json", "codex")
        validator.validate_task_runtime_assignment(root / ".shiki/tasks/fixture.json", "claude-code")
        validator.validate_task_runtime_assignment(root / ".shiki/tasks/fixture.json", "human")
        validator.validate_task_runtime_assignment(root / ".shiki/tasks/fixture.json", "other")

        config = root / ".shiki/config.yaml"
        original_config = config.read_text(encoding="utf-8")

        config.write_text(original_config.replace("  implementer: claude-code", "  implementer: missing-runtime"), encoding="utf-8")
        try:
            validator.validate_config_runtime_assignments()
        except validator.ValidationError:
            pass
        else:
            raise SystemExit("unknown runtime in config unexpectedly passed")

        config.write_text(original_config.replace("  verifier: github-actions", "  verifier: codex"), encoding="utf-8")
        try:
            validator.validate_config_runtime_assignments()
        except validator.ValidationError:
            pass
        else:
            raise SystemExit("wrong runtime role assignment unexpectedly passed")

        config.write_text(original_config.replace("  reviewer: claude-code-action\n", ""), encoding="utf-8")
        try:
            validator.validate_config_runtime_assignments()
        except validator.ValidationError:
            pass
        else:
            raise SystemExit("missing required runtime role unexpectedly passed")

        config.write_text(original_config.replace("  verifier: github-actions", "  verifier: other"), encoding="utf-8")
        try:
            validator.validate_config_runtime_assignments()
        except validator.ValidationError:
            pass
        else:
            raise SystemExit("other without rationale unexpectedly passed")

        config.write_text(original_config.replace("  verifier: github-actions", "  verifier: other\n  verifier_rationale: fixture fallback"), encoding="utf-8")
        validator.validate_config_runtime_assignments()

        try:
            validator.validate_task_runtime_assignment(root / ".shiki/tasks/fixture.json", "missing-runtime")
        except validator.ValidationError:
            pass
        else:
            raise SystemExit("unknown task assigned_runtime unexpectedly passed")

        try:
            validator.validate_task_runtime_assignment(root / ".shiki/tasks/fixture.json", "github-actions")
        except validator.ValidationError:
            pass
        else:
            raise SystemExit("non-task runtime assigned_runtime unexpectedly passed")
    finally:
        validator.ROOT = original_root
        validator.SHIKI = original_shiki

print("shiki runtime registry tests passed")
PY
