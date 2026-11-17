"""Execution capabilities registered by domain manifests, independent of ontology."""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
import os
import yaml

WorkflowPolicy = str
CLI_SKILL_REVIEW = "cli_skill_review_v1"
NETWORK_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


@dataclass(frozen=True)
class Capabilities:
    modeling: bool = True
    graph_query: bool = False
    graph_maintenance: bool = False
    structured_outputs: bool = False
    analysis_review: bool = False
    recovery: bool = False
    unbounded: bool = False


_POLICIES = {"legacy": (None, {"research", "server"}, Capabilities())}
_ENTRIES = {}
_ALIASES = {}
_LOADED = set()


def register_policy(name, *, domains, profiles, capabilities, entry=None, aliases=()):
    if not name or name in _POLICIES or name in _ALIASES:
        raise ValueError(f"Workflow policy already registered or empty: {name}")
    if any(alias in _POLICIES or alias in _ALIASES for alias in aliases):
        raise ValueError("Workflow alias already registered")
    _POLICIES[name] = (set(domains), set(profiles), capabilities)
    if entry is not None:
        _ENTRIES[name] = entry
    for alias in aliases:
        _ALIASES[alias] = name


def load_domain_policies(domain, root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[1] / "domains"
    path = (root / domain / "domain.yaml").resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Domain manifest escapes registry root")
    if path in _LOADED or not path.is_file():
        return
    manifest = yaml.safe_load(path.read_text()) or {}
    if manifest.get("domain") != domain:
        raise ValueError("Domain manifest identity mismatch")
    for name, declaration in manifest.get("workflows", {}).items():
        register_policy(
            name,
            domains={domain},
            profiles=declaration["profiles"],
            capabilities=Capabilities(**declaration.get("capabilities", {})),
            entry=declaration["entry"],
            aliases=declaration.get("aliases", ()),
        )
    _LOADED.add(path)


def _canonical(policy, domain=None):
    if policy not in _POLICIES and policy not in _ALIASES:
        if domain:
            load_domain_policies(domain)
        else:
            root = Path(__file__).resolve().parents[1] / "domains"
            for manifest in sorted(root.glob("*/domain.yaml")):
                load_domain_policies(manifest.parent.name, root)
    return _ALIASES.get(policy, policy)


def validate_scope(policy, domain, profile="research"):
    policy = _canonical(policy, domain)
    if policy not in _POLICIES:
        raise ValueError(f"Unregistered workflow policy: {policy}")
    domains, profiles, _ = _POLICIES[policy]
    if (domains is not None and domain not in domains) or profile not in profiles:
        raise ValueError(
            f"Workflow policy {policy} is not supported for {domain}/{profile}"
        )


def capabilities(spec):
    validate_scope(spec.workflow_policy, spec.domain, spec.workflow_profile)
    return _POLICIES[_canonical(spec.workflow_policy, spec.domain)][2]


def skill_review(value):
    if isinstance(value, dict):
        policy = value.get(
            "workflow_policy", value.get("request", {}).get("workflow_policy")
        )
    else:
        policy = getattr(value, "workflow_policy", None)
    record = _POLICIES.get(_canonical(policy)) if policy else None
    return bool(record and record[2].analysis_review)


def cli_policy(domain, source_mode="api", *, requested="legacy"):
    validate_scope(requested, domain)
    return requested


def network_environment(value):
    return (
        {key: os.environ[key] for key in NETWORK_ENV_KEYS if key in os.environ}
        if skill_review(value)
        else {}
    )


def workflow_executor(spec):
    validate_scope(spec.workflow_policy, spec.domain, spec.workflow_profile)
    entry = _ENTRIES[_canonical(spec.workflow_policy, spec.domain)]
    if callable(entry):
        return entry
    module, separator, name = entry.partition(":")
    if not separator:
        raise ValueError("Workflow entry must use module:function syntax")
    executor = getattr(import_module(module), name)
    if not callable(executor):
        raise TypeError("Workflow entry must be callable")
    return executor
