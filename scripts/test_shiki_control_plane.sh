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
python3 -m py_compile scripts/shiki_schema.py
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

expect_fail env \
  CCA_VERDICT_FILE=/tmp/shiki-cca-invalid-complete.json \
  STRUCTURED_OUTPUT='{"verdict":"complete"}' \
  python3 scripts/enforce_cca_verdict.py
grep "missing required property" /tmp/shiki-expected-fail.out >/dev/null

expect_fail env \
  CCA_VERDICT_FILE=/tmp/shiki-cca-invalid-repair.json \
  STRUCTURED_OUTPUT='{"verdict":"repair_required","summary":"needs repair","goal_id":"G-0001","task_id":"T-0001","pr":1,"head_sha":"abc123","can_merge":false,"checklist":[],"acceptance":[{"id":"A1","status":"fail"}],"mergegate":{},"confidence":0.5,"repair_packet":null}' \
  python3 scripts/enforce_cca_verdict.py
grep "repair_required verdict must include a non-null object" /tmp/shiki-expected-fail.out >/dev/null

env \
  CCA_VERDICT_FILE=/tmp/shiki-cca-valid-complete.json \
  STRUCTURED_OUTPUT='{"verdict":"complete","summary":"complete","goal_id":"G-0001","task_id":"T-0001","pr":1,"head_sha":"abc123","can_merge":true,"checklist":[{"id":"CCA-01","status":"pass","blocking":true,"evidence":"fixture"}],"acceptance":[{"id":"A1","status":"pass","evidence":"fixture"}],"mergegate":{"required_checks":"pass"},"confidence":1.0,"repair_packet":null}' \
  python3 scripts/enforce_cca_verdict.py >/tmp/shiki-cca-valid-complete.out
grep "CCA verdict complete" /tmp/shiki-cca-valid-complete.out >/dev/null

mkdir -p "$TARGET"
python3 scripts/shiki.py install-target "$TARGET" --local-only >/tmp/shiki-control-install.out

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
ledger_numbers = [int(path.stem.split("-")[1]) for path in (target / ".shiki" / "ledger").glob("L-*.json")]
ledger_id = f"L-{max(ledger_numbers, default=0) + 1:04d}"
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
    "reviewDecision": "",
    "reviews": [],
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
    "acceptance": [{"id": "A1", "status": "pass", "evidence": "fixture"}],
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
cca["acceptance"] = [{"id": "A1", "status": "pass", "evidence": "fixture"}]
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
pr["labels"] = [{"name": "review:required"}]
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
pr["reviews"] = []
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
