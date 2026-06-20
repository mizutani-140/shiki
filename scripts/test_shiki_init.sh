#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-init-test-$$"
FAKE_BIN="$TMP_ROOT/bin"

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

mkdir -p "$TMP_ROOT/missing-repo" "$TMP_ROOT/invalid-repo" "$TMP_ROOT/no-local" "$TMP_ROOT/local-only" "$FAKE_BIN"
export TMP_ROOT

cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"${SHIKI_FAKE_GH_LOG}"
case "$1 $2" in
  "auth status")
    exit 0
    ;;
  "repo view")
    exit 0
    ;;
  "repo create")
    echo "https://github.com/example/shiki-init-test"
    exit 0
    ;;
  "secret set")
    cat >/dev/null
    exit 0
    ;;
  "api repos/example/shiki-init-protect/branches/main/protection")
    echo "branch protection rejected" >&2
    exit 1
    ;;
  "api repos/"*"actions/permissions/workflow")
    cat >"${SHIKI_FAKE_GH_WORKFLOW_PAYLOAD:-/dev/null}"
    exit 0
    ;;
  "api repos/"*)
    cat >"${SHIKI_FAKE_GH_PAYLOAD:-/dev/null}"
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

expect_fail python3 scripts/shiki.py init "$TMP_ROOT/missing-repo"
expect_fail python3 scripts/shiki.py init "$TMP_ROOT/invalid-repo" --repo invalid-slug
expect_fail python3 scripts/shiki.py install-target "$TMP_ROOT/no-local"

python3 scripts/shiki.py install-target "$TMP_ROOT/local-only" --local-only >/tmp/shiki-local-only.out
python3 "$TMP_ROOT/local-only/scripts/validate_shiki.py"
test -z "$(find "$TMP_ROOT/local-only/.shiki/tasks" -type f -name '*.json' -print -quit)"
test -z "$(find "$TMP_ROOT/local-only/.shiki/ledger" -type f -name '*.json' -print -quit)"

expect_fail python3 scripts/shiki.py preflight "$TMP_ROOT/local-only" --require-github

DRY_RUN="$TMP_ROOT/dry-run"
: >"$SHIKI_FAKE_GH_LOG"
python3 scripts/shiki.py init "$DRY_RUN" \
  --repo example/shiki-init-dry-run >/tmp/shiki-init-dry-run.out
grep "dry-run: no bootstrap/init mutations were executed" /tmp/shiki-init-dry-run.out >/dev/null
grep "provider: github" /tmp/shiki-init-dry-run.out >/dev/null
grep "github-host: github.com" /tmp/shiki-init-dry-run.out >/dev/null
grep "remote-protocol: https" /tmp/shiki-init-dry-run.out >/dev/null
grep "git: configure origin https://github.com/example/shiki-init-dry-run.git" /tmp/shiki-init-dry-run.out >/dev/null
grep "github-api: use https://api.github.com" /tmp/shiki-init-dry-run.out >/dev/null
grep "filesystem: create target directory" /tmp/shiki-init-dry-run.out >/dev/null
grep "git: initialize repository" /tmp/shiki-init-dry-run.out >/dev/null
grep "github-repo: create or reuse example/shiki-init-dry-run" /tmp/shiki-init-dry-run.out >/dev/null
grep "secret: set CLAUDE_CODE_OAUTH_TOKEN" /tmp/shiki-init-dry-run.out >/dev/null
grep "branch-protection: configure required checks" /tmp/shiki-init-dry-run.out >/dev/null
grep "workflow-permissions: allow GitHub Actions to create and approve pull requests" /tmp/shiki-init-dry-run.out >/dev/null
grep "default-branch: set main" /tmp/shiki-init-dry-run.out >/dev/null
grep "commit: create manifest commit" /tmp/shiki-init-dry-run.out >/dev/null
grep "push: push main to origin" /tmp/shiki-init-dry-run.out >/dev/null
test ! -e "$DRY_RUN"
test -z "$(cat "$SHIKI_FAKE_GH_LOG")"

DRY_RUN_NO_PROTECT="$TMP_ROOT/dry-run-no-protect"
python3 scripts/shiki.py init "$DRY_RUN_NO_PROTECT" \
  --repo example/shiki-init-dry-run-no-protect \
  --no-protect >/tmp/shiki-init-dry-run-no-protect.out
grep "branch-protection: skipped by --no-protect" /tmp/shiki-init-dry-run-no-protect.out >/dev/null
grep "workflow-permissions: skipped by --no-protect" /tmp/shiki-init-dry-run-no-protect.out >/dev/null

DRY_RUN_NO_SECRET="$TMP_ROOT/dry-run-no-secret"
unset CLAUDE_CODE_OAUTH_TOKEN || true
python3 scripts/shiki.py init "$DRY_RUN_NO_SECRET" \
  --repo example/shiki-init-dry-run-no-secret >/tmp/shiki-init-dry-run-no-secret.out
grep "dry-run: no bootstrap/init mutations were executed" /tmp/shiki-init-dry-run-no-secret.out >/dev/null

: >"$SHIKI_FAKE_GH_LOG"
# In a bootstrapped target the checkout's origin is the target repo, not the
# platform repo; check_remote_adoption would refuse the hardcoded platform slug.
PLATFORM_REPO="mizutani-140/shiki"
if [ -f .shiki/repo.json ]; then
  PLATFORM_REPO="$(python3 -c 'import json; print(json.load(open(".shiki/repo.json"))["repo"])')"
fi
python3 scripts/shiki.py bootstrap-platform \
  --repo "$PLATFORM_REPO" >/tmp/shiki-bootstrap-platform-dry-run.out
grep "dry-run: no bootstrap/init mutations were executed" /tmp/shiki-bootstrap-platform-dry-run.out >/dev/null
grep "provider: github" /tmp/shiki-bootstrap-platform-dry-run.out >/dev/null
grep "filesystem: validate local Shiki platform files" /tmp/shiki-bootstrap-platform-dry-run.out >/dev/null
grep "github-repo: create or reuse $PLATFORM_REPO" /tmp/shiki-bootstrap-platform-dry-run.out >/dev/null
test -z "$(cat "$SHIKI_FAKE_GH_LOG")"

I_UNDERSTAND="$TMP_ROOT/i-understand"
: >"$SHIKI_FAKE_GH_LOG"
python3 scripts/shiki.py init "$I_UNDERSTAND" \
  --repo example/shiki-init-understand \
  --i-understand \
  --no-commit \
  --no-push \
  --no-set-secret \
  --no-protect >/tmp/shiki-init-understand.out
test -d "$I_UNDERSTAND/.git"
grep "auth status" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep "repo view example/shiki-init-understand --json name" "$SHIKI_FAKE_GH_LOG" >/dev/null

ORIGIN_MISMATCH="$TMP_ROOT/origin-mismatch"
mkdir -p "$ORIGIN_MISMATCH"
git -C "$ORIGIN_MISMATCH" init -b main >/tmp/shiki-init-origin-git.out
git -C "$ORIGIN_MISMATCH" remote add origin https://github.com/example/wrong-repo.git
expect_fail python3 scripts/shiki.py init "$ORIGIN_MISMATCH" \
  --repo example/shiki-init-test \
  --no-commit \
  --no-push \
  --no-set-secret \
  --no-protect
grep "origin already points" /tmp/shiki-expected-fail.out >/dev/null
test "$(git -C "$ORIGIN_MISMATCH" remote get-url origin)" = "https://github.com/example/wrong-repo.git"

ORIGIN_WITHOUT_DOT_GIT="$TMP_ROOT/origin-without-dot-git"
mkdir -p "$ORIGIN_WITHOUT_DOT_GIT"
git -C "$ORIGIN_WITHOUT_DOT_GIT" init -b main >/tmp/shiki-init-origin-url-git.out
git -C "$ORIGIN_WITHOUT_DOT_GIT" remote add origin https://github.com/example/shiki-init-test
python3 scripts/shiki.py init "$ORIGIN_WITHOUT_DOT_GIT" \
  --repo example/shiki-init-test >/tmp/shiki-init-origin-without-dot-git.out
grep "dry-run: no bootstrap/init mutations were executed" /tmp/shiki-init-origin-without-dot-git.out >/dev/null

ADOPTED="$TMP_ROOT/adopted"
mkdir -p "$ADOPTED"
git -C "$ADOPTED" init -b main >/tmp/shiki-init-adopt-git.out
git -C "$ADOPTED" remote add origin https://github.com/example/wrong-repo.git
python3 scripts/shiki.py init "$ADOPTED" \
  --repo example/shiki-init-test \
  --execute \
  --adopt-existing-repo \
  --no-commit \
  --no-push \
  --no-set-secret \
  --no-protect >/tmp/shiki-init-adopt.out
test "$(git -C "$ADOPTED" remote get-url origin)" = "https://github.com/example/shiki-init-test.git"

STAGING="$TMP_ROOT/staging"
mkdir -p "$STAGING"
printf 'do not stage me\n' >"$STAGING/unrelated.txt"
python3 scripts/shiki.py init "$STAGING" \
  --repo example/shiki-init-staging \
  --execute \
  --no-push \
  --no-set-secret \
  --no-protect >/tmp/shiki-init-staging.out
expect_fail git -C "$STAGING" ls-files --error-unmatch unrelated.txt
test -f "$STAGING/unrelated.txt"

MISSING_SECRET="$TMP_ROOT/missing-secret"
mkdir -p "$MISSING_SECRET"
unset CLAUDE_CODE_OAUTH_TOKEN || true
expect_fail python3 scripts/shiki.py init "$MISSING_SECRET" \
  --repo example/shiki-init-secret \
  --execute \
  --no-commit \
  --no-push \
  --no-protect
grep "missing required GitHub Actions secret source" /tmp/shiki-expected-fail.out >/dev/null

PROTECT_FAIL="$TMP_ROOT/protect-fail"
mkdir -p "$PROTECT_FAIL"
export CLAUDE_CODE_OAUTH_TOKEN="fake-test-token"
expect_fail python3 scripts/shiki.py init "$PROTECT_FAIL" \
  --repo example/shiki-init-protect \
  --execute \
  --no-commit \
  --no-push
grep "could not configure branch protection" /tmp/shiki-expected-fail.out >/dev/null

CONFIG_FALSE="$TMP_ROOT/config-false"
mkdir -p "$CONFIG_FALSE/.shiki"
cat >"$CONFIG_FALSE/.shiki/config.yaml" <<'YAML'
defaults:
  required_review: false
YAML
python3 - <<'PY'
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))
import shiki

root = Path(os.environ["TMP_ROOT"])
if shiki.branch_protection_review_count(Path.cwd()) < 1:
    raise SystemExit("required_review=true must require at least one approving review")
if shiki.branch_protection_review_count(root / "config-false") != 0:
    raise SystemExit("required_review=false must not force an approving review")
PY

PROTECT_PASS="$TMP_ROOT/protect-pass"
mkdir -p "$PROTECT_PASS"
export SHIKI_FAKE_GH_PAYLOAD="$TMP_ROOT/protect-payload.json"
export SHIKI_FAKE_GH_WORKFLOW_PAYLOAD="$TMP_ROOT/protect-workflow-payload.json"
: >"$SHIKI_FAKE_GH_LOG"
python3 scripts/shiki.py init "$PROTECT_PASS" \
  --repo example/shiki-init-protect-pass \
  --execute \
  --no-commit \
  --no-push >/tmp/shiki-init-protect-pass.out
grep "api repos/example/shiki-init-protect-pass/actions/permissions/workflow -X PUT" "$SHIKI_FAKE_GH_LOG" >/dev/null
python3 - "$SHIKI_FAKE_GH_WORKFLOW_PAYLOAD" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload["default_workflow_permissions"] != "read":
    raise SystemExit(f"expected default_workflow_permissions read, got {payload['default_workflow_permissions']}")
if payload["can_approve_pull_request_reviews"] is not True:
    raise SystemExit("expected can_approve_pull_request_reviews to be true")
PY
python3 - "$SHIKI_FAKE_GH_PAYLOAD" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload["required_pull_request_reviews"]["require_code_owner_reviews"] is not True:
    raise SystemExit("expected require_code_owner_reviews to be true")
count = payload["required_pull_request_reviews"]["required_approving_review_count"]
if count < 1:
    raise SystemExit(f"expected required_approving_review_count >= 1, got {count}")
contexts = payload["required_status_checks"]["contexts"]
if "MergeGate metadata check" not in contexts or "MergeGate policy check" not in contexts:
    raise SystemExit(f"branch protection contexts missing MergeGate checks: {contexts}")
PY

export SHIKI_FAKE_GH_PAYLOAD="$TMP_ROOT/protect-payload-review-false.json"
python3 - <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))
import shiki

shiki.protect_branch(
    "example/shiki-init-protect-review-false",
    "main",
    ["Validate Shiki mirror"],
    review_count=0,
)
PY
python3 - "$SHIKI_FAKE_GH_PAYLOAD" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
reviews = payload["required_pull_request_reviews"]
if reviews["require_code_owner_reviews"] is not False:
    raise SystemExit("expected require_code_owner_reviews to be false when review_count is 0")
if reviews["required_approving_review_count"] != 0:
    raise SystemExit("expected required_approving_review_count to remain 0")
PY

echo "shiki init tests passed"
