#!/usr/bin/env python3
"""GitHub CLI/API helpers for Shiki repository, secret, review, and PR evidence operations."""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from shiki_git import github_origin
from shiki_provider import ProviderConfig, ProviderConfigError, canonicalize_remote_url, default_provider_config, github_env, provider_from_repo_json, repo_api_path, validate_repo_slug
from shiki_process import ShikiError, first_line, info, load_default_config, print_json, read_json, require_tool, run, warn, write_json, shiki_path, target_path
from shiki_tasks import append_ledger, load_task, require_github_first_target, worktree_record

GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

def require_github_repo_slug(repo: str) -> None:
    try:
        validate_repo_slug(repo)
    except ValueError as error:
        raise ShikiError("repo must be a GitHub slug like OWNER/NAME") from error


def github_repo_exists(repo: str, provider_config: ProviderConfig | None = None) -> bool:
    config = provider_config or default_provider_config(repo)
    return run(["gh", "repo", "view", config.repo, "--json", "name"], env=github_env(config), check=False).returncode == 0


def ensure_github_repo(repo: str, visibility: str, provider_config: ProviderConfig | None = None) -> None:
    config = provider_config or default_provider_config(repo)
    if github_repo_exists(repo, provider_config=config):
        info(f"GitHub repository already exists: {config.repo}")
        return
    args = ["gh", "repo", "create", config.repo]
    args.append(f"--{visibility}")
    args.extend(["--confirm"])
    run(args, env=github_env(config))
    info(f"created GitHub repository: {config.repo}")


def set_default_branch(repo: str, branch: str, provider_config: ProviderConfig | None = None) -> None:
    config = provider_config or default_provider_config(repo)
    result = run(
        ["gh", "api", repo_api_path(config), "-X", "PATCH", "-f", f"default_branch={branch}"],
        env=github_env(config),
        check=False,
    )
    if result.returncode == 0:
        info(f"set default branch to {branch}")
    else:
        warn(f"could not set default branch: {result.stderr.strip()}")


def set_secret(repo: str, secret_name: str, value: str, provider_config: ProviderConfig | None = None) -> None:
    config = provider_config or default_provider_config(repo)
    run(["gh", "secret", "set", secret_name, "--repo", config.repo], input_text=value, env=github_env(config))
    info(f"set GitHub secret: {secret_name}")


def claude_secret_remediation(repo: str, secret_env: str) -> str:
    return (
        f"Run `shiki secret set-claude --repo {repo}` to mint (via `claude setup-token`), "
        f"verify, and set the secret in one step. Alternatively create the token with "
        f"`claude setup-token`, export it as {secret_env}, then run "
        f"`gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo {repo}` or rerun Shiki init/start."
    )


def configure_claude_code_secret(
    repo: str,
    *,
    enabled: bool,
    secret_env: str,
    provider_config: ProviderConfig | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "name": "CLAUDE_CODE_OAUTH_TOKEN",
        "enabled": enabled,
        "configured": False,
        "source": None,
        "remediation": "",
    }
    if not enabled:
        status["remediation"] = "Secret setup was disabled with --no-set-secret."
        return status

    secret_value = os.environ.get(secret_env, "")
    if not secret_value:
        status["remediation"] = claude_secret_remediation(repo, secret_env)
        raise ShikiError(
            f"missing required GitHub Actions secret source: {secret_env}. "
            "Claude Code login does not automatically expose a GitHub Actions token to Shiki. "
            f"{status['remediation']}"
        )

    set_secret(repo, "CLAUDE_CODE_OAUTH_TOKEN", secret_value, provider_config=provider_config)
    status["configured"] = True
    status["source"] = f"env:{secret_env}"
    return status


def github_secret_status(repo: str, secret_name: str, provider_config: ProviderConfig | None = None) -> dict[str, Any]:
    config = provider_config or default_provider_config(repo)
    result = run(["gh", "secret", "list", "--repo", config.repo], env=github_env(config), check=False)
    if result.returncode != 0:
        return {
            "name": secret_name,
            "checked": False,
            "configured": None,
            "error": first_line(result.stderr) or first_line(result.stdout),
        }
    names = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    return {
        "name": secret_name,
        "checked": True,
        "configured": secret_name in names,
    }


CLAUDE_OAUTH_TOKEN_RE = re.compile(r"sk-ant-oat[0-9]{2}-[A-Za-z0-9_-]+")


def _redact_token(text: str) -> str:
    """Redact any Claude OAuth token before the text is surfaced to logs/errors.

    Probe stderr / result text is never expected to echo the credential, but a
    secret-handling command redacts as defense-in-depth so no token can leak
    through an error message.
    """
    return CLAUDE_OAUTH_TOKEN_RE.sub("[REDACTED]", str(text or ""))


def _redact_secret(text: str, secret: str) -> str:
    """Redact OAuth-shaped tokens AND the exact candidate secret from surfaced text.

    ``_redact_token`` only matches the ``sk-ant-oat`` shape, so a non-OAuth /
    malformed candidate that the probe echoes back in a failure reason would
    otherwise survive into a verification-failure error. Also replacing the exact
    candidate value redacts it regardless of shape.
    """
    redacted = _redact_token(text)
    secret = (secret or "").strip()
    if secret:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def extract_setup_token_value(text: str) -> str | None:
    """Extract a long-lived Claude Code OAuth token from `claude setup-token` output.

    Scans for the ``sk-ant-oat`` token pattern and returns the longest match so a
    token surrounded by prompt/UI text is still recovered. Returns ``None`` when
    no token is present (e.g. the authorization was cancelled).
    """
    matches = CLAUDE_OAUTH_TOKEN_RE.findall(text or "")
    if not matches:
        return None
    return max(matches, key=len)


def looks_like_claude_oauth_token(token: str) -> bool:
    return bool(CLAUDE_OAUTH_TOKEN_RE.fullmatch((token or "").strip()))


# Anthropic/Claude env vars that can authenticate or reroute a `claude` call
# independently of CLAUDE_CODE_OAUTH_TOKEN. The verification probe blanks every
# one so the candidate OAuth token is the *only* credential that can authenticate
# it. shiki_process.run merges the probe env OVER the inherited os.environ, so an
# unblanked ambient ANTHROPIC_AUTH_TOKEN / API key / Bedrock-Vertex-Foundry route
# would pass through and let a bad token verify clean — the false positive that
# hides a CCA `401 Invalid bearer token`. Blanking (not unsetting) suffices: each
# is falsy/off when empty, and the merge model can only override keys, not delete
# them.
_PROBE_BLANKED_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
)

# Managed/enterprise Claude settings load at HIGHEST precedence and cannot be
# excluded by `--setting-sources` or by HOME/CLAUDE_CONFIG_DIR isolation. If such
# a source supplies an Anthropic credential, the probe can authenticate regardless
# of the candidate token — a false positive that would let a bad token verify
# clean.
#
# These are the documented FILE-based managed-settings directories. The list is a
# best-effort fast-fail (so a managed host fails before the operator authorizes a
# token) and is NOT the completeness guarantee: managed settings also come from
# Windows registry policy (HKLM/HKCU) and macOS managed preferences / MDM, which
# are not files and cannot be enumerated here. The authoritative, source-agnostic
# guarantee is the negative-control probe in ``verify_claude_oauth_token``.
_MANAGED_SETTINGS_DIRS = (
    "/Library/Application Support/ClaudeCode",  # macOS
    "/etc/claude-code",  # Linux
)


def managed_claude_settings_paths() -> list[str]:
    """Return FILE-based managed/enterprise Claude settings present on this host.

    Best-effort fast-fail covering the documented file locations: the macOS and
    Linux directories plus the Windows ``%PROGRAMDATA%`` and ``%PROGRAMFILES%``
    ``ClaudeCode`` directories, including each one's ``managed-settings.json`` and
    any ``managed-settings.d/*.json`` drop-ins. This is intentionally NOT complete
    (registry policy and macOS MDM-managed preferences are not files); the
    token-exclusivity guarantee is the negative-control probe. A non-empty list
    just lets the command fail before minting on a clearly managed host.
    """
    dirs = list(_MANAGED_SETTINGS_DIRS)
    for env_var in ("PROGRAMDATA", "PROGRAMFILES"):
        base = os.environ.get(env_var)
        if base:
            dirs.append(os.path.join(base, "ClaudeCode"))
    found: list[str] = []
    for directory in dirs:
        main = os.path.join(directory, "managed-settings.json")
        if os.path.exists(main):
            found.append(main)
        dropins = os.path.join(directory, "managed-settings.d")
        if os.path.isdir(dropins):
            found.extend(sorted(glob.glob(os.path.join(dropins, "*.json"))))
    return found


def claude_supports_setting_sources(*, runner=run) -> bool:
    """Whether the installed ``claude`` CLI accepts ``--setting-sources``.

    ``claude --help`` lists the flag as "Comma-separated list of setting sources
    to load (user, project, local)"; passing ``--setting-sources user`` lets the
    probe drop *project/local* settings explicitly. It is defense-in-depth, not the
    only barrier: the probe also runs from an isolated empty working directory, so
    when the flag is absent a project/local ``.claude`` is still undiscoverable and
    the probe stays fail-closed. Detection lets an older CLI fall back to the clean
    working directory instead of erroring on an unknown flag.
    """
    result = runner(["claude", "--help"], check=False)
    return "--setting-sources" in ((result.stdout or "") + "\n" + (result.stderr or ""))


def token_probe_invocation(
    token: str, config_dir: str, *, setting_sources: bool = True
) -> tuple[list[str], dict[str, str], str]:
    """Build the isolated probe command, env, and working directory for an OAuth token.

    Three layers keep the candidate ``token`` the *sole* credential under test, so
    an invalid/expired token fails closed instead of silently passing against the
    operator's environment:

    1. ``CLAUDE_CONFIG_DIR``/``HOME`` point at an isolated temp dir, so no on-disk
       *user* login/keychain or ``~/.claude/settings.json`` (``apiKeyHelper`` etc.)
       can mask the supplied token.
    2. Every entry in ``_PROBE_BLANKED_CREDENTIAL_ENV`` is blanked, so no ambient
       higher-precedence credential or cloud-provider route authenticates the probe.
    3. The probe runs from ``config_dir`` — a clean temp dir with no *project*
       ``.claude`` — and, when ``setting_sources`` is set (the CLI supports the
       flag), passes ``--setting-sources user`` so a repo-local
       ``.claude/settings.json`` / ``.claude/settings.local.json`` (project-supplied
       ``env`` creds or ``apiKeyHelper``) is neither discovered nor loaded. Without
       a clean cwd the probe would run in the repo root (``run`` defaults
       ``cwd=ROOT``), whose project settings could authenticate a bad token — the
       false positive that hides a CCA ``401``.

    Limitation: ``--setting-sources`` cannot exclude *managed/enterprise* settings
    (macOS ``/Library/Application Support/ClaudeCode/managed-settings.json``, Linux
    ``/etc/claude-code/managed-settings.json``); those always load at highest
    precedence and cannot be suppressed from userspace. On a host with managed
    Anthropic credentials the probe may pass regardless of the candidate token, so
    the probe alone is not token-exclusive there. ``cmd_secret_set_claude`` closes
    this gap by refusing to set the secret when ``managed_claude_settings_paths()``
    finds any such file; on every other host the probe is token-exclusive.
    """
    env = {name: "" for name in _PROBE_BLANKED_CREDENTIAL_ENV}
    env.update({
        "CLAUDE_CONFIG_DIR": config_dir,
        "HOME": config_dir,
        "CLAUDE_CODE_OAUTH_TOKEN": token,
    })
    argv = ["claude", "-p", "ping", "--output-format", "json"]
    if setting_sources:
        argv += ["--setting-sources", "user"]
    return argv, env, config_dir


# Probe outcome classes. The negative control must distinguish a genuine auth
# REJECTION (the only POSITIVE proof the token reached and was refused by the auth
# layer) from an INDETERMINATE failure (network/CLI/non-JSON/rate-limit), where the
# auth verdict is unknown. Inferring token-exclusivity from "the bad token did not
# authenticate" is unsafe when the non-authentication was indeterminate.
PROBE_AUTHENTICATED = "authenticated"
PROBE_AUTH_REJECTED = "auth_rejected"
PROBE_INDETERMINATE = "indeterminate"

# Substrings (lowercased) that mark a genuine authentication rejection. This is
# the FALLBACK path for CLIs that report no ``api_error_status`` (see
# ``_auth_rejection_status``): the CLI's prose is not a stable contract, so
# matching it is best-effort. Network / overload / rate-limit / generic CLI
# errors are deliberately absent so they fall through to INDETERMINATE, as are
# bare nouns like "oauth access token" that a NON-auth failure could also
# mention — a marker must be a rejection PREDICATE, never a topic.
_AUTH_REJECTION_MARKERS = (
    "invalid bearer",  # "401 Invalid bearer token" — wording of the pre-2026 CLI
    "invalid_bearer",
    "invalid x-api-key",
    "invalid api key",
    "invalid_api_key",
    "invalid token",
    "invalid_token",
    "token is invalid",  # current CLI: "401 OAuth access token is invalid."
    "authentication_error",  # Anthropic's structured auth-error TYPE (distinct from "authentication service ...")
    "unauthorized",
    "not logged in",
    "run /login",
    "token has expired",
    "expired token",
)

# Match markers only as whole tokens, never inside a larger word/identifier, so a
# service failure like ``unauthorized_error: rate_limit_exceeded`` is NOT read as
# an auth rejection (the ``_`` keeps ``unauthorized`` part of a larger token). The
# boundary is "not adjacent to a word char or hyphen".
_AUTH_REJECTION_RE = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(marker) for marker in _AUTH_REJECTION_MARKERS) + r")(?![\w-])"
)


# HTTP statuses that prove the auth layer adjudicated — and refused — the
# credential. Taken from the probe payload's structured ``api_error_status``,
# which, unlike the human-readable ``result`` prose, is not CLI wording that
# drifts between releases. Rate-limit / overload / server statuses are absent so
# they stay INDETERMINATE.
_AUTH_REJECTION_STATUSES = frozenset({401, 403})


def _auth_rejection_status(result: dict[str, Any]) -> int | None:
    """Return ``api_error_status`` when it proves an authentication rejection.

    Accepts the int or digit-string form. Anything else — absent, boolean,
    non-numeric, or a non-auth status such as ``429``/``500`` — returns ``None``
    so the caller falls through to the wording fallback and then to
    INDETERMINATE, preserving the fail-closed posture.
    """
    status = result.get("api_error_status")
    if isinstance(status, bool):
        return None
    if isinstance(status, str):
        status = status.strip()
        # `isascii()` is load-bearing: `str.isdigit()` alone is True for non-ASCII
        # digits, where `int()` either raises (²) or silently succeeds (４０１, ٤٠١).
        # An HTTP status is ASCII, and this path must never raise — an
        # unparseable status is INDETERMINATE, not a crash.
        if not (status.isascii() and status.isdigit()):
            return None
        status = int(status)
    if isinstance(status, int) and status in _AUTH_REJECTION_STATUSES:
        return status
    return None


def _classify_probe_result(result: Any) -> tuple[str, str]:
    """Classify a ``claude -p --output-format json`` probe result.

    Returns ``(classification, redacted_detail)``:

    - ``PROBE_AUTHENTICATED``: ``is_error=false`` with a billable response
      (``total_cost_usd > 0``).
    - ``PROBE_AUTH_REJECTED``: a genuine authentication rejection (e.g. ``401
      OAuth access token is invalid`` / unauthorized / not logged in) — the only
      positive proof the token was adjudicated by the auth layer. Proven by the
      structured ``api_error_status`` where the CLI reports one, and only
      otherwise by the ``_AUTH_REJECTION_MARKERS`` wording fallback.
    - ``PROBE_INDETERMINATE``: anything else (non-dict, network/overload/rate-limit,
      CLI error). The auth verdict is unknown, so callers must fail closed rather
      than infer token-exclusivity from a non-auth failure.
    """
    if not isinstance(result, dict):
        return PROBE_INDETERMINATE, "probe returned no structured result"
    detail = _redact_token(str(result.get("result") or "").strip())
    status = _auth_rejection_status(result)
    if status is not None:
        # Checked BEFORE the success branch: a payload carrying an auth status is
        # never an authenticated probe, whatever ``is_error``/``subtype`` claim.
        # The CLI reports ``subtype: "success"`` even on this failure, so neither
        # field is load-bearing here.
        return PROBE_AUTH_REJECTED, detail or f"authentication rejected (HTTP {status})"
    cost = result.get("total_cost_usd")
    if not result.get("is_error") and isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
        return PROBE_AUTHENTICATED, detail or "authenticated"
    if _AUTH_REJECTION_RE.search(detail.lower()):
        return PROBE_AUTH_REJECTED, detail or "authentication rejected"
    return PROBE_INDETERMINATE, detail or "indeterminate probe failure"


def interpret_token_probe(result: dict[str, Any]) -> tuple[bool, str]:
    """Whether a probe result proves the token valid (``is_error=false``, cost>0).

    Thin wrapper over ``_classify_probe_result``: only ``PROBE_AUTHENTICATED`` is a
    pass; every rejection/indeterminate outcome fails closed.
    """
    classification, detail = _classify_probe_result(result)
    if classification == PROBE_AUTHENTICATED:
        return True, "token authenticated"
    return False, detail


def _capture_setup_token() -> str:
    """Run interactive ``claude setup-token``, returning its captured stdout.

    ``stderr`` is inherited so the operator sees the authorization URL/prompts in
    real time, while ``stdout`` is captured to recover the printed token.
    """
    require_tool("claude")
    info("Running `claude setup-token` — complete the browser authorization when prompted.")
    proc = subprocess.run(["claude", "setup-token"], stdout=subprocess.PIPE, stderr=None, text=True)
    return proc.stdout or ""


def mint_claude_oauth_token(*, capture=_capture_setup_token) -> str:
    """Mint a long-lived Claude Code OAuth token via ``claude setup-token``."""
    token = extract_setup_token_value(capture())
    if not token:
        raise ShikiError(
            "could not read a token from `claude setup-token` output. "
            "Run it yourself and pipe it instead: "
            "`claude setup-token | shiki secret set-claude --repo OWNER/NAME --token-stdin`."
        )
    return token


# A deliberately-invalid OAuth-shaped token used as a negative control. It can
# never authenticate against the Anthropic API, so if a probe with THIS token
# succeeds, some credential other than the candidate (managed/MDM/registry/
# ambient settings) is authenticating the environment.
_NEGATIVE_CONTROL_TOKEN = "sk-ant-oat01-shiki-negative-control-deliberately-invalid-000000000000"


def _probe_token(token: str, *, runner, setting_sources: bool) -> tuple[str, str]:
    """Run one isolated probe for ``token``; return ``(classification, reason)``.

    Each probe gets its own fresh temp config dir so a prior probe's session
    cache cannot authenticate a later one. Non-JSON output is INDETERMINATE (a
    CLI/communication failure with no auth verdict). The exact candidate (any
    shape) and OAuth-shaped tokens are redacted from the surfaced reason.
    """
    config_dir = tempfile.mkdtemp(prefix="shiki-token-probe-")
    try:
        argv, env, cwd = token_probe_invocation(token, config_dir, setting_sources=setting_sources)
        result = runner(argv, env=env, cwd=cwd, input_text="", check=False)
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return PROBE_INDETERMINATE, _redact_secret(first_line(result.stderr), token) or "probe output was not JSON"
        classification, detail = _classify_probe_result(data)
        return classification, _redact_secret(detail, token)
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


def verify_claude_oauth_token(token: str, *, runner=run) -> tuple[bool, str]:
    """Verify a Claude OAuth token by an isolated, token-exclusive probe (fails closed).

    A candidate that is not authenticated rejects immediately. A candidate PASS is
    trusted ONLY when a **negative-control** probe — a deliberately-invalid token
    in the same isolated environment — comes back with a clean authentication
    REJECTION (the positive proof the auth layer adjudicated the token):

    - negative control authenticates  -> an independent credential (managed / MDM /
      registry / ambient) is in play; not token-exclusive -> fail closed.
    - negative control indeterminate   -> the auth verdict is unknown (network /
      CLI / non-JSON); token-exclusivity is NOT proven -> fail closed.
    - negative control auth-rejected   -> only the candidate authenticates ->
      trust the candidate PASS.

    This is source-agnostic: it catches any independent-credential source without
    enumerating them, and never infers token-exclusivity from an indeterminate
    failure.
    """
    require_tool("claude")
    setting_sources = claude_supports_setting_sources(runner=runner)
    candidate_class, reason = _probe_token(token, runner=runner, setting_sources=setting_sources)
    if candidate_class != PROBE_AUTHENTICATED:
        return False, reason
    negative_class, _ = _probe_token(_NEGATIVE_CONTROL_TOKEN, runner=runner, setting_sources=setting_sources)
    if negative_class == PROBE_AUTHENTICATED:
        return False, (
            "verification environment is not token-exclusive: a deliberately-invalid token also "
            "authenticated, so a managed/MDM/registry/ambient credential is authenticating "
            "independently of the candidate. The probe cannot prove the candidate token is valid here."
        )
    if negative_class != PROBE_AUTH_REJECTED:
        return False, (
            "could not prove token-exclusive verification: the negative-control probe did not return a "
            "clean authentication rejection (it failed for an indeterminate reason such as a network or "
            "CLI error), so the candidate token's pass cannot be trusted. Retry, or set the secret with "
            "`gh secret set` after confirming the token out of band."
        )
    return True, reason


def cmd_secret_set_claude(args: Any) -> int:
    """Mint/accept, verify, and cleanly set the CLAUDE_CODE_OAUTH_TOKEN secret.

    The unavoidable interactive step is the browser authorization in
    `claude setup-token`; obtaining, verifying (so a corrupt/expired token is
    caught before it reaches CI), and setting the secret are automated. The
    secret value is piped to `gh secret set` verbatim, so no trailing newline or
    paste artifact can corrupt it (the failure mode behind a silent CCA 401).
    """
    require_tool("gh")
    repo = args.repo or load_default_config().get("repo")
    if not repo:
        raise ShikiError("missing --repo OWNER/NAME and no default repo configured")
    require_github_repo_slug(repo)

    # Fail closed before minting: managed/enterprise settings load at highest
    # precedence and cannot be excluded by the probe, so a managed Anthropic
    # credential could authenticate it regardless of the candidate token. We
    # cannot guarantee token-exclusive verification there, so we refuse to set
    # the secret rather than risk silently writing an unverified token.
    managed = managed_claude_settings_paths()
    if managed:
        raise ShikiError(
            "managed Claude settings are present ("
            + ", ".join(managed)
            + "); they load at highest precedence and cannot be excluded by the verification probe, "
            "so a managed Anthropic credential could authenticate it independently of the candidate "
            "token. Token-exclusive verification cannot be guaranteed on this host, so the secret was "
            "NOT set. Confirm the token out of band, then set it with "
            f"`gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo {repo}` directly."
        )

    if args.token_stdin:
        token = sys.stdin.read()
        source = "stdin"
    elif args.from_env:
        token = os.environ.get(args.from_env, "")
        source = f"env:{args.from_env}"
    else:
        token = mint_claude_oauth_token()
        source = "claude setup-token"
    token = (token or "").strip()
    if not token:
        raise ShikiError(f"no token obtained from {source}")
    if not looks_like_claude_oauth_token(token):
        warn(f"token from {source} does not look like a Claude OAuth token (expected sk-ant-oat...)")

    # Verification is mandatory and fails closed: this command exists to stop a
    # corrupt/expired token reaching CI (the silent CCA 401). To set a secret
    # without this probe, use `gh secret set` directly.
    ok, reason = verify_claude_oauth_token(token)
    if not ok:
        raise ShikiError(
            f"token verification failed ({reason}); not setting the secret. "
            "Re-mint a fresh token with `claude setup-token`."
        )
    info(f"token verified: {reason}")

    # Defense-in-depth: the token is piped via stdin, never an argv, so it should
    # not surface in a set_secret failure — but if `gh` echoes its input on error,
    # redact the verified token (and any OAuth-shaped text) before re-raising so it
    # cannot leak into stderr/logs. `from None` drops the cause chain that still
    # carries the raw message.
    try:
        set_secret(repo, "CLAUDE_CODE_OAUTH_TOKEN", token, provider_config=None)
    except ShikiError as error:
        safe = _redact_token(str(error).replace(token, "[REDACTED]"))
        raise ShikiError(f"failed to set secret after verification: {safe}") from None
    print_json({
        "secret": "CLAUDE_CODE_OAUTH_TOKEN",
        "repo": repo,
        "source": source,
        "verified": True,
    })
    return 0


def protect_branch(
    repo: str,
    branch: str,
    required_checks: list[str],
    *,
    review_count: int,
    require_code_owner_review: bool = False,
    provider_config: ProviderConfig | None = None,
) -> None:
    config = provider_config or default_provider_config(repo)
    payload = {
        "required_status_checks": {
            "strict": True,
            "contexts": required_checks,
        },
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            # NOT `review_count > 0` (SADR-0021): the Review Bridge approves as a
            # bot, and a bot can never be a CODEOWNER, so coupling these two made a
            # solo-maintainer repository unable to merge any CODEOWNERS-touching PR.
            "require_code_owner_reviews": require_code_owner_review,
            "required_approving_review_count": review_count,
        },
        "restrictions": None,
        "required_conversation_resolution": True,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }
    result = run(
        [
            "gh",
            "api",
            repo_api_path(config, f"branches/{branch}/protection"),
            "-X",
            "PUT",
            "--input",
            "-",
        ],
        input_text=json.dumps(payload),
        env=github_env(config),
        check=False,
    )
    if result.returncode == 0:
        info(f"configured branch protection for {branch}")
    else:
        raise ShikiError(
            f"could not configure branch protection: {result.stderr.strip()}. "
            "Branch protection is required; rerun with --no-protect only for an explicit non-protected setup."
        )


def configure_workflow_permissions(
    repo: str,
    *,
    can_approve_pull_requests: bool = True,
    default_permissions: str = "read",
    provider_config: ProviderConfig | None = None,
) -> None:
    """Configure repository Actions workflow permissions for the CCA Review Bridge.

    Sets the default workflow token permission (``read``) and whether GitHub
    Actions may create and approve pull request reviews. The Review Bridge needs
    ``can_approve_pull_request_reviews=true`` to satisfy ``required_review: true``
    in solo operation after CCA returns ``complete`` (see SADR-0013 and
    ``docs/agents/decision-control.md``).

    Mirrors ``protect_branch``'s ``gh api ... -X PUT --input -`` pattern but
    warns instead of raising on failure: branch protection is the hard gate, and
    this default can also be set in repository Settings -> Actions -> General, so
    a missing Actions-admin scope must not abort an otherwise-complete bootstrap.
    """
    config = provider_config or default_provider_config(repo)
    payload = {
        "default_workflow_permissions": default_permissions,
        "can_approve_pull_request_reviews": can_approve_pull_requests,
    }
    result = run(
        [
            "gh",
            "api",
            repo_api_path(config, "actions/permissions/workflow"),
            "-X",
            "PUT",
            "--input",
            "-",
        ],
        input_text=json.dumps(payload),
        env=github_env(config),
        check=False,
    )
    if result.returncode == 0:
        info(
            "configured workflow permissions: "
            f"default={default_permissions}, can approve pull requests={can_approve_pull_requests}"
        )
    else:
        warn(
            f"could not configure workflow permissions: {result.stderr.strip()}. "
            "The CCA Review Bridge needs GitHub Actions allowed to create and approve "
            "pull requests; set default workflow permissions to read and enable "
            '"Allow GitHub Actions to create and approve pull requests" under '
            "repository Settings -> Actions -> General, or rerun Shiki init/start."
        )


def github_repo_from_origin(target: Path) -> str | None:
    origin = github_origin(target)
    if not origin:
        return None
    try:
        canonical = canonicalize_remote_url(origin)
    except ProviderConfigError:
        return None
    return "/".join(canonical.removeprefix("https://").split("/", 1)[1:])


def parse_github_number(value: str, kind: str) -> int:
    pattern = rf"/{kind}/([0-9]+)"
    match = re.search(pattern, value)
    if not match:
        raise ShikiError(f"could not parse GitHub {kind} number from: {value}")
    return int(match.group(1))


def target_provider_config(target: Path) -> ProviderConfig | None:
    repo_config = target / ".shiki" / "repo.json"
    if not repo_config.exists():
        return None
    try:
        return provider_from_repo_json(read_json(repo_config))
    except (ProviderConfigError, ShikiError) as error:
        raise ShikiError(f"{repo_config}: invalid provider config: {error}") from error


def github_issue_body(task: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"## Shiki",
            f"Goal: {task['goal_id']}",
            f"Task: {task['id']}",
            "",
            "## Scope",
            task["scope"],
            "",
            "## Acceptance",
            *[f"- {check}" for check in task.get("acceptance_checks", [])],
            "",
            "## Locks",
            *[f"- {lock}" for lock in task.get("locks", [])],
            "",
            "## Runtime",
            str(task.get("assigned_runtime", "codex")),
        ]
    )


def pre_pr_code_review_section(task: dict[str, Any]) -> list[str]:
    """The PR-12 ``## Pre-PR code review`` body section (SADR-0011).

    Rendered from the loop-recorded ``pre_pr_code_review`` block — the verdict of
    the independent read-only reviewer the loop ran before opening the PR. A PR is
    only ever opened on a ``clean`` verdict (a blocking/failed review fail-closes
    the loop before create_pr), so this section documents that the independent
    gate passed and links its ledger; it is never the implementer self-attesting.
    """
    review = task.get("pre_pr_code_review") or {}
    lines = ["## Pre-PR code review"]
    if not review:
        # Defensive: the section is always present for CCA PR-12, even when the
        # verdict block was not recorded (e.g. a manually opened PR).
        lines.append("- No independent pre-PR review verdict recorded.")
        return lines
    verdict = str(review.get("verdict", "unknown"))
    lines.append(f"- Verdict: {verdict}")
    lines.append("- Independent read-only reviewer (claude -p, read tools only) — SADR-0011")
    ledger_id = review.get("ledger_id")
    if ledger_id:
        lines.append(f"- Ledger: {ledger_id}")
    findings = review.get("findings") or []
    if findings:
        for finding in findings:
            title = str(finding.get("title", "finding")) if isinstance(finding, dict) else str(finding)
            lines.append(f"- Finding: {title}")
    else:
        lines.append("- Findings: none")
    return lines
def _task_test_command_for_body(task: dict[str, Any]) -> str:
    """The structured test command for the PR body's loop-observed TDD line.

    Mirrors the loop's ``task_test_command`` selection (the task's
    ``test_command`` or the safe unittest-discover default) so the PR records
    exactly what the loop exec'd. ``acceptance_checks`` is free-form prose and is
    never exec'd, so it is never shown here as the command.
    """
    from shiki_tasks import DEFAULT_TEST_COMMAND

    command = task.get("test_command")
    if isinstance(command, str) and command.strip():
        return command
    return DEFAULT_TEST_COMMAND


def github_pr_body(task: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"## Shiki",
            f"Goal: {task['goal_id']}",
            f"Task: {task['id']}",
            "CCA checklist profile: PR, TDD, V, CCA",
            "",
            "## Scope",
            task["scope"],
            "",
            "## Non-goals",
            *[f"- {item}" for item in task.get("non_goals", [])],
            "",
            "## Acceptance",
            *[f"- {check}" for check in task.get("acceptance_checks", [])],
            "",
            *pre_pr_code_review_section(task),
            "## TDD evidence (loop-observed)",
            "- The goal loop ran the task's tests in the worktree and recorded a "
            "type:check ledger (skill tdd, EXEC evidence) before opening this PR "
            "(SADR-0011); a red run blocks the PR.",
            f"- Test command: {_task_test_command_for_body(task)}",
            "",
            "## Evidence",
            "- python3 scripts/validate_shiki.py",
            "",
            "## Ledger evidence",
            *[f"- {entry}" for entry in task.get("ledger_evidence", [])],
            "",
            "## MergeGate",
            f"- Locks: {', '.join(task.get('locks', [])) or 'none'}",
            f"- Risk: {task.get('risk_level', 'low')}",
            "- CCA required: yes",
        ]
    )


def create_github_issue_for_task(target: Path, task_id: str) -> dict[str, Any]:
    require_tool("gh")
    task = load_task(target, task_id)
    config = target_provider_config(target)
    result = run(
        [
            "gh",
            "issue",
            "create",
            "--title",
            f"{task['id']}: {task['title']}",
            "--body",
            github_issue_body(task),
        ],
        cwd=target,
        env=github_env(config) if config else None,
    )
    url = result.stdout.strip().splitlines()[-1]
    issue_number = parse_github_number(url, "issues")
    task["github_issue"] = issue_number
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task["id"],
        ledger_type="handoff",
        summary=f"GitHub Issue #{issue_number} created for {task['id']}",
        evidence=[url],
        links=[url],
    )
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), task)
    return {"task_id": task["id"], "issue": issue_number, "url": url, "ledger_id": ledger_id}


def cmd_github_issue(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(create_github_issue_for_task(target, args.task_id))
    return 0


def create_github_pr_for_task(target: Path, task_id: str, *, base: str, head: str | None = None) -> dict[str, Any]:
    require_tool("gh")
    task = load_task(target, task_id)
    config = target_provider_config(target)
    result = run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--head",
            head or task["expected_branch"],
            "--title",
            f"{task['id']}: {task['title']}",
            "--body",
            github_pr_body(task),
        ],
        cwd=target,
        env=github_env(config) if config else None,
    )
    url = result.stdout.strip().splitlines()[-1]
    pr_number = parse_github_number(url, "pull")
    task["expected_pr"] = pr_number
    ledger_id = append_ledger(
        target,
        goal_id=task["goal_id"],
        task_id=task["id"],
        ledger_type="handoff",
        summary=f"GitHub PR #{pr_number} created for {task['id']}",
        evidence=[url],
        links=[url],
    )
    task.setdefault("ledger_evidence", []).append(ledger_id)
    write_json(shiki_path(target, "tasks", f"{task['id']}.json"), task)
    worktree = worktree_record(target, task["id"])
    if worktree:
        worktree["pr"] = pr_number
        write_json(shiki_path(target, "worktrees", f"{task['id']}.json"), worktree)
    return {"task_id": task["id"], "pr": pr_number, "url": url, "ledger_id": ledger_id}


def cmd_github_pr(args: argparse.Namespace) -> int:
    target = target_path(args.target)
    require_github_first_target(target)
    print_json(create_github_pr_for_task(target, args.task_id, base=args.base, head=args.head))
    return 0
