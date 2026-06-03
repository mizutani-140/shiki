#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-control-plane-test-$$"
TARGET="$TMP_ROOT/target"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

json_get() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

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
python3 -m py_compile scripts/shiki_state.py
python3 -m py_compile scripts/shiki_schema.py
python3 -m py_compile scripts/shiki_contracts.py
python3 scripts/shiki.py --help | grep -E "goal|issue|dispatch|repair" >/dev/null
python3 scripts/shiki.py runner --help | grep "codex" >/dev/null
python3 scripts/shiki.py goal --help | grep "complete" >/dev/null
python3 scripts/shiki.py issue --help | grep "plan" >/dev/null
grep "goal create" .codex/skills/shiki/SKILL.md >/dev/null
grep "Register durable state" .claude/commands/shiki.md >/dev/null
grep "shiki runner codex --target TARGET --task-id T-XXXX" .claude/commands/shiki.md >/dev/null
grep "manual \`codex\` command" .codex/skills/shiki/SKILL.md >/dev/null
grep "operator's requested Target Repository" .claude/commands/shiki.md >/dev/null
grep "not automatically the requested Target" .codex/skills/shiki/SKILL.md >/dev/null
grep -- "--max-turns 60" .github/workflows/shiki-claude-review.yml >/dev/null
grep "python3 -m py_compile scripts/\\*.py" .github/workflows/shiki-validate.yml >/dev/null
grep "for script in scripts/test_shiki_\\*.sh" .github/workflows/shiki-validate.yml >/dev/null
grep '"head_sha"' docs/agents/completion-check-agent.md >/dev/null
grep '"can_merge"' docs/agents/completion-check-agent.md >/dev/null
grep "contents: read" .github/workflows/shiki-orchestrator.yml >/dev/null
grep "commit-evidence:" .github/workflows/shiki-orchestrator.yml >/dev/null
grep 'git push -u origin "$evidence_branch"' .github/workflows/shiki-orchestrator.yml >/dev/null
grep "gh pr create" .github/workflows/shiki-orchestrator.yml >/dev/null
grep "CANONICAL_CCA_VERDICT_SCHEMA_PATH" scripts/shiki_contracts.py >/dev/null
grep "CANONICAL_REPAIR_PACKET_SCHEMA_PATH" scripts/shiki_contracts.py >/dev/null
grep "CANONICAL_SOURCE_OF_TRUTH_ORDER" scripts/shiki_contracts.py >/dev/null
grep "CODEOWNERS_CRITICAL_PATHS" scripts/shiki_contracts.py >/dev/null
grep "validate_codeowners_governance" scripts/validate_shiki.py >/dev/null
grep -F "/.github/workflows/* @mizutani-140" .github/CODEOWNERS >/dev/null
grep -F "/scripts/mergegate_check.py @mizutani-140" .github/CODEOWNERS >/dev/null
grep -F "/AGENTS.md @mizutani-140" .github/CODEOWNERS >/dev/null
grep ".shiki/schemas/cca-verdict.schema.json" AGENTS.md SYSTEM_PROMPT.md CLAUDE.md >/dev/null
if grep -R ".shiki/templates/cca-verdict.schema.json" AGENTS.md SYSTEM_PROMPT.md CLAUDE.md .codex .claude .github/prompts docs/agents skills/engineering >/tmp/shiki-obsolete-schema-paths.out; then
  cat /tmp/shiki-obsolete-schema-paths.out >&2
  exit 1
fi
if grep -R ".shiki/templates/repair-packet.schema.json" AGENTS.md SYSTEM_PROMPT.md CLAUDE.md .codex .claude .github/prompts docs/agents skills/engineering >/tmp/shiki-obsolete-repair-schema-paths.out; then
  cat /tmp/shiki-obsolete-repair-schema-paths.out >&2
  exit 1
fi
test -f skills/engineering/shiki/SKILL.md
grep '"status"' .shiki/schemas/goal.schema.json >/dev/null
grep '"historical"' .shiki/schemas/goal.schema.json >/dev/null
grep '"cca-verdict"' .shiki/schemas/ledger.schema.json >/dev/null
grep '"criterion"' .shiki/schemas/cca-verdict.schema.json >/dev/null
grep '"insufficient_evidence"' .shiki/schemas/cca-verdict.schema.json >/dev/null
grep 'github_token: \${{ github.token }}' .github/workflows/shiki-cca-completion.yml >/dev/null
grep "CCA Review Bridge" .github/workflows/shiki-cca-completion.yml >/dev/null
grep "author,headRefName" .github/workflows/shiki-cca-completion.yml >/dev/null
grep "BOT_LOGIN: github-actions\\[bot\\]" .github/workflows/shiki-cca-completion.yml >/dev/null
grep "Cannot submit CCA Review Bridge approval: authenticated identity is PR author" .github/workflows/shiki-cca-completion.yml >/dev/null
grep "already_approved" .github/workflows/shiki-cca-completion.yml >/dev/null
grep 'repos/${REPOSITORY}/pulls/${PR_NUMBER}/reviews' .github/workflows/shiki-cca-completion.yml >/dev/null
grep '"https://api.github.com/repos/${REPOSITORY}/pulls/${PR_NUMBER}/reviews"' .github/workflows/shiki-cca-completion.yml >/dev/null
grep "REST create review HTTP status" .github/workflows/shiki-cca-completion.yml >/dev/null
grep "create-review-response.json" .github/workflows/shiki-cca-completion.yml >/dev/null
grep "can_approve_pull_request_reviews" .github/workflows/shiki-cca-completion.yml >/dev/null
grep "This is not advisory Claude review" .github/workflows/shiki-cca-completion.yml >/dev/null
grep "reviewDecision,statusCheckRollup" .github/workflows/shiki-cca-completion.yml >/dev/null

python3 - "$ROOT" <<'PY'
import json
import pathlib
import re
import sys
import tempfile
import threading

root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts"))
import shiki_state

ids = [shiki_state.new_control_id("L") for _ in range(200)]
assert len(ids) == len(set(ids))
assert all(re.match(r"^L-\d{8}T\d{12}Z-[0-9a-f]{8}$", value) for value in ids)
assert not any(re.match(r"^L-\d{4,}$", value) for value in ids)

with tempfile.TemporaryDirectory() as tmp:
    target = pathlib.Path(tmp)
    ledger_dir = target / ".shiki" / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "L-9999.json").write_text('{"id":"L-9999"}\n')

    create_path = target / ".shiki" / "state" / "record.json"
    shiki_state.atomic_create_json(create_path, {"id": "record", "value": 1})
    before = create_path.read_text()
    try:
        shiki_state.atomic_create_json(create_path, {"id": "record", "value": 2})
    except FileExistsError:
        pass
    else:
        raise AssertionError("atomic_create_json must fail on existing files")
    assert create_path.read_text() == before

    replace_path = target / ".shiki" / "state" / "replace.json"
    shiki_state.atomic_replace_json(replace_path, {"id": "replace", "value": 1})
    shiki_state.atomic_replace_json(replace_path, {"id": "replace", "value": 2})
    assert json.loads(replace_path.read_text())["value"] == 2
    assert not list(replace_path.parent.glob("*.tmp"))

    values = iter(["L-20260603T121530123456Z-deadbeef", "L-20260603T121530123457Z-feedface"])
    original = shiki_state.new_control_id
    shiki_state.new_control_id = lambda prefix: next(values)
    try:
        (ledger_dir / "L-20260603T121530123456Z-deadbeef.json").write_text('{"id":"existing","value":1}\n')
        ledger_id = shiki_state.append_ledger_entry(
            target,
            lambda candidate: {
                "id": candidate,
                "timestamp": "2026-06-03T00:00:00+00:00",
                "goal_id": "G-0001",
                "type": "check",
                "actor": "test",
                "summary": "collision retry",
                "evidence": ["retry"],
            },
            retries=2,
        )
    finally:
        shiki_state.new_control_id = original
    assert ledger_id == "L-20260603T121530123457Z-feedface"
    assert json.loads((ledger_dir / "L-20260603T121530123456Z-deadbeef.json").read_text())["id"] == "existing"

    created: list[str] = []
    lock = threading.Lock()

    def append_one() -> None:
        new_id = shiki_state.append_ledger_entry(
            target,
            lambda candidate: {
                "id": candidate,
                "timestamp": "2026-06-03T00:00:00+00:00",
                "goal_id": "G-0001",
                "type": "check",
                "actor": "test",
                "summary": "concurrent append",
                "evidence": ["thread"],
            },
        )
        with lock:
            created.append(new_id)

    threads = [threading.Thread(target=append_one) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(created) == len(set(created)) == 20
    assert not (ledger_dir / "L-10000.json").exists()
PY

expect_fail env \
  CCA_VERDICT_FILE=/tmp/shiki-cca-invalid-complete.json \
  STRUCTURED_OUTPUT='{"verdict":"complete"}' \
  python3 scripts/enforce_cca_verdict.py
grep "missing required property" /tmp/shiki-expected-fail.out >/dev/null

expect_fail env \
  CCA_VERDICT_FILE=/tmp/shiki-cca-invalid-repair.json \
  STRUCTURED_OUTPUT='{"verdict":"repair_required","summary":"needs repair","goal_id":"G-0001","task_id":"T-0001","pr":1,"head_sha":"abc123","can_merge":false,"checklist":[],"acceptance":[{"criterion":"A1","status":"fail","evidence":["fixture"]}],"mergegate":{},"confidence":0.5,"repair_packet":null}' \
  python3 scripts/enforce_cca_verdict.py
grep "repair_required verdict must include a non-null object" /tmp/shiki-expected-fail.out >/dev/null

env \
  CCA_VERDICT_FILE=/tmp/shiki-cca-valid-complete.json \
  STRUCTURED_OUTPUT='{"verdict":"complete","summary":"complete","goal_id":"G-0001","task_id":"T-0001","pr":1,"head_sha":"abc123","can_merge":true,"checklist":[{"id":"CCA-01","status":"pass","blocking":true,"evidence":"fixture"}],"acceptance":[{"criterion":"A1","status":"pass","evidence":["fixture"]}],"mergegate":{"required_checks":"pass"},"confidence":1.0,"repair_packet":null}' \
  python3 scripts/enforce_cca_verdict.py >/tmp/shiki-cca-valid-complete.out
grep "CCA verdict complete" /tmp/shiki-cca-valid-complete.out >/dev/null

mkdir -p "$TARGET"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-control-install.out
test -f "$TARGET/.github/CODEOWNERS"
test -f "$TARGET/scripts/shiki_state.py"
test -f "$TARGET/skills/engineering/shiki/SKILL.md"
test -f "$TARGET/skills/engineering/grill-with-docs/SKILL.md"

printf '\nObsolete schema path: .shiki/templates/cca-verdict.schema.json\n' >>"$TARGET/AGENTS.md"
expect_fail python3 "$TARGET/scripts/validate_shiki.py"
grep "obsolete CCA schema path" /tmp/shiki-expected-fail.out >/dev/null
python3 - "$TARGET/AGENTS.md" <<'PY'
import pathlib
path = pathlib.Path(__import__("sys").argv[1])
text = path.read_text()
path.write_text(text.replace("\nObsolete schema path: .shiki/templates/cca-verdict.schema.json\n", ""))
PY

printf '\nObsolete repair path: .shiki/templates/repair-packet.schema.json\n' >>"$TARGET/CLAUDE.md"
expect_fail python3 "$TARGET/scripts/validate_shiki.py"
grep "obsolete repair packet schema path" /tmp/shiki-expected-fail.out >/dev/null
python3 - "$TARGET/CLAUDE.md" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
path.write_text(text.replace("\nObsolete repair path: .shiki/templates/repair-packet.schema.json\n", ""))
PY

python3 - "$TARGET/SYSTEM_PROMPT.md" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
path.write_text(text.replace("GitHub Issues, Pull Requests, Checks, Reviews, comments, and merge evidence", "Current conversation"))
PY
expect_fail python3 "$TARGET/scripts/validate_shiki.py"
grep "canonical source-of-truth block" /tmp/shiki-expected-fail.out >/dev/null
python3 - "$TARGET/SYSTEM_PROMPT.md" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
path.write_text(text.replace("Current conversation", "GitHub Issues, Pull Requests, Checks, Reviews, comments, and merge evidence"))
PY

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
ledger = target / ".shiki" / "ledger"
goal = target / ".shiki" / "goals"
goal.mkdir(parents=True, exist_ok=True)
(goal / "G-9999.json").write_text(json.dumps({
    "acceptance_evidence": ["validator fixture"],
    "completion_conditions": ["validator fixture"],
    "id": "G-9999",
    "non_goals": [],
    "outcome": "validator fixture",
    "required_skills": ["tdd"],
    "risk_level": "low",
    "status": "historical",
    "title": "Validator fixture",
}, indent=2, sort_keys=True) + "\n")

def write_ledger(path_name: str, entry_id: str) -> None:
    payload = {
        "actor": "validator-test",
        "evidence": ["validator id fixture"],
        "goal_id": "G-9999",
        "id": entry_id,
        "links": [],
        "summary": "validator id fixture",
        "timestamp": "2026-06-03T00:00:00+00:00",
        "type": "check",
    }
    (ledger / path_name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

write_ledger("L-9999.json", "L-9999")
write_ledger("L-20260603T121530123456Z-a1b2c3d4.json", "L-20260603T121530123456Z-a1b2c3d4")
PY
python3 "$TARGET/scripts/validate_shiki.py"

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys
target = pathlib.Path(sys.argv[1])
payload = {
    "actor": "validator-test",
    "evidence": ["filename mismatch fixture"],
    "goal_id": "G-9999",
    "id": "L-20260603T121530123456Z-a1b2c3d4",
    "links": [],
    "summary": "filename mismatch fixture",
    "timestamp": "2026-06-03T00:00:00+00:00",
    "type": "check",
}
(target / ".shiki" / "ledger" / "L-20260603T121530123457Z-a1b2c3d4.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/validate_shiki.py"
grep "duplicate id" /tmp/shiki-expected-fail.out >/dev/null
rm "$TARGET/.shiki/ledger/L-20260603T121530123457Z-a1b2c3d4.json"

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys
target = pathlib.Path(sys.argv[1])
payload = {
    "actor": "validator-test",
    "evidence": ["filename mismatch fixture"],
    "goal_id": "G-9999",
    "id": "L-20260603T121530123458Z-a1b2c3d4",
    "links": [],
    "summary": "filename mismatch fixture",
    "timestamp": "2026-06-03T00:00:00+00:00",
    "type": "check",
}
(target / ".shiki" / "ledger" / "L-20260603T121530123459Z-a1b2c3d4.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/validate_shiki.py"
grep "file name must match id" /tmp/shiki-expected-fail.out >/dev/null
rm "$TARGET/.shiki/ledger/L-20260603T121530123459Z-a1b2c3d4.json"

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys
target = pathlib.Path(sys.argv[1])
payload = {
    "actor": "validator-test",
    "evidence": ["malformed id fixture"],
    "goal_id": "G-9999",
    "id": "L-not-valid",
    "links": [],
    "summary": "malformed id fixture",
    "timestamp": "2026-06-03T00:00:00+00:00",
    "type": "check",
}
(target / ".shiki" / "ledger" / "L-not-valid.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/validate_shiki.py"
grep "id must match L-0001 or L-YYYYMMDDTHHMMSSffffffZ-<8 hex>" /tmp/shiki-expected-fail.out >/dev/null
rm "$TARGET/.shiki/ledger/L-not-valid.json"

cd "$TARGET"
git init -b main >/tmp/shiki-control-git-init.out
# Hermetic git identity so `git commit` works in CI where no global git user is configured.
git config user.email "shiki-test@example.com"
git config user.name "Shiki Test"
git remote add origin https://github.com/example/shiki-control-plane-test.git

python3 "$ROOT/scripts/shiki.py" goal create \
  --target "$TARGET" \
  --title "Ship searchable audit trail" \
  --outcome "Operators can search task evidence from GitHub PR records" \
  --completion-condition "All task slices have done status" \
  --completion-condition "CCA and MergeGate evidence exists" \
  --required-skill grill-with-docs \
  --required-skill tdd \
  >/tmp/shiki-goal-create.json

GOAL_ID="$(json_get /tmp/shiki-goal-create.json goal_id)"
case "$GOAL_ID" in
  G-[0-9][0-9][0-9][0-9]) echo "goal id unexpectedly used legacy max-plus-one format: $GOAL_ID" >&2; exit 1 ;;
  G-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *) echo "unexpected goal id: $GOAL_ID" >&2; exit 1 ;;
esac
test -f "$TARGET/.shiki/goals/$GOAL_ID.json"

python3 "$ROOT/scripts/shiki.py" issue plan \
  --target "$TARGET" \
  --goal-id "$GOAL_ID" \
  --title "Search audit evidence by task" \
  --scope "Add the smallest vertical slice for searching task evidence" \
  --acceptance-check "A user can query task evidence by task id" \
  --acceptance-check "Verification command records evidence" \
  --lock "path:src/audit/*" \
  --required-skill tdd \
  >/tmp/shiki-issue-plan.json

TASK_ID="$(json_get /tmp/shiki-issue-plan.json task_id)"
case "$TASK_ID" in
  T-[0-9][0-9][0-9][0-9]) echo "task id unexpectedly used legacy max-plus-one format: $TASK_ID" >&2; exit 1 ;;
  T-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *) echo "unexpected task id: $TASK_ID" >&2; exit 1 ;;
esac
test -f "$TARGET/.shiki/tasks/$TASK_ID.json"
test -f "$TARGET/.shiki/dag/$GOAL_ID.json"

python3 "$ROOT/scripts/shiki.py" lock acquire --target "$TARGET" "$TASK_ID" >/tmp/shiki-lock.json
python3 "$ROOT/scripts/shiki.py" dispatch check --target "$TARGET" "$TASK_ID" >/tmp/shiki-dispatch.json
python3 "$ROOT/scripts/shiki.py" worktree allocate --target "$TARGET" "$TASK_ID" >/tmp/shiki-worktree.json
test -f "$TARGET/.shiki/worktrees/$TASK_ID.json"

python3 "$ROOT/scripts/shiki.py" repair packet \
  --target "$TARGET" \
  --task-id "$TASK_ID" \
  --pr 123 \
  --failing-item "missing verification evidence" \
  --minimal-change "add the requested verification evidence only" \
  --required-skill evidence-only \
  --verification-command "python3 scripts/validate_shiki.py" \
  >/tmp/shiki-repair.json
python3 -c 'import json; out=json.load(open("/tmp/shiki-repair.json")); packet=json.load(open(out["repair_file"])); assert packet["required_skill"] == "evidence-only"'

python3 - "$TARGET" "$TASK_ID" "$GOAL_ID" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
task_id = sys.argv[2]
goal_id = sys.argv[3]
task_path = target / ".shiki" / "tasks" / f"{task_id}.json"
task = json.loads(task_path.read_text())
task["expected_pr"] = 123
task["status"] = "review"
ledger_id = "L-9999"
while (target / ".shiki" / "ledger" / f"{ledger_id}.json").exists():
    number = int(ledger_id.split("-")[1]) - 1
    ledger_id = f"L-{number:04d}"
task["ledger_evidence"].append(ledger_id)
task_path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n")

ledger = {
    "actor": "codex-front",
    "evidence": ["diagnose skill used before bounded repair", "tdd verification recorded"],
    "goal_id": goal_id,
    "id": ledger_id,
    "links": ["https://github.com/example/shiki-control-plane-test/pull/123"],
    "summary": "diagnose and tdd evidence for MergeGate contract test PR #123",
    "task_id": task_id,
    "timestamp": "2026-01-01T00:00:00+00:00",
    "type": "review",
}
(target / ".shiki" / "ledger" / f"{ledger_id}.json").write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")

gha = target / ".shiki" / "gha"
gha.mkdir(parents=True, exist_ok=True)
body = f"""## Task
- Goal: {goal_id}
- Task: {task_id}

## Scope
MergeGate contract test.

## Acceptance
Policy validates task contract.

## Evidence
Local contract fixture.

## MergeGate
Locks are declared in task metadata.
"""
pr = {
    "number": 123,
    "body": body,
    "headRefName": task["expected_branch"],
    "headRefOid": "abc123",
    "labels": [],
    "reviewDecision": "APPROVED",
    "reviews": [{"state": "APPROVED", "author": {"login": "human-reviewer"}}],
    "statusCheckRollup": [
        {
            "name": "Validate Shiki mirror",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "headSha": "abc123",
        },
        {
            "name": "CCA verdict",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "headSha": "abc123",
        },
        {
            "name": "MergeGate metadata check",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "headSha": "abc123",
        },
        {
            "name": "MergeGate policy check",
            "status": "IN_PROGRESS",
            "conclusion": None,
            "headSha": "abc123",
        },
    ],
}
(gha / "pr.json").write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")
(gha / "changed-files.txt").write_text("src/audit/query.py\n")
cca = {
    "verdict": "complete",
    "summary": "fixture complete",
    "goal_id": goal_id,
    "task_id": task_id,
    "pr": 123,
    "head_sha": "abc123",
    "can_merge": True,
    "checklist": [],
    "acceptance": [{"criterion": "A1", "status": "pass", "evidence": ["fixture"]}],
    "mergegate": {"required_checks": "pass"},
    "confidence": 1,
}
(gha / "cca-verdict.json").write_text(json.dumps(cca, indent=2, sort_keys=True) + "\n")
PY
python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json" \
  >/tmp/shiki-mergegate-pass.json
grep '"mergegate": "ready"' /tmp/shiki-mergegate-pass.json >/dev/null

python3 - "$TARGET" <<'PY'
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "config.yaml"
text = path.read_text()
path.write_text(text.replace("    - Validate Shiki mirror\n", "    - Validate Shiki mirror\n    - Missing required job\n"))
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "Required check Missing required job is not defined by workflow job names" /tmp/shiki-expected-fail.out >/dev/null
python3 - "$TARGET" <<'PY'
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "config.yaml"
path.write_text(path.read_text().replace("    - Missing required job\n", ""))
PY

test -f "$TARGET/.shiki/gha/mergegate-result.json"
printf 'src/other.py\n' >"$TARGET/.shiki/gha/changed-files.txt"
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "outside declared task locks" /tmp/shiki-expected-fail.out >/dev/null
printf 'src/audit/query.py\n' >"$TARGET/.shiki/gha/changed-files.txt"

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "cca-verdict.json"
cca = json.loads(path.read_text())
del cca["head_sha"]
path.write_text(json.dumps(cca, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "CCA verdict schema violation" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "cca-verdict.json"
cca = json.loads(path.read_text())
cca["head_sha"] = "abc123"
cca["acceptance"] = []
path.write_text(json.dumps(cca, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "CCA verdict acceptance evidence is empty" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "cca-verdict.json"
cca = json.loads(path.read_text())
cca["acceptance"] = [{"criterion": "A1", "status": "pass", "evidence": ["fixture"]}]
cca["checklist"] = [{"id": "CCA-99", "status": "fail", "blocking": True}]
path.write_text(json.dumps(cca, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "blocking failed checklist items" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "cca-verdict.json"
cca = json.loads(path.read_text())
cca["checklist"] = []
cca["task_id"] = "T-9999"
path.write_text(json.dumps(cca, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "CCA task_id" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" "$TASK_ID" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
task_id = sys.argv[2]
path = target / ".shiki" / "gha" / "cca-verdict.json"
cca = json.loads(path.read_text())
cca["task_id"] = task_id
path.write_text(json.dumps(cca, indent=2, sort_keys=True) + "\n")
PY

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "pr.json"
pr = json.loads(path.read_text())
pr["statusCheckRollup"][0]["conclusion"] = "FAILURE"
path.write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "Required check Validate Shiki mirror is not successful" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "pr.json"
pr = json.loads(path.read_text())
pr["statusCheckRollup"][0]["conclusion"] = "SUCCESS"
pr["statusCheckRollup"][0]["headSha"] = "old-sha"
path.write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "does not match PR headRefOid" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "pr.json"
pr = json.loads(path.read_text())
pr["statusCheckRollup"][0]["headSha"] = "abc123"
pr["reviewDecision"] = ""
pr["reviews"] = []
pr["labels"] = []
pr["statusCheckRollup"].append(
    {
        "name": "Claude review",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "headSha": "abc123",
    }
)
path.write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "Required review is missing" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "pr.json"
pr = json.loads(path.read_text())
pr["reviewDecision"] = "APPROVED"
pr["reviews"] = [{"state": "APPROVED", "author": {"login": "human-reviewer"}}]
pr["labels"] = [{"name": "review:required"}]
path.write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")
PY
python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json" \
  >/tmp/shiki-mergegate-review-pass.json
grep '"mergegate": "ready"' /tmp/shiki-mergegate-review-pass.json >/dev/null

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "pr.json"
pr = json.loads(path.read_text())
pr["reviewDecision"] = ""
pr["reviews"] = [{"state": "APPROVED", "author": {"login": "github-actions[bot]"}}]
path.write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")
PY
python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json" \
  >/tmp/shiki-mergegate-actions-bot-review-pass.json
grep '"mergegate": "ready"' /tmp/shiki-mergegate-actions-bot-review-pass.json >/dev/null

python3 - "$TARGET" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
path = target / ".shiki" / "gha" / "pr.json"
pr = json.loads(path.read_text())
pr["labels"] = []
pr["reviews"] = [{"state": "CHANGES_REQUESTED", "author": {"login": "reviewer"}}]
path.write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "review requested changes" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" "$TASK_ID" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
task_id = sys.argv[2]
pr_path = target / ".shiki" / "gha" / "pr.json"
pr = json.loads(pr_path.read_text())
pr["labels"] = []
pr["reviewDecision"] = "APPROVED"
pr["reviews"] = [{"state": "APPROVED", "author": {"login": "human-reviewer"}}]
pr_path.write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n")

task_path = target / ".shiki" / "tasks" / f"{task_id}.json"
task = json.loads(task_path.read_text())
task["risk_level"] = "high"
task_path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "Guardian approval is required" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" "$TASK_ID" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
task_id = sys.argv[2]
ledger_path = next((target / ".shiki" / "ledger").glob("L-*.json"))
ledger = json.loads(ledger_path.read_text())
ledger["evidence"].append("no Guardian approval evidence is present")
ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")

task_path = target / ".shiki" / "tasks" / f"{task_id}.json"
task = json.loads(task_path.read_text())
task["risk_level"] = "high"
task_path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "Guardian approval is required" /tmp/shiki-expected-fail.out >/dev/null

python3 - "$TARGET" "$TASK_ID" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
task_id = sys.argv[2]
config_path = target / ".shiki" / "config.yaml"
config_text = config_path.read_text()
if "    - guardian-user\n" not in config_text:
    config_text = config_text.replace("  users:\n", "  users:\n    - guardian-user\n")
    config_path.write_text(config_text)

ledger_path = next((target / ".shiki" / "ledger").glob("L-*.json"))
ledger = json.loads(ledger_path.read_text())
ledger["guardian_approval"] = {
    "approved": True,
    "approver": "guardian-user",
    "source": "structured_ledger",
}
ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")

task_path = target / ".shiki" / "tasks" / f"{task_id}.json"
task = json.loads(task_path.read_text())
task["risk_level"] = "high"
task_path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n")
PY
python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json" \
  >/tmp/shiki-mergegate-guardian-pass.json
grep '"mergegate": "ready"' /tmp/shiki-mergegate-guardian-pass.json >/dev/null

python3 - "$TARGET" "$TASK_ID" <<'PY'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
task_id = sys.argv[2]
task_path = target / ".shiki" / "tasks" / f"{task_id}.json"
task = json.loads(task_path.read_text())
task["risk_level"] = "low"
task_path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n")

lock = {
    "task_id": "T-9999",
    "goal_id": "G-9999",
    "locks": ["path:src/audit/*"],
    "state": "active",
    "owner": "other",
    "created_at": "2026-01-01T00:00:00+00:00",
}
(target / ".shiki" / "locks").mkdir(parents=True, exist_ok=True)
(target / ".shiki" / "locks" / "T-9999.json").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
PY
expect_fail python3 "$TARGET/scripts/mergegate_check.py" \
  --target "$TARGET" \
  --pr-json "$TARGET/.shiki/gha/pr.json" \
  --changed-files "$TARGET/.shiki/gha/changed-files.txt" \
  --cca-verdict "$TARGET/.shiki/gha/cca-verdict.json" \
  --result-file "$TARGET/.shiki/gha/mergegate-result.json"
grep "Lock conflict" /tmp/shiki-expected-fail.out >/dev/null
rm -f "$TARGET/.shiki/locks/T-9999.json"

python3 "$ROOT/scripts/shiki.py" task status --target "$TARGET" "$TASK_ID" --status done >/tmp/shiki-task-status.json
python3 "$ROOT/scripts/shiki.py" goal complete --target "$TARGET" "$GOAL_ID" >/tmp/shiki-goal-complete.json

python3 "$TARGET/scripts/validate_shiki.py"

echo "shiki control-plane tests passed"
