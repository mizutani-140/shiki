#!/usr/bin/env python3
"""GitHub-compatible provider and remote configuration helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

RemoteProtocol = str
ProviderKind = str

DEFAULT_PROVIDER = "github"
DEFAULT_GITHUB_HOST = "github.com"
DEFAULT_REMOTE_PROTOCOL = "https"

REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$")


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    host: str
    protocol: str
    repo: str
    web_base_url: str
    api_base_url: str
    ssh_host: str


class ProviderConfigError(ValueError):
    pass


def normalize_repo_slug(repo: str) -> str:
    value = repo.strip().removesuffix("/")
    if value.endswith(".git"):
        value = value[:-4]
    validate_repo_slug(value)
    return value


def validate_repo_slug(repo: str) -> None:
    if not REPO_SLUG.match(repo):
        raise ProviderConfigError("repo must be a GitHub slug like OWNER/NAME")


def validate_host(host: str) -> None:
    if not host or "/" in host or ":" in host or not HOSTNAME.match(host):
        raise ProviderConfigError("GitHub host must be a hostname like github.com")


def api_base_url_for_host(host: str) -> str:
    if host == DEFAULT_GITHUB_HOST:
        return "https://api.github.com"
    return f"https://{host}/api/v3"


def provider_from_values(
    *,
    repo: str,
    host: str | None = None,
    protocol: str | None = None,
    provider: str | None = None,
    api_base_url: str | None = None,
) -> ProviderConfig:
    provider_value = (provider or DEFAULT_PROVIDER).strip().lower()
    if provider_value != DEFAULT_PROVIDER:
        raise ProviderConfigError("Only provider=github is currently supported.")

    host_value = (host or DEFAULT_GITHUB_HOST).strip().lower()
    validate_host(host_value)

    protocol_value = (protocol or DEFAULT_REMOTE_PROTOCOL).strip().lower()
    if protocol_value not in {"https", "ssh"}:
        raise ProviderConfigError("remote protocol must be https or ssh")

    repo_value = normalize_repo_slug(repo)
    web_base_url = f"https://{host_value}"
    api_value = (api_base_url or api_base_url_for_host(host_value)).strip().removesuffix("/")
    parsed_api = urlparse(api_value)
    if parsed_api.scheme != "https" or not parsed_api.netloc:
        raise ProviderConfigError("GitHub API URL must be an https URL")

    return ProviderConfig(
        provider=provider_value,
        host=host_value,
        protocol=protocol_value,
        repo=repo_value,
        web_base_url=web_base_url,
        api_base_url=api_value,
        ssh_host=host_value,
    )


def default_provider_config(repo: str) -> ProviderConfig:
    return provider_from_values(repo=repo)


def canonical_remote_url(config: ProviderConfig) -> str:
    if config.protocol == "ssh":
        return f"git@{config.ssh_host}:{config.repo}.git"
    return f"{config.web_base_url}/{config.repo}.git"


def _remote_parts(url: str) -> tuple[str, str]:
    value = url.strip().removesuffix("/")
    if value.startswith("git@") and ":" in value:
        host, path = value.removeprefix("git@").split(":", 1)
        return host.lower(), normalize_repo_slug(path)

    parsed = urlparse(value)
    if parsed.scheme in {"https", "http", "ssh"} and parsed.netloc:
        host = parsed.netloc
        if "@" in host:
            host = host.split("@", 1)[1]
        return host.lower(), normalize_repo_slug(parsed.path.lstrip("/"))

    raise ProviderConfigError(f"unsupported Git remote URL: {url}")


def canonicalize_remote_url(url: str) -> str:
    host, repo = _remote_parts(url)
    return f"https://{host}/{repo}"


def remote_matches_provider(url: str, config: ProviderConfig) -> bool:
    try:
        host, repo = _remote_parts(url)
    except ProviderConfigError:
        return False
    return host == config.host and repo == config.repo


def repo_api_path(config: ProviderConfig, suffix: str = "") -> str:
    suffix_value = suffix.strip("/")
    base = f"repos/{config.repo}"
    return f"{base}/{suffix_value}" if suffix_value else base


def provider_config_as_json(config: ProviderConfig) -> dict[str, str]:
    return {
        "provider": config.provider,
        "repo": config.repo,
        "host": config.host,
        "remote_protocol": config.protocol,
        "web_base_url": config.web_base_url,
        "api_base_url": config.api_base_url,
        "ssh_host": config.ssh_host,
        "canonical_remote_url": canonical_remote_url(config),
    }


def provider_from_repo_json(data: dict[str, object]) -> ProviderConfig:
    repo = str(data.get("repo") or "")
    return provider_from_values(
        repo=repo,
        provider=str(data.get("provider") or DEFAULT_PROVIDER),
        host=str(data.get("host") or DEFAULT_GITHUB_HOST),
        protocol=str(data.get("remote_protocol") or DEFAULT_REMOTE_PROTOCOL),
        api_base_url=str(data.get("api_base_url") or api_base_url_for_host(str(data.get("host") or DEFAULT_GITHUB_HOST))),
    )


def github_env(config: ProviderConfig) -> dict[str, str]:
    if config.host == DEFAULT_GITHUB_HOST:
        return {}
    return {"GH_HOST": config.host}
