#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STRICT=0
if [[ "${1:-}" == "--strict" ]]; then
  STRICT=1
fi

cd "$ROOT"

if ! command -v shellcheck >/dev/null 2>&1; then
  if [[ "$STRICT" == "1" ]]; then
    echo "shellcheck is required in strict mode" >&2
    exit 1
  fi
  echo "Skipping shellcheck: shellcheck is not installed"
  exit 0
fi

mapfile -t scripts < <(find scripts -maxdepth 1 -type f -name '*.sh' | sort)
if [[ "${#scripts[@]}" == "0" ]]; then
  echo "No shell scripts found for shellcheck"
  exit 0
fi

shellcheck "${scripts[@]}"
echo "Shiki shellcheck passed"
