from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


VALID_NETWORK_MODES = {"online", "offline"}
VALID_RESOURCE_TIERS = {"light", "standard", "full"}


@dataclass(frozen=True)
class RuntimePolicy:
    network_mode: str = "online"
    resource_tier: str = "light"

    @property
    def online(self) -> bool:
        return self.network_mode == "online"

    @property
    def offline(self) -> bool:
        return self.network_mode == "offline"

    @property
    def light_resource_tier(self) -> bool:
        return self.resource_tier == "light"

    @property
    def standard_resource_tier(self) -> bool:
        return self.resource_tier == "standard"

    @property
    def full_resource_tier(self) -> bool:
        return self.resource_tier == "full"

    @property
    def model_policy(self) -> str:
        return "local_only" if self.offline else "allow_download"

    @property
    def strict_preflight(self) -> bool:
        return self.offline


@dataclass(frozen=True)
class PreflightIssue:
    level: str
    code: str
    message: str
    checker_name: str | None = None

    def format(self) -> str:
        prefix = f"[{self.code}]"
        if self.checker_name:
            prefix += f" {self.checker_name}:"
        return f"{prefix} {self.message}"


def build_runtime_policy(config: Any) -> RuntimePolicy:
    deployment = _get_section(config, "deployment")
    network_mode = str(_get_value(deployment, "network_mode", "online") or "online").strip().lower()
    resource_tier = str(_get_value(deployment, "resource_tier", "light") or "light").strip().lower()
    return RuntimePolicy(network_mode=network_mode, resource_tier=resource_tier)


def apply_runtime_environment(policy: RuntimePolicy) -> None:
    if not policy.offline:
        return
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_DATASETS_OFFLINE", "1")


def run_global_preflight(config: Any, policy: RuntimePolicy) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []

    if policy.network_mode not in VALID_NETWORK_MODES:
        issues.append(PreflightIssue(
            level="error",
            code="invalid_network_mode",
            message=(
                f"deployment.network_mode must be one of {sorted(VALID_NETWORK_MODES)}, "
                f"got {policy.network_mode!r}."
            ),
        ))

    if policy.resource_tier not in VALID_RESOURCE_TIERS:
        issues.append(PreflightIssue(
            level="error",
            code="invalid_resource_tier",
            message=(
                f"deployment.resource_tier must be one of {sorted(VALID_RESOURCE_TIERS)}, "
                f"got {policy.resource_tier!r}."
            ),
        ))

    if not policy.offline:
        # TODO: (network_mode) Add best-effort online dependency checks here as tools
        # expose cheap, non-network preflight hooks.
        return issues
        

    # TODO: (network_mode) !!!!Re-enable strict offline endpoint checks after local
    # LLM deployment is ready. Development still uses an external relay LLM.
    # issues.extend(_validate_offline_llm_endpoint(config, "agent", required=_agent_requires_llm(config)))
    # issues.extend(_validate_offline_llm_endpoint(config, "tool_llm", required=_tool_llm_configured(config)))
    return issues


def handle_preflight_issues(
    issues: list[PreflightIssue],
    *,
    strict: bool,
    logger: Any | None = None,
) -> None:
    if not issues:
        return

    errors = [issue for issue in issues if issue.level == "error"]
    warnings = [issue for issue in issues if issue.level != "error"]

    for issue in warnings:
        if logger is not None and hasattr(logger, "warning"):
            logger.warning(f"Preflight warning: {issue.format()}")

    blocking = errors + (warnings if strict else [])
    if blocking:
        message = "Preflight failed:\n" + "\n".join(f"  - {issue.format()}" for issue in blocking)
        raise RuntimeError(message)


def _validate_offline_llm_endpoint(
    config: Any,
    section_name: str,
    *,
    required: bool,
) -> list[PreflightIssue]:
    section = _get_section(config, section_name)
    base_url = _get_value(section, "base_url", None)
    if not base_url:
        if not required:
            return []
        return [PreflightIssue(
            level="error",
            code="offline_missing_llm_endpoint",
            message=(
                f"deployment.network_mode=offline requires {section_name}.base_url "
                "to point to a local or intranet OpenAI-compatible service."
            ),
        )]

    if _is_public_ip_endpoint(str(base_url)):
        return [PreflightIssue(
            level="error",
            code="offline_public_llm_endpoint",
            message=(
                f"deployment.network_mode=offline cannot use public endpoint "
                f"{section_name}.base_url={base_url!r}."
            ),
        )]
    # TODO: (network_mode) Add optional endpoint allowlisting once the first
    # deployment config needs approved intranet hostnames beyond private IPs.
    return []


def _agent_requires_llm(config: Any) -> bool:
    agent = _get_section(config, "agent")
    return _get_value(agent, "type", "opencode") == "opencode"


def _tool_llm_configured(config: Any) -> bool:
    tool_llm = _get_section(config, "tool_llm")
    return bool(_get_value(tool_llm, "model", None) or _get_value(tool_llm, "base_url", None))


def _is_public_ip_endpoint(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostnames may be intranet DNS names; keep the first version flexible.
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


def _get_section(config: Any, name: str) -> Any:
    if isinstance(config, dict):
        return config.get(name, {})
    return getattr(config, name, {})


def _get_value(section: Any, name: str, default: Any = None) -> Any:
    if isinstance(section, dict):
        return section.get(name, default)
    return getattr(section, name, default)
