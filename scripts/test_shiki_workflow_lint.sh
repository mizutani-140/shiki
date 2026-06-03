#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STRICT=0
if [[ "${1:-}" == "--strict" ]]; then
  STRICT=1
fi

cd "$ROOT"

if ! command -v actionlint >/dev/null 2>&1; then
  if [[ "$STRICT" == "1" ]]; then
    echo "actionlint is required in strict mode" >&2
    exit 1
  fi
  echo "Skipping actionlint: actionlint is not installed"
  exit 0
fi

actionlint \
  -ignore 'SC2153:.*BOT_LOGIN' \
  .github/workflows/*.yml
echo "Shiki workflow actionlint passed"
