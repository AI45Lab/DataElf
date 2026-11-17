"""Explicit opt-in boundaries; a graph reference alone never selects a workflow."""


def reviewed_analysis(spec):
    from dataelf.discovery.policies import skill_review, validate_scope
    validate_scope(spec.workflow_policy, spec.domain, spec.workflow_profile)
    return skill_review(spec)


def unbounded_analysis(spec):
    from dataelf.discovery.policies import capabilities
    return capabilities(spec).unbounded
