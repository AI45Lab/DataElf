"""Host execution protocols, independent of domain business orchestration.

Manifests expose named business hooks, not runtime profiles, recovery policies,
budgets or Pi implementation paths. The host binds a protocol to a hook.
"""

import os
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

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
# Historical serialized spelling only, not domain dispatch. Never generate a
# new policy per domain. Both spellings have identical capability/scope checks.
_ALIASES = {"ai_index_cli_skill_review_v1": CLI_SKILL_REVIEW}
_WORKFLOW_KEYS = {}
_DOMAIN_WORKFLOWS = {}
_LOADED = set()


def register_policy(
    name, *, domains, profiles, capabilities, entry=None, aliases=(), workflow=None
):
    """Register a host protocol; domain plugins need not call this function."""
    if not name or name in _POLICIES or name in _ALIASES:
        raise ValueError(f"Workflow policy already registered or empty: {name}")
    if any(alias in _POLICIES or alias in _ALIASES for alias in aliases):
        raise ValueError("Workflow alias already registered")
    if entry is not None and workflow is not None:
        raise ValueError("Choose a host entry or a domain business hook, not both")
    _POLICIES[name] = (
        None if domains is None else set(domains),
        set(profiles),
        capabilities,
    )
    if workflow is not None:
        _WORKFLOW_KEYS[name] = workflow
    if entry is not None:
        _ENTRIES[name] = entry
    for alias in aliases:
        _ALIASES[alias] = name


# A single domain-neutral host preset. Any domain may supply this business
# hook; domain names and executable module paths are not hardcoded here.
register_policy(
    CLI_SKILL_REVIEW,
    domains=None,
    profiles={"research"},
    capabilities=Capabilities(
        graph_query=True,
        graph_maintenance=True,
        structured_outputs=True,
        analysis_review=True,
        recovery=True,
        unbounded=True,
    ),
    workflow="reviewed_analysis",
)


def load_domain_workflows(domain, root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[1] / "domains"
    path = (root / domain / "domain.yaml").resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Domain manifest escapes registry root")
    if path in _LOADED or not path.is_file():
        return
    manifest = yaml.safe_load(path.read_text()) or {}
    if manifest.get("domain") != domain:
        raise ValueError("Domain manifest identity mismatch")
    # Delayed to avoid the contracts -> policy scope validator dependency.
    from .contracts import DomainWorkflow

    entries = {
        name: DomainWorkflow.model_validate(declaration).entry
        for name, declaration in manifest.get("workflows", {}).items()
    }
    previous = _DOMAIN_WORKFLOWS.get(domain)
    if previous is not None and previous != entries:
        raise ValueError(f"Conflicting workflow declarations for domain: {domain}")
    _DOMAIN_WORKFLOWS[domain] = entries
    _LOADED.add(path)


def _canonical(policy, domain=None):
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
    workflow = _WORKFLOW_KEYS.get(policy)
    if workflow is not None:
        load_domain_workflows(domain)
        if workflow not in _DOMAIN_WORKFLOWS.get(domain, {}):
            raise ValueError(
                f"Workflow policy {policy} is not supported for {domain}/{profile}: "
                f"missing business hook {workflow}"
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
    policy = _canonical(spec.workflow_policy, spec.domain)
    workflow = _WORKFLOW_KEYS.get(policy)
    entry = (
        _DOMAIN_WORKFLOWS[spec.domain][workflow]
        if workflow is not None
        else _ENTRIES[policy]
    )
    if callable(entry):
        return entry
    module, separator, name = entry.partition(":")
    if not separator:
        raise ValueError("Workflow entry must use module:function syntax")
    executor = getattr(import_module(module), name)
    if not callable(executor):
        raise TypeError("Workflow entry must be callable")
    return executor
