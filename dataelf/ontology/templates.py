"""Domain-checked, portable templates with explicit source bindings."""
from __future__ import annotations

import json
from pathlib import Path

from .contracts import OntologyContract, OntologyTemplate, SourceInput, TemplateSource, ExtractionPlan


# Historical imports remain compatible for unmigrated domains.
from .domain_policy import TemplateDomainError, require_domain


def load_templates(paths: list[Path], *, domain: str) -> OntologyTemplate:
    if not paths:
        raise ValueError("template mode requires at least one template")
    combined = None
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != "dataelf.ontology.template.v1":
            raise ValueError(f"unsupported template version: {path}; use dataelf.ontology.template.v1 with an explicit domain")
        if not isinstance(raw.get("domain"), str) or not raw["domain"].strip():
            raise TemplateDomainError(f"template must explicitly declare its domain: {path}")
        template = OntologyTemplate.model_validate(raw)
        require_domain(template.domain, domain)
        if combined is None:
            combined = template
            continue
        contract, target_contract = template.contract, combined.contract
        if contract.namespace != target_contract.namespace or contract.graphs != target_contract.graphs:
            raise ValueError("template namespaces/graphs conflict")
        for target, values, label in [(combined.source_bindings, template.source_bindings, "source_bindings"),
                                      *[(getattr(target_contract, attr), getattr(contract, attr), attr)
                                        for attr in ("classes", "properties")]]:
            for key, value in values.items():
                if key in target and target[key] != value:
                    raise ValueError(f"conflicting template definition: {label}.{key}")
                target[key] = value
        target_contract.mappings.extend(contract.mappings)
        target_contract.constraints.extend(contract.constraints)
        target_contract.queries.extend(contract.queries)
    return combined


def bind_template(template: OntologyTemplate, sources: list[SourceInput]) -> list[SourceInput]:
    result = []
    for source in sources:
        binding = template.source_bindings.get(source.source_id)
        if binding is None:
            result.append(source)
            continue
        fmt = source.format or source.path.suffix.lstrip('.').lower()
        if fmt != binding.format:
            raise ValueError(f"template source format mismatch: {source.source_id}; expected {binding.format}, got {fmt}")
        if source.record_pointers is not None and binding.records_path:
            raise ValueError(f"template binding conflicts with exact record selection: {source.source_id}")
        result.append(source.model_copy(update={'records_path': binding.records_path}))
    return result


def export_template(contract: OntologyContract, plan: ExtractionPlan, sources: list[SourceInput]) -> OntologyTemplate:
    """Freeze the final reviewed semantics and executed selectors, not file paths."""
    portable = contract.model_copy(deep=True)
    for mapping in portable.mappings:
        mapping.selector = plan.selectors.get(mapping.id, mapping.selector)
        # Grounding of the research run stays in stage1/contract.json and reviews.
        mapping.evidence = []
    bindings = {}
    for source in sources:
        if source.record_pointers is not None:
            raise ValueError("cannot export reusable template with snapshot-specific record_pointers")
        binding = TemplateSource(format=source.format or source.path.suffix.lstrip('.').lower(), records_path=source.records_path)
        if source.source_id in bindings and bindings[source.source_id] != binding:
            raise ValueError(f"cannot export conflicting source selections: {source.source_id}")
        bindings[source.source_id] = binding
    return OntologyTemplate(domain=contract.domain, contract=portable, source_bindings=bindings)
