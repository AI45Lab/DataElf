"""Explicit opt-in boundaries; a graph reference alone never selects a workflow."""


def ai_index_research(spec):
    from dataelf.ontology.capabilities import skill_review, validate_scope
    validate_scope(spec.workflow_policy, spec.domain, spec.workflow_profile)
    return skill_review(spec)


def unbounded_analysis(spec):
    from dataelf.ontology.capabilities import capabilities
    return capabilities(spec).unbounded
