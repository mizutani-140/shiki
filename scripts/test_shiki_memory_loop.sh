#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-memory-loop-test-$$"
TARGET="$TMP_ROOT/target"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

json_get() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

expect_fail() {
  if "$@" >/tmp/shiki-memory-expected-fail.out 2>&1; then
    echo "expected failure but command succeeded: $*" >&2
    cat /tmp/shiki-memory-expected-fail.out >&2
    return 1
  fi
}

cd "$ROOT"
python3 scripts/validate_shiki.py

# The wrapper module, schema, and migration must be wired in.
test -f .shiki/schemas/memory-entry.schema.json
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import shiki_installer
from validate_shiki import LEDGER_TYPES, MEMORY_ID
from shiki_contracts import TARGET_STATE_DIRECTORIES
from shiki_migrations import migration_ids, MEMORIES_MIGRATION_ID

if "memory-transition" not in LEDGER_TYPES:
    raise SystemExit("LEDGER_TYPES missing memory-transition")
if ".shiki/memories" not in TARGET_STATE_DIRECTORIES:
    raise SystemExit("TARGET_STATE_DIRECTORIES missing .shiki/memories")
if MEMORIES_MIGRATION_ID not in migration_ids():
    raise SystemExit("memories migration not registered")
if "scripts/shiki_memory.py" not in set(shiki_installer.TEMPLATE_PATHS):
    raise SystemExit("TEMPLATE_PATHS missing scripts/shiki_memory.py")
if "scripts/test_shiki_memory_loop.sh" not in set(shiki_installer.TEMPLATE_PATHS):
    raise SystemExit("TEMPLATE_PATHS missing scripts/test_shiki_memory_loop.sh")
# manifest install.create_directories must match TARGET_STATE_DIRECTORIES order.
import json
manifest = json.loads(Path(".shiki/manifest.json").read_text())
if tuple(manifest["install"]["create_directories"]) != tuple(TARGET_STATE_DIRECTORIES):
    raise SystemExit("manifest create_directories drifted from TARGET_STATE_DIRECTORIES")
PY

# Install into a fresh target; the migration must apply with no pending state.
mkdir -p "$TARGET"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-memory-install.out
cd "$TARGET"
git init -b main >/tmp/shiki-memory-git.out
git remote add origin https://github.com/example/shiki-memory-loop-test.git
git add .
git -c user.name="Shiki Test" -c user.email="shiki@example.test" commit -m "init" >/tmp/shiki-memory-commit.out
python3 "$ROOT/scripts/shiki.py" migrate apply --execute --target "$TARGET" >/tmp/shiki-memory-migrate.out
python3 "$ROOT/scripts/shiki.py" migrate status --json --target "$TARGET" >/tmp/shiki-memory-migrate-status.json
test "$(json_get /tmp/shiki-memory-migrate-status.json pending_count)" = "0"
python3 "$ROOT/scripts/validate_shiki.py" 2>/dev/null || true  # target validator runs against its own tree below

# A real anchoring goal must exist; the memory's ledger events reference it.
python3 "$ROOT/scripts/shiki.py" goal create --target "$TARGET" --title "Memory anchor" --outcome "Anchor for memory capture" >/tmp/shiki-memory-goal.json
GOAL=$(json_get /tmp/shiki-memory-goal.json goal_id)

# Capture against a non-existent goal fails open (no file written, no poison ledger).
NOGOAL=$(python3 "$ROOT/scripts/shiki.py" memory capture --target "$TARGET" \
  --area locks --source-kind manual --claim "x" --goal-id G-99999999)
test "$(echo "$NOGOAL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["written"])')" = "False"
# Capture with no --goal-id is rejected by the CLI (required).
expect_fail python3 "$ROOT/scripts/shiki.py" memory capture --target "$TARGET" --area locks --source-kind manual --claim "x"

# Capture -> investigate -> promote -> distill happy path against the real goal.
CAP=$(python3 "$ROOT/scripts/shiki.py" memory capture --target "$TARGET" \
  --area locks --source-kind manual \
  --claim "Lock declarations were missing on review-fix files across goals." \
  --goal-id "$GOAL")
MEM=$(echo "$CAP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["memory_id"])')
test "$(echo "$CAP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["written"])')" = "True"

python3 "$ROOT/scripts/shiki.py" memory investigate --target "$TARGET" "$MEM" --summary "Reproduced across PR #122 and #124." >/dev/null

# Acceptance: raw->verified and raw->distilled direct promotion are impossible
# (the CLI only exposes the one-step transitions; assert via the engine).
python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
from shiki_memory import memory_transition_errors
assert memory_transition_errors("raw", "verified"), "raw->verified must be rejected"
assert memory_transition_errors("raw", "distilled"), "raw->distilled must be rejected"
assert memory_transition_errors("investigated", "distilled"), "investigated->distilled must be rejected"
PY

# A verified promotion needs a real local evidence file.
LEDGER_FILE=$(find "$TARGET/.shiki/ledger" -name 'L-*.json' | head -1)
REL=".shiki/ledger/$(basename "$LEDGER_FILE")"
# Zero local evidence is rejected.
expect_fail python3 "$ROOT/scripts/shiki.py" memory promote --target "$TARGET" "$MEM"
python3 "$ROOT/scripts/shiki.py" memory promote --target "$TARGET" "$MEM" --local-evidence ledger "$REL" >/dev/null
test "$(json_get "$TARGET/.shiki/memories/$MEM.json" status)" = "verified"

# distill requires --approve and is refused in autonomous context.
expect_fail python3 "$ROOT/scripts/shiki.py" memory distill --target "$TARGET" "$MEM" --rule "Declare every touched file in task locks." --approved-by mizutani-140
SHIKI_AUTONOMOUS_EXECUTION=1 expect_fail python3 "$ROOT/scripts/shiki.py" memory distill --target "$TARGET" "$MEM" --rule "x" --approved-by op --approve
grep "operator-only" /tmp/shiki-memory-expected-fail.out >/dev/null

# Atomicity (B2): a distill that fails its pre-checks leaves no orphan approval
# ledger and does not change the memory. Use an invalid supersede target.
LEDGER_BEFORE=$(find "$TARGET/.shiki/ledger" -name 'L-*.json' | wc -l | tr -d ' ')
expect_fail python3 "$ROOT/scripts/shiki.py" memory distill --target "$TARGET" "$MEM" \
  --rule "x" --approved-by mizutani-140 --approve --supersede "$MEM"
LEDGER_AFTER=$(find "$TARGET/.shiki/ledger" -name 'L-*.json' | wc -l | tr -d ' ')
test "$LEDGER_BEFORE" = "$LEDGER_AFTER"  # no approval ledger written on failure
test "$(json_get "$TARGET/.shiki/memories/$MEM.json" status)" = "verified"  # memory unchanged
# redaction skipped writes no memory file (B4).
SKIP=$(python3 "$ROOT/scripts/shiki.py" memory capture --target "$TARGET" --area locks --source-kind manual --claim "x" --goal-id "$GOAL" --redaction skipped)
test "$(echo "$SKIP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["written"])')" = "False"

# Operator distill succeeds and records an approval ledger.
python3 "$ROOT/scripts/shiki.py" memory distill --target "$TARGET" "$MEM" \
  --rule "Declare every touched file in task locks before opening a PR." --approved-by mizutani-140 --approve >/dev/null
test "$(json_get "$TARGET/.shiki/memories/$MEM.json" status)" = "distilled"
test "$(json_get "$TARGET/.shiki/memories/$MEM.json" active)" = "True"
python3 -c "import json; d=json.load(open('$TARGET/.shiki/memories/$MEM.json')); assert d['approval_ledger'].startswith('.shiki/ledger/L-')"

# The distilled rule's approval_ledger must exist; the target validator passes.
cd "$TARGET"
python3 "$TARGET/scripts/validate_shiki.py"

# revoke deactivates the rule.
cd "$ROOT"
python3 "$ROOT/scripts/shiki.py" memory revoke --target "$TARGET" "$MEM" --revoked-by mizutani-140 --reason "rule superseded by automation" >/dev/null
test "$(json_get "$TARGET/.shiki/memories/$MEM.json" active)" = "False"
cd "$TARGET" && python3 "$TARGET/scripts/validate_shiki.py"
cd "$ROOT"

# Validator is the fail-closed boundary: a directly-committed poisoned memory
# file (bypassing the CLI effectors) must be REJECTED by validate_shiki.py.
# This proves the repository-wide validator enforces the memory contract on
# committed state, not just the CLI write path.
python3 - "$TARGET" "$MEM" <<'PY'
import copy, json, subprocess, sys
from pathlib import Path

target = Path(sys.argv[1])
mem = sys.argv[2]
mem_dir = target / ".shiki" / "memories"
validator = str(target / "scripts" / "validate_shiki.py")
valid = json.loads((mem_dir / f"{mem}.json").read_text(encoding="utf-8"))


def validates() -> bool:
    return subprocess.run(
        [sys.executable, validator], cwd=str(target), capture_output=True
    ).returncode == 0


if not validates():
    raise SystemExit("baseline target tree must validate before poisoning")


def reject(name: str, entry: dict, *, filename: str) -> None:
    path = mem_dir / filename
    path.write_text(json.dumps(entry), encoding="utf-8")
    poisoned_rejected = not validates()
    path.unlink()
    if not poisoned_rejected:
        raise SystemExit(f"validate_shiki.py must REJECT committed poisoned memory: {name}")
    if not validates():
        raise SystemExit(f"clean tree must validate again after removing {name}")


# 1. filename does not match id.
e = copy.deepcopy(valid)
e["id"] = "MEM-00000001"
reject("filename!=id", e, filename="MEM-90000001.json")

# 2. distilled entry missing its operator approval_ledger (memory_entry_errors).
e = copy.deepcopy(valid)
e["id"] = "MEM-00000002"
e.pop("approval_ledger", None)
reject("distilled-without-approval_ledger", e, filename="MEM-00000002.json")

# 3. memory anchored to a non-existent goal (referential integrity).
e = copy.deepcopy(valid)
e["id"] = "MEM-00000003"
e["source"] = {**e.get("source", {}), "goal_id": "G-90000003"}
reject("dangling-source-goal_id", e, filename="MEM-00000003.json")

# 4. memory with NO source.goal_id (unanchored to any Goal) is rejected.
e = copy.deepcopy(valid)
e["id"] = "MEM-00000004"
e["source"] = {"kind": "manual"}
reject("missing-source-goal_id", e, filename="MEM-00000004.json")

print("validator poisoned-memory rejection checks passed")
PY

echo "shiki memory loop tests passed"
