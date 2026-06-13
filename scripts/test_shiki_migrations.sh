#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-migrations-test-$$"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

expect_fail() {
  if "$@" >/tmp/shiki-migrations-expected-fail.out 2>&1; then
    echo "expected failure but command succeeded: $*" >&2
    cat /tmp/shiki-migrations-expected-fail.out >&2
    return 1
  fi
}

json_get() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
current = data
for part in sys.argv[2].split("."):
    if isinstance(current, list):
        current = current[int(part)]
    else:
        current = current[part]
print(current)
PY
}

finding_status() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
for finding in data["findings"]:
    if finding["id"] == sys.argv[2]:
        print(finding["status"])
        raise SystemExit(0)
raise SystemExit(f"missing finding {sys.argv[2]}")
PY
}

make_target() {
  local target="$1"
  mkdir -p "$target"
  python3 scripts/shiki.py install-target "$target" --local-only --no-validate >/tmp/shiki-migrations-install.out
}

add_baseline_goal() {
  local target="$1"
  mkdir -p "$target/.shiki/goals"
  printf '{"id":"G-0012"}\n' >"$target/.shiki/goals/G-0012.json"
}

cd "$ROOT"

python3 - <<'PY'
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd() / "scripts"))

from shiki_migrations import Migration, migration_ids, migration_registry, validate_migration_registry

ids = migration_ids()
if ids != tuple(sorted(ids)):
    raise SystemExit("migration IDs are not deterministic")
if len(ids) != len(set(ids)):
    raise SystemExit("migration IDs are not unique")
if validate_migration_registry():
    raise SystemExit("default registry should validate")

duplicate = migration_registry() + (migration_registry()[0],)
if not any("duplicate" in error for error in validate_migration_registry(duplicate)):
    raise SystemExit("duplicate migration id fixture did not fail")

unknown_dependency = (
    Migration(
        id="M-20260604-9999-fixture",
        title="Fixture",
        description="Fixture",
        introduced_in="test",
        requires=("M-20260604-9998-missing",),
    ),
)
if not any("unknown dependency" in error for error in validate_migration_registry(unknown_dependency)):
    raise SystemExit("unknown dependency fixture did not fail")
PY

VALID="$TMP_ROOT/valid"
make_target "$VALID"

python3 scripts/shiki.py migrate status --json --target "$VALID" >/tmp/shiki-migrate-valid.json
python3 -m json.tool /tmp/shiki-migrate-valid.json >/dev/null
test "$(json_get /tmp/shiki-migrate-valid.json valid)" = "True"
test "$(json_get /tmp/shiki-migrate-valid.json pending_count)" = "0"
test "$(json_get /tmp/shiki-migrate-valid.json applied_count)" = "5"

# M-20260612-0001-spec-freeze backfills stored plans that predate Spec Freeze.
SPEC_FREEZE_TARGET="$TMP_ROOT/spec-freeze"
make_target "$SPEC_FREEZE_TARGET"
mkdir -p "$SPEC_FREEZE_TARGET/.shiki/plans"
cat >"$SPEC_FREEZE_TARGET/.shiki/plans/P-0001.json" <<'JSON'
{
  "id": "P-0001",
  "title": "Pre-freeze plan",
  "outcome": "Plan stored before the Spec Freeze contract",
  "grill_with_docs": {"status": "complete", "source": "fixture"},
  "tasks": [
    {"title": "t", "scope": "s", "acceptance_checks": ["c"]}
  ]
}
JSON
python3 - "$SPEC_FREEZE_TARGET/.shiki/migrations/state.json" <<'PY'
import json
import sys

path = sys.argv[1]
state = json.load(open(path))
state["applied"] = [record for record in state["applied"] if record["id"] != "M-20260612-0001-spec-freeze"]
json.dump(state, open(path, "w"), indent=2)
PY
python3 scripts/shiki.py migrate apply --target "$SPEC_FREEZE_TARGET" >/tmp/shiki-migrate-sf-dry.out
grep "spec-freeze" /tmp/shiki-migrate-sf-dry.out >/dev/null
python3 - "$SPEC_FREEZE_TARGET/.shiki/plans/P-0001.json" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
assert "spec_freeze" not in plan, "dry-run must not mutate plans"
PY
python3 scripts/shiki.py migrate apply --execute --target "$SPEC_FREEZE_TARGET" >/tmp/shiki-migrate-sf-exec.out
python3 - "$SPEC_FREEZE_TARGET/.shiki/plans/P-0001.json" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1]))
assert plan.get("spec_freeze", {}).get("status") == "frozen", "backfill missing"
PY

python3 scripts/shiki.py migrate status --json --target "$VALID" >/tmp/shiki-migrate-valid-2.json
cmp /tmp/shiki-migrate-valid.json /tmp/shiki-migrate-valid-2.json >/dev/null

python3 scripts/shiki.py migrate plan --target "$VALID" >/tmp/shiki-migrate-plan.out
grep "migrations: none" /tmp/shiki-migrate-plan.out >/dev/null

MISSING_STATE="$TMP_ROOT/missing-state"
make_target "$MISSING_STATE"
add_baseline_goal "$MISSING_STATE"
rm "$MISSING_STATE/.shiki/migrations/state.json"
expect_fail python3 scripts/shiki.py migrate status --json --target "$MISSING_STATE"
test "$(json_get /tmp/shiki-migrations-expected-fail.out valid)" = "False"

python3 scripts/shiki.py migrate plan --json --target "$MISSING_STATE" >/tmp/shiki-migrate-plan-missing.json
test "$(json_get /tmp/shiki-migrate-plan-missing.json dry_run)" = "True"
grep ".shiki/migrations/state.json" /tmp/shiki-migrate-plan-missing.json >/dev/null
if [ -f "$MISSING_STATE/.shiki/migrations/state.json" ]; then
  echo "migrate plan unexpectedly created state" >&2
  exit 1
fi

python3 scripts/shiki.py migrate apply --target "$MISSING_STATE" >/tmp/shiki-migrate-apply-dry-run.out
grep "dry-run" /tmp/shiki-migrate-apply-dry-run.out >/dev/null
grep ".shiki/migrations/state.json" /tmp/shiki-migrate-apply-dry-run.out >/dev/null
if [ -f "$MISSING_STATE/.shiki/migrations/state.json" ]; then
  echo "dry-run apply unexpectedly created state" >&2
  exit 1
fi

python3 scripts/shiki.py migrate apply --execute --target "$MISSING_STATE" >/tmp/shiki-migrate-apply-execute.out
test -f "$MISSING_STATE/.shiki/migrations/state.json"
python3 scripts/shiki.py migrate status --json --target "$MISSING_STATE" >/tmp/shiki-migrate-applied.json
test "$(json_get /tmp/shiki-migrate-applied.json valid)" = "True"
test "$(json_get /tmp/shiki-migrate-applied.json pending_count)" = "0"

python3 scripts/shiki.py migrate apply --execute --target "$MISSING_STATE" >/tmp/shiki-migrate-apply-idempotent.out
grep "migrations: none" /tmp/shiki-migrate-apply-idempotent.out >/dev/null

I_UNDERSTAND="$TMP_ROOT/i-understand"
make_target "$I_UNDERSTAND"
add_baseline_goal "$I_UNDERSTAND"
rm "$I_UNDERSTAND/.shiki/migrations/state.json"
python3 scripts/shiki.py migrate apply --i-understand --target "$I_UNDERSTAND" >/tmp/shiki-migrate-i-understand.out
test -f "$I_UNDERSTAND/.shiki/migrations/state.json"

expect_fail python3 scripts/shiki.py migrate apply --migration M-20260604-9999-missing --execute --target "$VALID"
grep "Unknown migration id" /tmp/shiki-migrations-expected-fail.out >/dev/null

INVALID_JSON="$TMP_ROOT/invalid-json"
make_target "$INVALID_JSON"
printf '{not-json' >"$INVALID_JSON/.shiki/migrations/state.json"
expect_fail python3 scripts/shiki.py migrate status --json --target "$INVALID_JSON"
grep "invalid JSON" /tmp/shiki-migrations-expected-fail.out >/dev/null

python3 - <<'PY'
from pathlib import Path
import json
import sys

root = Path.cwd()
sys.path.insert(0, str(root / "scripts"))

from shiki_migrations import validate_migration_state_data

valid = json.loads((root / ".shiki/migrations/state.json").read_text(encoding="utf-8"))

duplicate = dict(valid)
duplicate["applied"] = valid["applied"] + [valid["applied"][0]]
if not any("duplicate applied" in error for error in validate_migration_state_data(duplicate)):
    raise SystemExit("duplicate applied record fixture did not fail")

unknown = dict(valid)
unknown["applied"] = [dict(valid["applied"][0], id="M-20260604-9999-unknown")]
if not any("unknown applied" in error for error in validate_migration_state_data(unknown)):
    raise SystemExit("unknown applied migration fixture did not fail")

malformed = dict(valid)
malformed["applied"] = [{"id": "M-20260604-0001-baseline"}]
if not any("actor" in error for error in validate_migration_state_data(malformed)):
    raise SystemExit("malformed applied record fixture did not fail")

missing_baseline = dict(valid)
missing_baseline["applied"] = []
if not any("baseline migration" in error for error in validate_migration_state_data(missing_baseline)):
    raise SystemExit("missing baseline fixture did not fail")
PY

python3 - <<'PY'
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd() / "scripts"))

from shiki_migrations import Migration, apply_migrations

destructive = (
    Migration(
        id="M-20260604-9999-destructive-fixture",
        title="Destructive fixture",
        description="Fixture",
        introduced_in="test",
        destructive=True,
    ),
)
try:
    apply_migrations(Path.cwd(), dry_run=False, i_understand=False, registry=destructive)
except Exception as error:
    if "--i-understand" not in str(error):
        raise
else:
    raise SystemExit("destructive migration did not require --i-understand")
PY

python3 scripts/shiki.py doctor --json --target "$VALID" >/tmp/shiki-migrate-doctor-valid.json
test "$(finding_status /tmp/shiki-migrate-doctor-valid.json doctor.migrations.registry)" = "pass"
test "$(finding_status /tmp/shiki-migrate-doctor-valid.json doctor.migrations.state)" = "pass"
test "$(finding_status /tmp/shiki-migrate-doctor-valid.json doctor.migrations.pending)" = "pass"

PENDING="$TMP_ROOT/pending"
make_target "$PENDING"
rm "$PENDING/.shiki/migrations/state.json"
expect_fail python3 scripts/shiki.py doctor --json --target "$PENDING"
test "$(finding_status /tmp/shiki-migrations-expected-fail.out doctor.migrations.state)" = "fail"
test "$(finding_status /tmp/shiki-migrations-expected-fail.out doctor.migrations.pending)" = "warn"

python3 - <<'PY'
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path.cwd() / "scripts"))

from shiki_installer import manifest_stage_paths
from shiki_manifest import load_manifest, manifest_install_include, manifest_required_files

manifest = load_manifest(Path.cwd())
if ".shiki/migrations/state.json" not in manifest_required_files(manifest):
    raise SystemExit("manifest required files omit migration state")
if ".shiki/migrations/state.json" not in manifest_install_include(manifest):
    raise SystemExit("manifest install include omits migration state")
if ".shiki/migrations/state.json" not in manifest_stage_paths(Path.cwd()):
    raise SystemExit("manifest staging omits migration state")
PY

echo "shiki migration tests passed"
