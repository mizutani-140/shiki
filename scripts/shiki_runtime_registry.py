"""Canonical Shiki runtime registry and adapter contract metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


RuntimeRole = Literal[
    "front",
    "planner",
    "implementer",
    "completion_checker",
    "reviewer",
    "verifier",
    "runner",
    "human_gate",
]

RuntimeExecutionMode = Literal[
    "local_cli",
    "github_action",
    "workflow_job",
    "human",
    "external_runner",
    "placeholder",
]

RuntimeAuthMode = Literal[
    "none",
    "chatgpt_oauth",
    "claude_subscription_oauth",
    "github_token",
    "github_secret",
    "manual",
    "future",
]

RUNTIME_ROLES: tuple[str, ...] = (
    "front",
    "planner",
    "implementer",
    "completion_checker",
    "reviewer",
    "verifier",
    "runner",
    "human_gate",
)

CONFIG_RUNTIME_ROLES: tuple[str, ...] = (
    "front",
    "planner",
    "implementer",
    "completion_checker",
    "reviewer",
    "verifier",
)

TASK_RUNTIME_ROLES: tuple[str, ...] = ("planner", "implementer", "runner", "human_gate")


class RuntimeRegistryError(ValueError):
    """Raised when a Shiki runtime contract is invalid."""


@dataclass(frozen=True)
class RuntimeDescriptor:
    name: str
    display_name: str
    roles: tuple[str, ...]
    execution_mode: str
    auth_mode: str
    required_tools: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = ()
    github_workflows: tuple[str, ...] = ()
    supports_local_execution: bool = False
    supports_github_execution: bool = False
    supports_automated_review: bool = False
    supports_completion_judgment: bool = False
    supports_handoff: bool = False
    experimental: bool = False
    deprecated: bool = False
    requires_rationale: bool = False
    description: str = ""


_RUNTIME_DESCRIPTORS: tuple[RuntimeDescriptor, ...] = (
    RuntimeDescriptor(
        name="claude-code",
        display_name="Claude Code",
        roles=("planner", "implementer", "runner", "reviewer"),
        execution_mode="local_cli",
        auth_mode="claude_subscription_oauth",
        required_tools=("claude",),
        supports_local_execution=True,
        supports_handoff=True,
        description="Local Claude Code planning, review, and default implementation/runner runtime (ADR 0008).",
    ),
    RuntimeDescriptor(
        name="claude-code-action",
        display_name="Claude Code Action",
        roles=("reviewer",),
        execution_mode="github_action",
        auth_mode="github_secret",
        required_secrets=("CLAUDE_CODE_OAUTH_TOKEN",),
        github_workflows=("Shiki Claude Review",),
        supports_github_execution=True,
        supports_automated_review=True,
        description="GitHub Actions reviewer runtime using Claude Code Action.",
    ),
    RuntimeDescriptor(
        name="codex",
        display_name="Codex CLI",
        roles=("front", "implementer", "runner"),
        execution_mode="local_cli",
        auth_mode="chatgpt_oauth",
        required_tools=("codex",),
        supports_local_execution=True,
        supports_handoff=True,
        description="Local Codex implementation and runner runtime.",
    ),
    RuntimeDescriptor(
        name="codex-front",
        display_name="Codex Front",
        roles=("front", "implementer"),
        execution_mode="local_cli",
        auth_mode="chatgpt_oauth",
        required_tools=("codex",),
        supports_local_execution=True,
        supports_handoff=True,
        description="Operator-facing Codex entrypoint used by Shiki front/runtime assignment.",
    ),
    RuntimeDescriptor(
        name="github-actions",
        display_name="GitHub Actions",
        roles=("verifier",),
        execution_mode="workflow_job",
        auth_mode="github_token",
        github_workflows=("Shiki Validate", "Shiki MergeGate", "Shiki Orchestrator"),
        supports_github_execution=True,
        description="GitHub Actions verifier and workflow executor runtime.",
    ),
    RuntimeDescriptor(
        name="github-cca",
        display_name="GitHub CCA",
        roles=("completion_checker",),
        execution_mode="workflow_job",
        auth_mode="github_token",
        required_secrets=("CLAUDE_CODE_OAUTH_TOKEN",),
        github_workflows=("Shiki CCA Completion",),
        supports_github_execution=True,
        supports_completion_judgment=True,
        description="GitHub Actions completion-check runtime for CCA verdicts.",
    ),
    RuntimeDescriptor(
        name="hermes-runner",
        display_name="Hermes Runner",
        roles=("runner",),
        execution_mode="external_runner",
        auth_mode="future",
        supports_handoff=True,
        experimental=True,
        description="Future external runner placeholder; contract is defined but no adapter is implemented.",
    ),
    RuntimeDescriptor(
        name="human",
        display_name="Human",
        roles=("human_gate", "reviewer"),
        execution_mode="human",
        auth_mode="manual",
        supports_automated_review=False,
        description="Manual human-in-the-loop gate or reviewer runtime.",
    ),
    RuntimeDescriptor(
        name="other",
        display_name="Other Runtime",
        roles=RUNTIME_ROLES,
        execution_mode="placeholder",
        auth_mode="future",
        experimental=True,
        deprecated=True,
        requires_rationale=True,
        description="Legacy fallback placeholder. New config use requires explicit rationale.",
    ),
)


def runtime_registry() -> dict[str, RuntimeDescriptor]:
    return {descriptor.name: descriptor for descriptor in _RUNTIME_DESCRIPTORS}


def runtime_names() -> tuple[str, ...]:
    return tuple(sorted(runtime_registry()))


def get_runtime(name: str) -> RuntimeDescriptor:
    try:
        return runtime_registry()[name]
    except KeyError as error:
        raise RuntimeRegistryError(f"unknown runtime: {name}") from error


def runtime_exists(name: str) -> bool:
    return name in runtime_registry()


def runtimes_for_role(role: str) -> tuple[RuntimeDescriptor, ...]:
    validate_runtime_role(role)
    return tuple(descriptor for descriptor in runtime_registry().values() if role in descriptor.roles)


def validate_runtime_role(role: str) -> None:
    if role not in RUNTIME_ROLES:
        raise RuntimeRegistryError(f"unknown runtime role: {role}")


def validate_runtime_name(name: str) -> None:
    get_runtime(name)


def validate_runtime_role_assignment(role: str, runtime_name: str) -> None:
    validate_runtime_role(role)
    descriptor = get_runtime(runtime_name)
    if role not in descriptor.roles:
        raise RuntimeRegistryError(f"runtime {runtime_name!r} is not allowed for role {role!r}")


def runtime_registry_as_json() -> dict[str, object]:
    return {
        name: asdict(runtime_registry()[name])
        for name in runtime_names()
    }
