#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-start-test-$$"
TARGET="$TMP_ROOT/target"
FAKE_BIN="$TMP_ROOT/bin"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

json_get() {
  python3 -c 'import json,sys; text=open(sys.argv[1]).read(); start=text.rfind("\n{"); start = 0 if start == -1 else start + 1; print(json.loads(text[start:])[sys.argv[2]])' "$1" "$2"
}

cd "$ROOT"

python3 scripts/validate_shiki.py
python3 -m py_compile scripts/shiki.py scripts/validate_shiki.py
python3 scripts/shiki.py --help | grep "start" >/dev/null

mkdir -p "$TARGET" "$FAKE_BIN"

# Record a target's GitHub identity BEFORE its first start. The installer wires
# the per-target CODEOWNERS owner from .shiki/repo.json, so a target that already
# knows it is example/shiki-start-test installs a CODEOWNERS naming @example; the
# start flow's own write_target_repo_config later overwrites this seed with the
# full config. Without it, start's install (which runs before repo.json is
# written, and keeps the existing CODEOWNERS on a re-run) would ship the
# maintainer's owner, and the target's own validate_shiki — run at the end of
# install and again at line ~151 — would then fail its CODEOWNERS check for a
# foreign owner.
seed_repo_json() {
  mkdir -p "$1/.shiki"
  cat >"$1/.shiki/repo.json" <<'JSON'
{
  "source_of_truth": "github",
  "default_branch": "main",
  "mirror": ".shiki",
  "provider": "github",
  "repo": "example/shiki-start-test",
  "host": "github.com",
  "remote_protocol": "https"
}
JSON
}
seed_repo_json "$TARGET"

cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"${SHIKI_FAKE_GH_LOG}"
case "$1 $2" in
  "auth status")
    exit 0
    ;;
  "repo view")
    exit 1
    ;;
  "repo create")
    echo "https://github.com/example/shiki-start-test"
    exit 0
    ;;
  "issue create")
    echo "https://github.com/example/shiki-start-test/issues/101"
    exit 0
    ;;
  "secret set")
    cat >/dev/null
    exit 0
    ;;
  "secret list")
    echo "CLAUDE_CODE_OAUTH_TOKEN"
    exit 0
    ;;
  "api repos/"*"actions/permissions/workflow")
    cat >"${SHIKI_FAKE_GH_WORKFLOW_PAYLOAD:-/dev/null}"
    exit 0
    ;;
  "api repos/"*"/protection")
    cat >/dev/null
    exit 0
    ;;
  "api repos/"*)
    cat >/dev/null
    exit 0
    ;;
esac
echo "fake gh unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/gh"
export PATH="$FAKE_BIN:$PATH"
export SHIKI_FAKE_GH_LOG="$TMP_ROOT/gh.log"
export GIT_AUTHOR_NAME="Shiki Test"
export GIT_AUTHOR_EMAIL="shiki-test@example.local"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
export CLAUDE_CODE_OAUTH_TOKEN="fake-test-token"

cat >"$TMP_ROOT/answers.json" <<'JSON'
{
  "repo": "example/shiki-start-test",
  "project_name": "Shiki Start Test",
  "goal": "Ship a one command Shiki start flow",
  "outcome": "A user can run one command and receive a GitHub-first Shiki project with task evidence",
  "completion_conditions": [
    "The first generated task is dispatchable",
    "A GitHub issue exists for the first task"
  ],
  "non_goals": [
    "Do not require manual shiki init before start",
    "Do not bypass grill-with-docs"
  ],
  "risk_level": "medium",
  "required_skills": ["grill-with-docs", "to-prd", "to-issues", "tdd"],
  "approve_spec_freeze": true,
  "tasks": [
    {
      "title": "Create one command start path",
      "scope": "Initialize the repo, persist the grilled plan, run Shiki orchestration, and create the first GitHub issue",
      "acceptance_checks": ["One command creates Shiki run state and issue evidence"],
      "locks": ["path:src/start/*"],
      "required_skills": ["tdd"]
    }
  ]
}
JSON

# Start without explicit Spec Freeze approval must fail (SADR-0009).
python3 - "$TMP_ROOT/answers.json" "$TMP_ROOT/answers-unapproved.json" <<'PY'
import json
import sys

answers = json.load(open(sys.argv[1]))
answers.pop("approve_spec_freeze", None)
json.dump(answers, open(sys.argv[2], "w"), indent=2)
PY
if python3 scripts/shiki.py start "$TARGET" --answers-file "$TMP_ROOT/answers-unapproved.json" --execute --no-push --no-protect </dev/null 2>/tmp/shiki-start-unapproved.out; then
  echo "expected start without spec-freeze approval to fail" >&2
  exit 1
fi
grep "Spec Freeze was not approved" /tmp/shiki-start-unapproved.out >/dev/null

python3 scripts/shiki.py start \
  "$TARGET" \
  --answers-file "$TMP_ROOT/answers.json" \
  --execute \
  --no-push \
  --no-protect \
  >/tmp/shiki-start.json

test -f "$TARGET/.shiki/repo.json"
test -n "$(find "$TARGET/.shiki/plans" -type f -name 'P-*.json' -print -quit)"
test -n "$(find "$TARGET/.shiki/runs" -type f -name 'RUN-*.json' -print -quit)"
test -n "$(find "$TARGET/.shiki/starts" -type f -name 'START-*.json' -print -quit)"
test -n "$(find "$TARGET/.shiki/tasks" -type f -name 'T-*.json' -print -quit)"
grep "repo create" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep "issue create" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep "secret set CLAUDE_CODE_OAUTH_TOKEN --repo example/shiki-start-test" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep '"configured": true' /tmp/shiki-start.json >/dev/null

START_ID="$(json_get /tmp/shiki-start.json start_id)"
GOAL_ID="$(json_get /tmp/shiki-start.json goal_id)"
SKILLS_DIR="$(json_get /tmp/shiki-start.json skills_dir)"
case "$START_ID" in
  START-[0-9][0-9][0-9][0-9] | START-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *) echo "unexpected start id: $START_ID" >&2; exit 1 ;;
esac
test -f "$TARGET/.shiki/starts/$START_ID.json"
test -n "$SKILLS_DIR"
test -f "$TARGET/.shiki/goals/$GOAL_ID.json"

python3 "$TARGET/scripts/validate_shiki.py"

# Protect-enabled start must configure GitHub Actions workflow permissions for
# the CCA Review Bridge (default=read, can-approve=true) right after branch
# protection, through cmd_start -> cmd_init (SADR-0013).
PROTECT_TARGET="$TMP_ROOT/protect-target"
mkdir -p "$PROTECT_TARGET"
: >"$SHIKI_FAKE_GH_LOG"
export SHIKI_FAKE_GH_WORKFLOW_PAYLOAD="$TMP_ROOT/start-workflow-payload.json"
python3 scripts/shiki.py start \
  "$PROTECT_TARGET" \
  --answers-file "$TMP_ROOT/answers.json" \
  --execute \
  --no-push \
  >/tmp/shiki-start-protect.json
grep "api repos/example/shiki-start-test/actions/permissions/workflow -X PUT" "$SHIKI_FAKE_GH_LOG" >/dev/null
python3 - "$SHIKI_FAKE_GH_WORKFLOW_PAYLOAD" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload["default_workflow_permissions"] != "read":
    raise SystemExit(f"expected default_workflow_permissions read, got {payload['default_workflow_permissions']}")
if payload["can_approve_pull_request_reviews"] is not True:
    raise SystemExit("expected can_approve_pull_request_reviews to be true")
PY

# A first run that fails part-way through GitHub governance must be re-attemptable:
# start now re-runs init unconditionally, so a retry re-drives secret, branch
# protection, and workflow-permission setup instead of silently skipping them
# because .shiki/repo.json + origin already exist.
cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"${SHIKI_FAKE_GH_LOG}"
case "$*" in
  "auth status")
    exit 0
    ;;
  "repo view "*)
    exit 1
    ;;
  "repo create "*)
    echo "https://github.com/example/shiki-start-test"
    exit 0
    ;;
  "issue create "*)
    echo "https://github.com/example/shiki-start-test/issues/101"
    exit 0
    ;;
  "secret set "*)
    cat >/dev/null
    exit 0
    ;;
  "secret list "*)
    echo "CLAUDE_CODE_OAUTH_TOKEN"
    exit 0
    ;;
  *"actions/permissions/workflow"*)
    cat >/dev/null
    exit 0
    ;;
  *"/protection"*)
    n="$(cat "${SHIKI_FAKE_GH_PROTECT_COUNT}" 2>/dev/null || echo 0)"
    n=$((n + 1))
    echo "$n" >"${SHIKI_FAKE_GH_PROTECT_COUNT}"
    if [ "$n" -le "${SHIKI_FAKE_GH_PROTECT_FAILS:-0}" ]; then
      echo "branch protection rejected" >&2
      exit 1
    fi
    cat >/dev/null
    exit 0
    ;;
  "api repos/"*)
    cat >/dev/null
    exit 0
    ;;
esac
echo "fake gh unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/gh"
export SHIKI_FAKE_GH_PROTECT_COUNT="$TMP_ROOT/protect-count"

# A1: protection fails on run 1, then succeeds. Run 2 must re-attempt BOTH the
# protection and workflow-permissions calls (today both are zero).
RETRY_PROTECT="$TMP_ROOT/retry-protect"
mkdir -p "$RETRY_PROTECT"
seed_repo_json "$RETRY_PROTECT"
: >"$SHIKI_FAKE_GH_PROTECT_COUNT"
export SHIKI_FAKE_GH_PROTECT_FAILS=1
export CLAUDE_CODE_OAUTH_TOKEN="fake-test-token"
: >"$SHIKI_FAKE_GH_LOG"
if python3 scripts/shiki.py start "$RETRY_PROTECT" --answers-file "$TMP_ROOT/answers.json" --execute --no-push </dev/null >/tmp/shiki-start-retry1.out 2>&1; then
  echo "expected start run 1 to fail when branch protection is rejected" >&2
  exit 1
fi
test -f "$RETRY_PROTECT/.shiki/repo.json"
: >"$SHIKI_FAKE_GH_LOG"
python3 scripts/shiki.py start "$RETRY_PROTECT" --answers-file "$TMP_ROOT/answers.json" --execute --no-push </dev/null >/tmp/shiki-start-retry2.out
grep "branches/main/protection -X PUT" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep "actions/permissions/workflow -X PUT" "$SHIKI_FAKE_GH_LOG" >/dev/null

# A2: the runtime token is unset on run 1 (secret set never happens); run 2 with
# the token present must attempt the secret set.
RETRY_SECRET="$TMP_ROOT/retry-secret"
mkdir -p "$RETRY_SECRET"
seed_repo_json "$RETRY_SECRET"
: >"$SHIKI_FAKE_GH_PROTECT_COUNT"
export SHIKI_FAKE_GH_PROTECT_FAILS=0
unset CLAUDE_CODE_OAUTH_TOKEN
: >"$SHIKI_FAKE_GH_LOG"
if python3 scripts/shiki.py start "$RETRY_SECRET" --answers-file "$TMP_ROOT/answers.json" --execute --no-push </dev/null >/tmp/shiki-start-secret1.out 2>&1; then
  echo "expected start run 1 to fail when the runtime token is unset" >&2
  exit 1
fi
if grep -q "secret set" "$SHIKI_FAKE_GH_LOG"; then
  echo "run 1 must not have set the secret without a token" >&2
  exit 1
fi
test -f "$RETRY_SECRET/.shiki/repo.json"
export CLAUDE_CODE_OAUTH_TOKEN="fake-test-token"
: >"$SHIKI_FAKE_GH_LOG"
python3 scripts/shiki.py start "$RETRY_SECRET" --answers-file "$TMP_ROOT/answers.json" --execute --no-push </dev/null >/tmp/shiki-start-secret2.out
grep "secret set CLAUDE_CODE_OAUTH_TOKEN --repo example/shiki-start-test" "$SHIKI_FAKE_GH_LOG" >/dev/null

# A3: run 2 still exits non-zero when the underlying cause is never fixed, yet it
# still re-attempts the failing step rather than skipping it.
RETRY_UNFIXED="$TMP_ROOT/retry-unfixed"
mkdir -p "$RETRY_UNFIXED"
seed_repo_json "$RETRY_UNFIXED"
: >"$SHIKI_FAKE_GH_PROTECT_COUNT"
export SHIKI_FAKE_GH_PROTECT_FAILS=99
export CLAUDE_CODE_OAUTH_TOKEN="fake-test-token"
: >"$SHIKI_FAKE_GH_LOG"
if python3 scripts/shiki.py start "$RETRY_UNFIXED" --answers-file "$TMP_ROOT/answers.json" --execute --no-push </dev/null >/tmp/shiki-start-unfixed1.out 2>&1; then
  echo "expected start run 1 to fail while protection is rejected" >&2
  exit 1
fi
: >"$SHIKI_FAKE_GH_LOG"
if python3 scripts/shiki.py start "$RETRY_UNFIXED" --answers-file "$TMP_ROOT/answers.json" --execute --no-push </dev/null >/tmp/shiki-start-unfixed2.out 2>&1; then
  echo "expected start run 2 to still fail while the cause is unfixed" >&2
  exit 1
fi
grep "branches/main/protection -X PUT" "$SHIKI_FAKE_GH_LOG" >/dev/null

echo "shiki start tests passed"
