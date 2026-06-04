#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/shiki-provider-test-$$"
FAKE_BIN="$TMP_ROOT/bin"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

expect_fail() {
  if "$@" >/tmp/shiki-provider-expected-fail.out 2>&1; then
    echo "expected failure but command succeeded: $*" >&2
    cat /tmp/shiki-provider-expected-fail.out >&2
    return 1
  fi
}

cd "$ROOT"

python3 - <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))
from shiki_provider import (
    ProviderConfigError,
    canonical_remote_url,
    github_env,
    provider_from_repo_json,
    provider_from_values,
    remote_matches_provider,
)

default = provider_from_values(repo="OWNER/REPO")
assert default.provider == "github"
assert default.host == "github.com"
assert default.protocol == "https"
assert default.web_base_url == "https://github.com"
assert default.api_base_url == "https://api.github.com"
assert canonical_remote_url(default) == "https://github.com/OWNER/REPO.git"
assert github_env(default) == {}

ssh_default = provider_from_values(repo="OWNER/REPO", protocol="ssh")
assert canonical_remote_url(ssh_default) == "git@github.com:OWNER/REPO.git"

enterprise = provider_from_values(repo="OWNER/REPO", host="github.example.com")
assert enterprise.api_base_url == "https://github.example.com/api/v3"
assert canonical_remote_url(enterprise) == "https://github.example.com/OWNER/REPO.git"
assert github_env(enterprise) == {"GH_HOST": "github.example.com"}

enterprise_ssh = provider_from_values(repo="OWNER/REPO", host="github.example.com", protocol="ssh")
assert canonical_remote_url(enterprise_ssh) == "git@github.example.com:OWNER/REPO.git"

explicit_api = provider_from_values(
    repo="OWNER/REPO",
    host="github.example.com",
    api_base_url="https://github.example.com/custom/api",
)
assert explicit_api.api_base_url == "https://github.example.com/custom/api"

for kwargs in (
    {"repo": "OWNER/REPO", "provider": "gitlab"},
    {"repo": "OWNER/REPO", "protocol": "git"},
    {"repo": "OWNER"},
):
    try:
        provider_from_values(**kwargs)
    except ProviderConfigError:
        pass
    else:
        raise SystemExit(f"invalid provider config accepted: {kwargs}")

assert remote_matches_provider("https://github.com/OWNER/REPO.git", default)
assert remote_matches_provider("https://github.com/OWNER/REPO", default)
assert remote_matches_provider("git@github.com:OWNER/REPO.git", default)
assert remote_matches_provider("git@github.example.com:OWNER/REPO.git", enterprise)
assert remote_matches_provider("https://github.example.com/OWNER/REPO.git", enterprise)
assert not remote_matches_provider("https://github.com/OTHER/REPO.git", default)
assert not remote_matches_provider("https://gitlab.com/OWNER/REPO.git", default)

legacy = provider_from_repo_json({"source_of_truth": "github", "repo": "OWNER/REPO", "default_branch": "main", "mirror": ".shiki"})
assert legacy.host == "github.com"
assert legacy.protocol == "https"
assert legacy.api_base_url == "https://api.github.com"

print("provider config basics passed")
PY

mkdir -p "$FAKE_BIN"
cat >"$FAKE_BIN/gh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo "GH_HOST=${GH_HOST:-} $*" >>"${SHIKI_FAKE_GH_LOG}"
case "$1 $2" in
  "auth status")
    exit 0
    ;;
  "repo view")
    exit 0
    ;;
  "repo create")
    echo "https://${GH_HOST:-github.com}/example/shiki-provider-test"
    exit 0
    ;;
esac
echo "fake gh unsupported: $*" >&2
exit 1
SH
chmod +x "$FAKE_BIN/gh"
export PATH="$FAKE_BIN:$PATH"
export SHIKI_FAKE_GH_LOG="$TMP_ROOT/gh.log"

DRY_RUN="$TMP_ROOT/dry-run"
python3 scripts/shiki.py init "$DRY_RUN" \
  --repo example/shiki-provider-test \
  --github-host github.example.com \
  --remote-protocol ssh \
  --github-api-url https://github.example.com/api/v3 >/tmp/shiki-provider-dry-run.out
grep "provider: github" /tmp/shiki-provider-dry-run.out >/dev/null
grep "github-host: github.example.com" /tmp/shiki-provider-dry-run.out >/dev/null
grep "remote-protocol: ssh" /tmp/shiki-provider-dry-run.out >/dev/null
grep "git: configure origin git@github.example.com:example/shiki-provider-test.git" /tmp/shiki-provider-dry-run.out >/dev/null
grep "github-api: use https://github.example.com/api/v3" /tmp/shiki-provider-dry-run.out >/dev/null
test ! -e "$DRY_RUN"

EXECUTED="$TMP_ROOT/executed"
python3 scripts/shiki.py init "$EXECUTED" \
  --repo example/shiki-provider-test \
  --github-host github.example.com \
  --remote-protocol ssh \
  --execute \
  --no-commit \
  --no-push \
  --no-set-secret \
  --no-protect >/tmp/shiki-provider-executed.out
test "$(git -C "$EXECUTED" remote get-url origin)" = "git@github.example.com:example/shiki-provider-test.git"
grep "GH_HOST=github.example.com auth status" "$SHIKI_FAKE_GH_LOG" >/dev/null
grep "GH_HOST=github.example.com repo view example/shiki-provider-test --json name" "$SHIKI_FAKE_GH_LOG" >/dev/null

python3 - "$EXECUTED/.shiki/repo.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
expected = {
    "provider": "github",
    "repo": "example/shiki-provider-test",
    "host": "github.example.com",
    "remote_protocol": "ssh",
    "web_base_url": "https://github.example.com",
    "api_base_url": "https://github.example.com/api/v3",
    "ssh_host": "github.example.com",
    "canonical_remote_url": "git@github.example.com:example/shiki-provider-test.git",
}
for key, value in expected.items():
    if data.get(key) != value:
        raise SystemExit(f"{key} expected {value!r}, got {data.get(key)!r}")
PY

MISMATCH="$TMP_ROOT/mismatch"
mkdir -p "$MISMATCH"
git -C "$MISMATCH" init -b main >/tmp/shiki-provider-mismatch-git.out
git -C "$MISMATCH" remote add origin https://github.example.com/example/other.git
expect_fail python3 scripts/shiki.py init "$MISMATCH" \
  --repo example/shiki-provider-test \
  --github-host github.example.com \
  --remote-protocol ssh
grep "origin already points" /tmp/shiki-provider-expected-fail.out >/dev/null

MATCH="$TMP_ROOT/match"
mkdir -p "$MATCH"
git -C "$MATCH" init -b main >/tmp/shiki-provider-match-git.out
git -C "$MATCH" remote add origin https://github.example.com/example/shiki-provider-test
python3 scripts/shiki.py init "$MATCH" \
  --repo example/shiki-provider-test \
  --github-host github.example.com \
  --remote-protocol ssh >/tmp/shiki-provider-match.out
grep "dry-run: no bootstrap/init mutations were executed" /tmp/shiki-provider-match.out >/dev/null

python3 scripts/shiki.py init --help >/tmp/shiki-provider-init-help.out
grep -- "--provider" /tmp/shiki-provider-init-help.out >/dev/null
grep -- "--github-host" /tmp/shiki-provider-init-help.out >/dev/null
grep -- "--remote-protocol" /tmp/shiki-provider-init-help.out >/dev/null
grep -- "--github-api-url" /tmp/shiki-provider-init-help.out >/dev/null

python3 scripts/shiki.py start --help >/tmp/shiki-provider-start-help.out
grep -- "--github-host" /tmp/shiki-provider-start-help.out >/dev/null

python3 scripts/shiki.py bootstrap-platform --help >/tmp/shiki-provider-bootstrap-help.out
grep -- "--remote-protocol" /tmp/shiki-provider-bootstrap-help.out >/dev/null

echo "shiki provider config tests passed"
