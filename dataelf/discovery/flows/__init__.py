"""Thin execution dispatch; plugins and registered policies own their behavior."""
from dataelf.ontology.capabilities import CLI_SKILL_REVIEW, validate_scope, register_policy


def resolve_spec(spec, config, saved_spec=None):
    # Saved tasks are loaded using JobSpec.from_saved; their explicit choice wins.
    policy = spec.workflow_policy
    if saved_spec is not None:
        if (saved_spec.domain, saved_spec.workflow_profile) != (spec.domain, spec.workflow_profile):
            raise ValueError('Cannot recover a task using another domain or profile')
        if 'workflow_policy' in spec.model_fields_set and policy != saved_spec.workflow_policy:
            raise ValueError('Cannot change the saved workflow selection during recovery')
        policy = saved_spec.workflow_policy
    elif 'workflow_policy' not in spec.model_fields_set and spec.workflow_profile != 'server':
        policy = config.workflow_policy
    validate_scope(policy, spec.domain, spec.workflow_profile)
    from dataelf.discovery.contracts import JobSpec
    return JobSpec.model_validate({**spec.model_dump(), 'workflow_policy': policy})


def legacy(*args):
    from .legacy import execute
    return execute(*args)


def ai_index(*args):
    from .ai_index import execute
    return execute(*args)


_FLOWS = {'legacy': legacy, CLI_SKILL_REVIEW: ai_index}


def register_workflow(name, execute, *, domains, profiles, capabilities):
    if not callable(execute):
        raise TypeError('Workflow executor must be callable')
    register_policy(name, domains=domains, profiles=profiles, capabilities=capabilities)
    _FLOWS[name] = execute


def execution_flow(spec):
    validate_scope(spec.workflow_policy, spec.domain, spec.workflow_profile)
    if spec.workflow_profile == 'server':
        from dataelf_server.workflows.entry import execute
        return execute
    return _FLOWS[spec.workflow_policy]
