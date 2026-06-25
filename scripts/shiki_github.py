#!/usr/bin/env python3
"""GitHub CLI/API helpers for Shiki repository, secret, review, and PR evidence operations."""

from __future__ import annotations

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
# unblanked ambient ANTHROPIC_AUTH_TOKEN / API key / Bedrock-Vertex route would
# pass through and let a bad token verify clean — the false positive that hides a
# CCA `401 Invalid bearer token`. Blanking (not unsetting) suffices: each is
# falsy/off when empty, and the merge model can only override keys, not delete
# them.
_PROBE_BLANKED_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
)


def token_probe_invocation(token: str, config_dir: str) -> tuple[list[str], dict[str, str]]:
    """Build the isolated-config probe command + env that validates an OAuth token.

    Isolating ``CLAUDE_CONFIG_DIR``/``HOME`` defeats a local keychain login or
    on-disk settings (``apiKeyHelper`` etc.) masking the supplied env token, and
    every entry in ``_PROBE_BLANKED_CREDENTIAL_ENV`` is blanked so no ambient
    higher-precedence credential or cloud-provider route can authenticate the
    probe. The candidate ``token`` is therefore the sole credential under test:
    an invalid/expired token fails closed instead of silently passing against the
    operator's interactive session or an ambient ``ANTHROPIC_AUTH_TOKEN``.
    """
    env = {name: "" for name in _PROBE_BLANKED_CREDENTIAL_ENV}
    env.update({
        "CLAUDE_CONFIG_DIR": config_dir,
        "HOME": config_dir,
        "CLAUDE_CODE_OAUTH_TOKEN": token,
    })
    argv = ["claude", "-p", "ping", "--output-format", "json"]
    return argv, env


def interpret_token_probe(result: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether a ``claude -p --output-format json`` probe proved the token valid.

    A valid token returns ``is_error=false`` with a billable response
    (``total_cost_usd > 0``). An invalid/expired token short-circuits with
    ``is_error=true``, ``total_cost_usd=0`` and an auth-failure message such as
    ``401 Invalid bearer token``. Fail closed on anything else.
    """
    if not isinstance(result, dict):
        return False, "probe returned no structured result"
    detail = _redact_token(str(result.get("result") or "").strip())
    if result.get("is_error"):
        return False, detail or "authentication failed"
    cost = result.get("total_cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
        return False, detail or "no billable model response (token likely invalid)"
    return True, "token authenticated"


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


def verify_claude_oauth_token(token: str, *, runner=run) -> tuple[bool, str]:
    """Verify a Claude OAuth token by an isolated-config probe (fails closed)."""
    require_tool("claude")
    config_dir = tempfile.mkdtemp(prefix="shiki-token-probe-")
    try:
        argv, env = token_probe_invocation(token, config_dir)
        result = runner(argv, env=env, input_text="", check=False)
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return False, _redact_token(first_line(result.stderr)) or "probe output was not JSON"
        return interpret_token_probe(data)
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


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
            "require_code_owner_reviews": review_count > 0,
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
    in solo operation after CCA returns ``complete`` (see ADR 0013 and
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
    """The PR-12 ``## Pre-PR code review`` body section (ADR 0011).

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
    lines.append("- Independent read-only reviewer (claude -p, read tools only) — ADR 0011")
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
            "(ADR 0011); a red run blocks the PR.",
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
