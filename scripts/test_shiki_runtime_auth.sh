#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-runtime-auth-test-$$"
FAKE_BIN="$TMP_ROOT/bin"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

json_get() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
current = data
for part in sys.argv[2].split("."):
    current = current[part]
print(current)
PY
}

cd "$ROOT"

python3 scripts/validate_shiki.py
python3 -m py_compile scripts/shiki.py
python3 scripts/shiki.py --help | grep "doctor" >/dev/null

mkdir -p "$FAKE_BIN"

cat >"$FAKE_BIN/claude" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "--version")
    echo "2.1.156 (Claude Code)"
    ;;
  "auth status")
    echo '{"loggedIn":false,"authMethod":"none","apiProvider":"firstParty"}'
    exit 1
    ;;
  *)
    echo "fake claude unsupported: $*" >&2
    exit 1
    ;;
esac
SH
chmod +x "$FAKE_BIN/claude"

cat >"$FAKE_BIN/codex" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "--version")
    echo "codex-cli 0.135.0"
    ;;
  "login status")
    echo "WARNING: proceeding, even though we could not update PATH" >&2
    echo "Logged in using ChatGPT" >&2
    ;;
  *)
    echo "fake codex unsupported: $*" >&2
    exit 1
    ;;
esac
SH
chmod +x "$FAKE_BIN/codex"

cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "--version")
    echo "gh version 2.74.0"
    ;;
  "auth status")
    echo "github.com" >&2
    echo "  X Failed to log in to github.com account mizutani-140" >&2
    exit 1
    ;;
  *)
    echo "fake gh unsupported: $*" >&2
    exit 1
    ;;
esac
SH
chmod +x "$FAKE_BIN/gh"

PATH="$FAKE_BIN:$PATH" python3 scripts/shiki.py doctor --json >/tmp/shiki-doctor.json

test "$(json_get /tmp/shiki-doctor.json runtimes.codex_front.ready)" = "True"
test "$(json_get /tmp/shiki-doctor.json runtimes.claude_code.ready)" = "False"
test "$(json_get /tmp/shiki-doctor.json runtimes.github_cli.ready)" = "False"
test "$(json_get /tmp/shiki-doctor.json entrypoints.codex.slash_command_supported)" = "False"
grep "Claude Code supports" /tmp/shiki-doctor.json >/dev/null
grep "not a \`/shiki\` slash command" /tmp/shiki-doctor.json >/dev/null
grep "claude auth login" /tmp/shiki-doctor.json >/dev/null
grep "gh auth login" /tmp/shiki-doctor.json >/dev/null

echo "shiki runtime auth tests passed"
