#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-init-test-$$"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

expect_fail() {
  if "$@" >/tmp/shiki-expected-fail.out 2>&1; then
    echo "expected failure but command succeeded: $*" >&2
    cat /tmp/shiki-expected-fail.out >&2
    return 1
  fi
}

cd "$ROOT"

python3 scripts/validate_shiki.py
python3 -m py_compile scripts/shiki.py
python3 scripts/shiki.py --help | grep -E "init|preflight" >/dev/null

grep "shiki start" .claude/commands/shiki.md >/dev/null
grep "shiki start" .codex/skills/shiki/SKILL.md >/dev/null

mkdir -p "$TMP_ROOT/missing-repo" "$TMP_ROOT/invalid-repo" "$TMP_ROOT/no-local" "$TMP_ROOT/local-only"

expect_fail python3 scripts/shiki.py init "$TMP_ROOT/missing-repo"
expect_fail python3 scripts/shiki.py init "$TMP_ROOT/invalid-repo" --repo invalid-slug
expect_fail python3 scripts/shiki.py install-target "$TMP_ROOT/no-local"

python3 scripts/shiki.py install-target "$TMP_ROOT/local-only" --local-only >/tmp/shiki-local-only.out
python3 "$TMP_ROOT/local-only/scripts/validate_shiki.py"
test -z "$(find "$TMP_ROOT/local-only/.shiki/tasks" -type f -name '*.json' -print -quit)"
test -z "$(find "$TMP_ROOT/local-only/.shiki/ledger" -type f -name '*.json' -print -quit)"

expect_fail python3 scripts/shiki.py preflight "$TMP_ROOT/local-only" --require-github

echo "shiki init tests passed"
