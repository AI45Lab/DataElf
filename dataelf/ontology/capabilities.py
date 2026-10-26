"""Host-selected workflow capabilities; legacy remains the shared default."""
from dataclasses import dataclass
import os

CLI_SKILL_REVIEW = 'ai_index_cli_skill_review_v1'
WorkflowPolicy = str
NETWORK_ENV_KEYS=('http_proxy','https_proxy','all_proxy','no_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY')


def network_environment(value):
    """Explicit Skill roles inherit the host's network route, never expose it in prompts."""
    return {key:os.environ[key] for key in NETWORK_ENV_KEYS if key in os.environ} if skill_review(value) else {}


def skill_review(value) -> bool:
    if isinstance(value, dict):
        policy = value.get('workflow_policy', value.get('request', {}).get('workflow_policy'))
    else:
        policy = getattr(value, 'workflow_policy', None)
    return policy == CLI_SKILL_REVIEW


def cli_policy(domain: str, source_mode: str = 'api', *, requested='legacy') -> WorkflowPolicy:
    """Source mode never selects an execution policy."""
    validate_scope(requested, domain)
    return requested


@dataclass(frozen=True)
class Capabilities:
    modeling: bool = True  # Only an explicitly supplied plugin modeler.
    graph_query: bool = False
    graph_maintenance: bool = False
    structured_outputs: bool = False
    analysis_review: bool = False
    recovery: bool = False
    unbounded: bool = False


_POLICIES = {
    'legacy': (None, {'research', 'server'}, Capabilities()),
    CLI_SKILL_REVIEW: ({'ai_index'}, {'research'}, Capabilities(
        graph_query=True, graph_maintenance=True, structured_outputs=True,
        analysis_review=True, recovery=True, unbounded=True)),
}


def register_policy(name, *, domains, profiles, capabilities):
    """Domain extensions register their own policy before creating tasks."""
    if not name or name in _POLICIES:
        raise ValueError(f'Workflow policy already registered or empty: {name}')
    _POLICIES[name] = (set(domains), set(profiles), capabilities)


def capabilities(spec):
    policy = getattr(spec, 'workflow_policy', 'legacy')
    validate_scope(policy, spec.domain, spec.workflow_profile)
    return _POLICIES[policy][2]


def validate_scope(policy, domain, profile='research'):
    if policy not in _POLICIES:
        raise ValueError(f'Unregistered workflow policy: {policy}')
    domains, profiles, _ = _POLICIES[policy]
    if (domains is not None and domain not in domains) or profile not in profiles:
        raise ValueError(f'Workflow policy {policy} is not supported for {domain}/{profile}')
