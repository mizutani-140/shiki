#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-doctor-test-$$"
FAKE_BIN="$TMP_ROOT/bin"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

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

expect_fail() {
  if "$@" >/tmp/shiki-doctor-expected-fail.out 2>&1; then
    echo "expected failure but command succeeded: $*" >&2
    cat /tmp/shiki-doctor-expected-fail.out >&2
    return 1
  fi
}

make_target() {
  local target="$1"
  mkdir -p "$target"
  python3 scripts/shiki.py install-target "$target" --local-only --no-validate >/tmp/shiki-doctor-install.out
  git -C "$target" init -b main >/tmp/shiki-doctor-git.out
  git -C "$target" remote add origin https://github.com/example/shiki-doctor.git
  cat >"$target/.shiki/repo.json" <<'JSON'
{
  "provider": "github",
  "repo": "example/shiki-doctor",
  "host": "github.com",
  "remote_protocol": "https",
  "web_base_url": "https://github.com",
  "api_base_url": "https://api.github.com",
  "ssh_host": "github.com",
  "canonical_remote_url": "https://github.com/example/shiki-doctor.git"
}
JSON
}

cd "$ROOT"

VALID="$TMP_ROOT/valid"
make_target "$VALID"

python3 scripts/shiki.py doctor --json --target "$VALID" >/tmp/shiki-doctor-valid.json
python3 -m json.tool /tmp/shiki-doctor-valid.json >/dev/null
case "$(json_get /tmp/shiki-doctor-valid.json status)" in
  pass|warn) ;;
  *) echo "valid fixture should pass or warn" >&2; exit 1 ;;
esac
test "$(finding_status /tmp/shiki-doctor-valid.json doctor.provider.repo_json)" = "pass"
test "$(finding_status /tmp/shiki-doctor-valid.json doctor.git.origin)" = "pass"
test "$(finding_status /tmp/shiki-doctor-valid.json doctor.workflows.required_files)" = "pass"
test "$(finding_status /tmp/shiki-doctor-valid.json doctor.codeowners.coverage)" = "pass"
test "$(finding_status /tmp/shiki-doctor-valid.json doctor.guardian.policy)" = "pass"
test "$(finding_status /tmp/shiki-doctor-valid.json doctor.guardian.approvers)" = "pass"
test "$(finding_status /tmp/shiki-doctor-valid.json doctor.worktrees.unregistered)" = "pass"
grep '"id": "doctor.contract.validate_shiki"' /tmp/shiki-doctor-valid.json >/dev/null

NO_REPO="$TMP_ROOT/no-repo"
make_target "$NO_REPO"
rm "$NO_REPO/.shiki/repo.json"
python3 scripts/shiki.py doctor --json --target "$NO_REPO" >/tmp/shiki-doctor-no-repo.json
test "$(finding_status /tmp/shiki-doctor-no-repo.json doctor.provider.repo_json)" = "warn"
grep "shiki init" /tmp/shiki-doctor-no-repo.json >/dev/null
expect_fail python3 scripts/shiki.py doctor --json --strict --target "$NO_REPO"

INVALID_PROVIDER="$TMP_ROOT/invalid-provider"
make_target "$INVALID_PROVIDER"
python3 - "$INVALID_PROVIDER/.shiki/repo.json" <<'PY'
import json
import sys

path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["provider"] = "gitlab"
json.dump(data, open(path, "w", encoding="utf-8"))
PY
expect_fail python3 scripts/shiki.py doctor --json --target "$INVALID_PROVIDER"
test "$(finding_status /tmp/shiki-doctor-expected-fail.out doctor.provider.repo_json)" = "fail"

ORIGIN_MISMATCH="$TMP_ROOT/origin-mismatch"
make_target "$ORIGIN_MISMATCH"
git -C "$ORIGIN_MISMATCH" remote set-url origin https://github.com/example/other.git
expect_fail python3 scripts/shiki.py doctor --json --target "$ORIGIN_MISMATCH"
test "$(finding_status /tmp/shiki-doctor-expected-fail.out doctor.git.origin)" = "fail"

MISSING_MANIFEST="$TMP_ROOT/missing-manifest"
make_target "$MISSING_MANIFEST"
rm "$MISSING_MANIFEST/.shiki/manifest.json"
expect_fail python3 scripts/shiki.py doctor --json --target "$MISSING_MANIFEST"
test "$(finding_status /tmp/shiki-doctor-expected-fail.out doctor.manifest.layout)" = "fail"

MISSING_CODEOWNERS="$TMP_ROOT/missing-codeowners"
make_target "$MISSING_CODEOWNERS"
rm "$MISSING_CODEOWNERS/.github/CODEOWNERS"
expect_fail python3 scripts/shiki.py doctor --json --target "$MISSING_CODEOWNERS"
test "$(finding_status /tmp/shiki-doctor-expected-fail.out doctor.codeowners.coverage)" = "fail"

INVALID_RUNTIME="$TMP_ROOT/invalid-runtime"
make_target "$INVALID_RUNTIME"
python3 - "$INVALID_RUNTIME/.shiki/config.yaml" <<'PY'
import sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
text = text.replace("  implementer: claude-code", "  implementer: unknown-runtime")
open(path, "w", encoding="utf-8").write(text)
PY
expect_fail python3 scripts/shiki.py doctor --json --target "$INVALID_RUNTIME"
test "$(finding_status /tmp/shiki-doctor-expected-fail.out doctor.runtime.assignments)" = "fail"

MISSING_WORKFLOW="$TMP_ROOT/missing-workflow"
make_target "$MISSING_WORKFLOW"
rm "$MISSING_WORKFLOW/.github/workflows/shiki-validate.yml"
expect_fail python3 scripts/shiki.py doctor --json --target "$MISSING_WORKFLOW"
test "$(finding_status /tmp/shiki-doctor-expected-fail.out doctor.workflows.required_files)" = "fail"

mkdir -p "$FAKE_BIN"
cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "--version")
    echo "gh version 2.74.0"
    ;;
  "auth status")
    echo "github.com" >&2
    ;;
  "repo view example/shiki-doctor --json name,defaultBranchRef")
    echo '{"name":"shiki-doctor","defaultBranchRef":{"name":"main"}}'
    ;;
  "secret list --repo example/shiki-doctor")
    echo "CLAUDE_CODE_OAUTH_TOKEN 2026-06-04T00:00:00Z"
    ;;
  "api repos/example/shiki-doctor/branches/main/protection")
    cat <<'JSON'
{
  "required_status_checks": {
    "contexts": [
      "Validate Shiki mirror",
      "CCA verdict",
      "MergeGate metadata check",
      "MergeGate policy check"
    ]
  },
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "require_code_owner_reviews": true
  }
}
JSON
    ;;
  "api repos/example/shiki-doctor/actions/permissions/workflow")
    echo '{"default_workflow_permissions":"read","can_approve_pull_request_reviews":true}'
    ;;
  "api repos/example/shiki-doctor/issues/comments?per_page=1")
    echo '[]'
    ;;
  "api repos/example/shiki-doctor/issues/events?per_page=1")
    echo '[]'
    ;;
  *)
    echo "fake gh unsupported: $*" >&2
    exit 1
    ;;
esac
SH
chmod +x "$FAKE_BIN/gh"

SECRET_SENTINEL="super-secret-token-value" PATH="$FAKE_BIN:$PATH" \
  python3 scripts/shiki.py doctor --json --online --target "$VALID" >/tmp/shiki-doctor-online.json
test "$(finding_status /tmp/shiki-doctor-online.json doctor.github.repo_exists)" = "pass"
test "$(finding_status /tmp/shiki-doctor-online.json doctor.github.branch_protection)" = "pass"
test "$(finding_status /tmp/shiki-doctor-online.json doctor.github.workflow_permissions)" = "pass"
test "$(finding_status /tmp/shiki-doctor-online.json doctor.secrets.claude_code_oauth_token)" = "pass"
test "$(finding_status /tmp/shiki-doctor-online.json doctor.guardian.github_events)" = "pass"
if grep -q "super-secret-token-value" /tmp/shiki-doctor-online.json; then
  echo "doctor output leaked secret sentinel" >&2
  exit 1
fi

cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "--version")
    echo "gh version 2.74.0"
    ;;
  "auth status")
    echo "github.com" >&2
    ;;
  "repo view example/shiki-doctor --json name,defaultBranchRef")
    echo '{"name":"shiki-doctor","defaultBranchRef":{"name":"main"}}'
    ;;
  "secret list --repo example/shiki-doctor")
    echo "permission denied" >&2
    exit 1
    ;;
  "api repos/example/shiki-doctor/branches/main/protection")
    echo "permission denied" >&2
    exit 1
    ;;
  "api repos/example/shiki-doctor/actions/permissions/workflow")
    echo "permission denied" >&2
    exit 1
    ;;
  "api repos/example/shiki-doctor/issues/comments?per_page=1")
    echo "permission denied" >&2
    exit 1
    ;;
  "api repos/example/shiki-doctor/issues/events?per_page=1")
    echo "permission denied" >&2
    exit 1
    ;;
  *)
    echo "fake gh unsupported: $*" >&2
    exit 1
    ;;
esac
SH
chmod +x "$FAKE_BIN/gh"
PATH="$FAKE_BIN:$PATH" python3 scripts/shiki.py doctor --json --online --target "$VALID" >/tmp/shiki-doctor-online-permission.json
test "$(finding_status /tmp/shiki-doctor-online-permission.json doctor.github.branch_protection)" = "warn"
test "$(finding_status /tmp/shiki-doctor-online-permission.json doctor.github.workflow_permissions)" = "warn"
test "$(finding_status /tmp/shiki-doctor-online-permission.json doctor.secrets.claude_code_oauth_token)" = "warn"
test "$(finding_status /tmp/shiki-doctor-online-permission.json doctor.guardian.github_events)" = "warn"

python3 scripts/shiki.py doctor --target "$NO_REPO" >/tmp/shiki-doctor-human.out
grep "provider:" /tmp/shiki-doctor-human.out >/dev/null
grep "remediation:" /tmp/shiki-doctor-human.out >/dev/null

# An unregistered git worktree is a governance violation and must be flagged.
UNREGISTERED_WT="$TMP_ROOT/unregistered-worktree"
make_target "$UNREGISTERED_WT"
git -C "$UNREGISTERED_WT" add .
git -C "$UNREGISTERED_WT" -c user.name="Shiki Test" -c user.email="shiki@example.test" commit -m "init" >/tmp/shiki-doctor-wt-commit.out
git -C "$UNREGISTERED_WT" worktree add "$TMP_ROOT/stray-worktree" -b stray-branch >/tmp/shiki-doctor-wt-add.out 2>&1
expect_fail python3 scripts/shiki.py doctor --json --target "$UNREGISTERED_WT"
test "$(finding_status /tmp/shiki-doctor-expected-fail.out doctor.worktrees.unregistered)" = "fail"

echo "shiki doctor tests passed"
