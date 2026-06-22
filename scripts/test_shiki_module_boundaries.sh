#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))

modules = [
    "shiki",
    "shiki_bootstrap",
    "shiki_cli",
    "shiki_config",
    "shiki_doctor",
    "shiki_git",
    "shiki_github",
    "shiki_guardian",
    "shiki_guardian_review",
    "shiki_installer",
    "shiki_loop",
    "shiki_memory",
    "shiki_migrations",
    "shiki_process",
    "shiki_provider",
    "shiki_runtime",
    "shiki_runtime_adapters",
    "shiki_runtime_registry",
    "shiki_tasks",
]
for module in modules:
    importlib.import_module(module)

import shiki

for export in [
    "ensure_control_dirs",
    "manifest_stage_paths",
    "branch_protection_review_count",
    "protect_branch",
]:
    if not hasattr(shiki, export):
        raise SystemExit(f"missing transitional shiki.{export} export")

line_count = len((Path.cwd() / "scripts" / "shiki.py").read_text(encoding="utf-8").splitlines())
if line_count > 350:
    raise SystemExit(f"scripts/shiki.py must remain a thin shim, got {line_count} lines")

stage_paths = set(shiki.manifest_stage_paths(Path.cwd()))
for required in [
    "scripts/shiki.py",
    "scripts/shiki_bootstrap.py",
    "scripts/shiki_cli.py",
    "scripts/shiki_config.py",
    "scripts/shiki_doctor.py",
    "scripts/shiki_git.py",
    "scripts/shiki_github.py",
    "scripts/shiki_guardian.py",
    "scripts/shiki_guardian_review.py",
    "scripts/shiki_installer.py",
    "scripts/shiki_loop.py",
    "scripts/shiki_memory.py",
    "scripts/shiki_migrations.py",
    "scripts/shiki_provider.py",
    "scripts/shiki_process.py",
    "scripts/shiki_runtime.py",
    "scripts/shiki_runtime_adapters.py",
    "scripts/shiki_runtime_registry.py",
    "scripts/shiki_tasks.py",
    "scripts/test_shiki_doctor.sh",
    "scripts/test_shiki_runner_claude.sh",
    "scripts/test_shiki_migrations.sh",
    "scripts/test_shiki_guardian_policy.sh",
    "scripts/test_shiki_module_boundaries.sh",
]:
    if required not in stage_paths:
        raise SystemExit(f"manifest staging omitted {required}")
print("module import and staging smoke passed")
PY

python3 scripts/shiki.py --help >/tmp/shiki-module-help.out
grep "init" /tmp/shiki-module-help.out >/dev/null
grep "bootstrap-platform" /tmp/shiki-module-help.out >/dev/null
grep "runner" /tmp/shiki-module-help.out >/dev/null

python3 scripts/shiki.py doctor --help >/tmp/shiki-module-doctor-help.out
grep -- "--target" /tmp/shiki-module-doctor-help.out >/dev/null
grep -- "--online" /tmp/shiki-module-doctor-help.out >/dev/null
grep -- "--strict" /tmp/shiki-module-doctor-help.out >/dev/null

python3 scripts/shiki.py migrate --help >/tmp/shiki-module-migrate-help.out
grep "status" /tmp/shiki-module-migrate-help.out >/dev/null
grep "plan" /tmp/shiki-module-migrate-help.out >/dev/null
grep "apply" /tmp/shiki-module-migrate-help.out >/dev/null

python3 scripts/shiki.py init --help >/tmp/shiki-module-init-help.out
for flag in \
  "--execute" \
  "--i-understand" \
  "--adopt-existing-repo" \
  "--provider" \
  "--github-host" \
  "--github-api-url" \
  "--remote-protocol" \
  "--no-set-secret" \
  "--no-protect" \
  "--no-commit" \
  "--no-push"; do
  grep -- "$flag" /tmp/shiki-module-init-help.out >/dev/null
done

python3 scripts/shiki.py bootstrap-platform --help >/tmp/shiki-module-bootstrap-help.out
grep -- "--execute" /tmp/shiki-module-bootstrap-help.out >/dev/null
grep -- "--i-understand" /tmp/shiki-module-bootstrap-help.out >/dev/null
grep -- "--github-host" /tmp/shiki-module-bootstrap-help.out >/dev/null

python3 scripts/shiki.py bootstrap-github --help >/tmp/shiki-module-bootstrap-github-help.out
grep -- "--execute" /tmp/shiki-module-bootstrap-github-help.out >/dev/null
grep -- "--remote-protocol" /tmp/shiki-module-bootstrap-github-help.out >/dev/null

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

DRY_RUN_TARGET="$TMP_ROOT/dry-run"
python3 scripts/shiki.py init "$DRY_RUN_TARGET" \
  --repo example/shiki-module-boundary \
  --no-commit \
  --no-push >/tmp/shiki-module-init-dry-run.out
grep "dry-run: no bootstrap/init mutations were executed" /tmp/shiki-module-init-dry-run.out >/dev/null
if [ -e "$DRY_RUN_TARGET/.shiki/repo.json" ]; then
  echo "dry-run unexpectedly wrote .shiki/repo.json" >&2
  exit 1
fi

INSTALL_TARGET="$TMP_ROOT/install-target"
mkdir -p "$INSTALL_TARGET"
python3 scripts/shiki.py install-target "$INSTALL_TARGET" --local-only --no-validate >/tmp/shiki-module-install.out
for path in \
  "scripts/shiki.py" \
  "scripts/shiki_bootstrap.py" \
  "scripts/shiki_cli.py" \
  "scripts/shiki_config.py" \
  "scripts/shiki_doctor.py" \
  "scripts/shiki_git.py" \
  "scripts/shiki_github.py" \
  "scripts/shiki_installer.py" \
  "scripts/shiki_migrations.py" \
  "scripts/shiki_provider.py" \
  "scripts/shiki_process.py" \
  "scripts/shiki_runtime.py" \
  "scripts/shiki_runtime_registry.py" \
  "scripts/shiki_tasks.py"; do
  if [ ! -f "$INSTALL_TARGET/$path" ]; then
    echo "install-target omitted $path" >&2
    exit 1
  fi
done

echo "shiki module boundary tests passed"
