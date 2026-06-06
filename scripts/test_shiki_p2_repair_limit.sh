#!/usr/bin/env bash
# P2.3.9 — Repair loop attempt limit (3) and required repair-packet content.
#
# The default automatic repair limit is 3. `shiki repair packet` must:
#  * accept attempts up to and including 3,
#  * reject attempt 4 with a clear limit error,
#  * emit a packet whose JSON satisfies every required field of
#    .shiki/schemas/repair-packet.schema.json.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
TARGET="$TMP_ROOT/target"
mkdir -p "$TARGET"

python3 scripts/shiki.py install-target "$TARGET" --local-only --no-validate >/dev/null

cd "$TARGET"
git init -b main >/dev/null
git config user.email "shiki-test@example.com"
git config user.name "Shiki Test"
git remote add origin https://github.com/example/shiki-p2-repair-test.git
cd "$ROOT"

GOAL_JSON="$(python3 scripts/shiki.py goal create \
  --target "$TARGET" \
  --title "Repair limit fixture goal" \
  --outcome "Repair packets enforce the attempt limit" \
  --completion-condition "Repair packet schema is satisfied" \
  --required-skill tdd)"
GOAL_ID="$(printf '%s' "$GOAL_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["goal_id"])')"

TASK_JSON="$(python3 scripts/shiki.py issue plan \
  --target "$TARGET" \
  --goal-id "$GOAL_ID" \
  --title "Repair limit fixture task" \
  --scope "Fixture task for repair-packet attempt-limit coverage" \
  --acceptance-check "Repair packet is created" \
  --lock "path:src/fixture/*" \
  --required-skill tdd)"
TASK_ID="$(printf '%s' "$TASK_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["task_id"])')"

make_packet() {
  local attempt="$1"
  python3 scripts/shiki.py repair packet \
    --target "$TARGET" \
    --task-id "$TASK_ID" \
    --pr 4242 \
    --attempt "$attempt" \
    --failing-item "V-01" \
    --failing-acceptance-criteria "Repair packet is created" \
    --minimal-change "Fix the failing unit test only" \
    --prohibited-change "Do not edit scripts/*.py" \
    --required-skill tdd \
    --verification-command "python3 -m unittest discover -s tests" \
    --stop-condition "Stop after 3 failed attempts and report blockers"
}

# Attempt 3 is the last allowed attempt; it must succeed and produce a packet.
PACKET_JSON="$(make_packet 3)"
REPAIR_FILE="$(printf '%s' "$PACKET_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["repair_file"])')"
test -f "$REPAIR_FILE"

# Validate packet content against the canonical repair-packet schema fields.
python3 - "$TARGET" "$REPAIR_FILE" "$GOAL_ID" "$TASK_ID" <<'PY'
import json
import sys
from pathlib import Path

target, repair_file, goal_id, task_id = sys.argv[1:5]
schema = json.loads(
    (Path(target) / ".shiki" / "schemas" / "repair-packet.schema.json").read_text(encoding="utf-8")
)
packet = json.loads(Path(repair_file).read_text(encoding="utf-8"))

for field in schema.get("required", []):
    assert field in packet, f"repair packet missing required field {field}"

assert packet["goal_id"] == goal_id, packet["goal_id"]
assert packet["task_id"] == task_id, packet["task_id"]
assert packet["pr"] == 4242, packet["pr"]
assert packet["attempt"] == 3, packet["attempt"]
assert packet["required_skill"] == "tdd", packet["required_skill"]
assert packet["minimal_required_changes"], packet["minimal_required_changes"]
assert packet["verification_commands"], packet["verification_commands"]
assert packet["stop_condition"], packet["stop_condition"]
print("repair packet content satisfies schema")
PY

# Attempt 4 exceeds the limit and must fail with a clear message.
set +e
OVER_LIMIT="$(make_packet 4 2>&1)"
STATUS=$?
set -e
if [ "$STATUS" -eq 0 ]; then
  echo "expected repair attempt 4 to fail with limit error" >&2
  exit 1
fi
printf '%s' "$OVER_LIMIT" | grep -q "repair attempt limit is 3"

echo "P2.3.9 repair attempt-limit and packet content passed"
