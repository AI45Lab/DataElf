from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from rdflib import BNode, Dataset, Graph, Literal, Namespace, RDF, RDFS, URIRef
from rdflib.namespace import OWL, XSD

from dataelf.domains.ai_index.modeling.contracts import (
    OntologyRunResult,
    AI_INDEX_MODELING_STAGE1_FAILED,
)


BASE_SCHEMA_VERSION = "dataelf-scope-v2-template-base.v1"
FRAGMENT_SCHEMA_VERSION = "dataelf-scope-v2-template-fragment.v1"
SOURCE_ENDPOINTS = {
    "news": "/openapi/news/search",
    "twitter": "/openapi/ecosystem/xx/search",
    "github": "/openapi/ecosystem/gh/search",
    "huggingface": "/openapi/ecosystem/hf/search",
    "youtube": "/openapi/ecosystem/y2b/search",
}


class ScopeV2TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class ComposedScopeV2Template:
    namespace: str
    sources: tuple[str, ...]
    templates: tuple[str, ...]
    classes: dict[str, dict[str, Any]]
    object_properties: dict[str, dict[str, Any]]
    datatype_properties: dict[str, dict[str, Any]]
    fragments: dict[str, dict[str, Any]]

    def ontology(self, source_fingerprint: str) -> dict[str, Any]:
        return {
            "schemaVersion": "dataelf-ontology.v2",
            "metadata": {
                "ontologyId": "dataelf_ai_index_scope_v2",
                "namespace": self.namespace,
                "title": "DataElf AI Index Scope V2 Ontology",
                "description": "Composed fixed ontology for prefetched Scope V2 sources.",
                "version": "1",
                "sourceFingerprint": source_fingerprint,
                "generationMode": "composed_template",
                "templateIds": list(self.templates),
            },
            "classes": self.classes,
            "objectProperties": self.object_properties,
            "datatypeProperties": self.datatype_properties,
        }


def default_template_root() -> Path:
    return Path(__file__).resolve().parent / "templates" / "scope_v2"


def compose_scope_v2_templates(
    sources: Iterable[str], template_root: Path | None = None
) -> ComposedScopeV2Template:
    selected = tuple(dict.fromkeys(str(value) for value in sources))
    if not selected:
        raise ScopeV2TemplateError("no Scope V2 source was selected")
    unknown = sorted(set(selected) - set(SOURCE_ENDPOINTS))
    if unknown:
        raise ScopeV2TemplateError(f"unsupported Scope V2 template sources: {unknown}")
    root = (template_root or default_template_root()).resolve()
    base = _read_json(root / "base.json")
    if base.get("schemaVersion") != BASE_SCHEMA_VERSION or base.get("templateId") != "scope_v2_base":
        raise ScopeV2TemplateError("invalid Scope V2 base template")
    if base.get("approval", {}).get("decision") != "approve":
        raise ScopeV2TemplateError("Scope V2 base template is not approved")

    classes: dict[str, dict[str, Any]] = {}
    objects: dict[str, dict[str, Any]] = {}
    datatypes: dict[str, dict[str, Any]] = {}
    _merge_definitions(classes, base.get("classes"), "class", "scope_v2_base")
    _merge_definitions(objects, base.get("objectProperties"), "object property", "scope_v2_base")
    _merge_definitions(datatypes, base.get("datatypeProperties"), "datatype property", "scope_v2_base")
    fragments: dict[str, dict[str, Any]] = {}
    entity_classes: set[str] = set()
    observation_classes: set[str] = set()
    template_ids = ["scope_v2_base"]
    for source in selected:
        fragment = _read_json(root / f"{source}.json")
        expected_id = f"scope_v2_{source}"
        if (
            fragment.get("schemaVersion") != FRAGMENT_SCHEMA_VERSION
            or fragment.get("templateId") != expected_id
            or fragment.get("source") != source
            or fragment.get("endpoint") != SOURCE_ENDPOINTS[source]
        ):
            raise ScopeV2TemplateError(f"invalid Scope V2 template fragment: {source}")
        if fragment.get("approval", {}).get("decision") != "approve":
            raise ScopeV2TemplateError(f"Scope V2 template fragment is not approved: {source}")
        _validate_fragment_mappings(fragment)
        _merge_definitions(classes, fragment.get("classes"), "class", expected_id)
        _merge_definitions(objects, fragment.get("objectProperties"), "object property", expected_id)
        _merge_definitions(datatypes, fragment.get("datatypeProperties"), "datatype property", expected_id)
        fragments[source] = fragment
        entity_class = str(fragment.get("entity", {}).get("classId") or "")
        observation_class = str(fragment.get("entity", {}).get("observationClassId") or "")
        if entity_class:
            entity_classes.add(entity_class)
        if observation_class:
            observation_classes.add(observation_class)
        template_ids.append(expected_id)

    for identifier in entity_classes:
        if identifier in classes:
            classes[identifier]["subClassOf"] = "DomainEntity"
    for identifier in observation_classes:
        if identifier in classes:
            classes[identifier]["subClassOf"] = "EntityObservation"
    _normalize_property_domains(objects, entity_classes, observation_classes)
    _normalize_property_domains(datatypes, entity_classes, observation_classes)

    # A generated fragment may refer to a shared semantic class without
    # repeating its definition. Preserve strict property references by adding a
    # conservative concept declaration rather than dropping the relation.
    referenced_classes = {
        str(item.get(key))
        for item in [*objects.values(), *datatypes.values()]
        for key in ("domain", "range")
        if item.get(key) and not str(item.get(key)).startswith("xsd:")
    }
    for identifier in sorted(referenced_classes - set(classes)):
        classes[identifier] = {
            "id": identifier,
            "label": identifier,
            "kind": "concept",
            "comment": f"Shared Scope V2 concept {identifier}.",
        }
    return ComposedScopeV2Template(
        namespace=str(base.get("namespace") or "urn:dataelf:ontology:ai-index:"),
        sources=selected,
        templates=tuple(template_ids),
        classes=classes,
        object_properties=objects,
        datatype_properties=datatypes,
        fragments=fragments,
    )


class ScopeV2TemplateOntologyRunner:
    """Bind generated template fragments and deterministically compile Scope V2 RDF."""

    def __init__(self, template_root: Path | None = None):
        self.template_root = (template_root or default_template_root()).resolve()

    def run(self, workspace_path: Path) -> OntologyRunResult:
        workspace = workspace_path.resolve()
        try:
            result_path = _scope_result_path(workspace)
            result = _read_json(result_path)
            source_details = {
                source: details
                for source, details in result.get("sources", {}).items()
                if source in SOURCE_ENDPOINTS and isinstance(details, dict)
            }
            sources = tuple(source_details)
            item_count = sum(int(value.get("kept_count") or 0) for value in source_details.values())
            if not sources or item_count <= 0:
                raise ScopeV2TemplateError("Scope V2 template binding requires at least one source record")
            composed = compose_scope_v2_templates(sources, self.template_root)
            source_fingerprint = _sha256_json({
                source: [item.get("source_id") for item in details.get("items", []) if isinstance(item, dict)]
                for source, details in source_details.items()
            })
            run_id = f"scope_v2_template_{source_fingerprint[:16]}"
            stage1_bundle = workspace / "modeling" / "server" / "stage1" / "published" / run_id
            stage2_bundle = workspace / "modeling" / "server" / "stage2" / "published" / run_id
            stage1_bundle.mkdir(parents=True, exist_ok=True)
            stage2_bundle.mkdir(parents=True, exist_ok=True)
            ontology = composed.ontology(source_fingerprint)
            grounding = {
                "schemaVersion": "dataelf-grounding.v2",
                "sourceFingerprint": source_fingerprint,
                "generationMode": "composed_template",
                "templateIds": list(composed.templates),
                "scopeV2Result": str(result_path),
                "sourceFragments": {
                    source: {
                        "templateId": fragment["templateId"],
                        "endpoint": fragment["endpoint"],
                        "entity": fragment["entity"],
                        "scalarMappings": fragment.get("scalarMappings", []),
                        "relationMappings": fragment.get("relationMappings", []),
                    }
                    for source, fragment in composed.fragments.items()
                },
            }
            _write_json(stage1_bundle / "ontology.json", ontology)
            _write_json(stage1_bundle / "grounding.json", grounding)
            template_errors = _validate_composed_template(composed)
            _write_json(stage1_bundle / "validation.json", {
                "status": "valid" if not template_errors else "invalid",
                "errors": template_errors,
                "templateIds": list(composed.templates),
            })
            _write_json(stage1_bundle / "review.json", {
                "verdict": "approve" if not template_errors else "revise",
                "issues": template_errors,
                "mode": "deterministic_template_contract_review",
            })
            _write_json(stage1_bundle / "manifest.json", {
                "generationMode": "composed_template", "templateIds": list(composed.templates),
                "sourceFingerprint": source_fingerprint, "modelCalls": 0,
            })
            if template_errors:
                raise ScopeV2TemplateError("composed template failed semantic validation: " + "; ".join(template_errors))

            dataset, projection = _materialize_dataset(composed, source_details, result_path)
            nquads = stage2_bundle / "graph.nq"
            ntriples = stage2_bundle / "graph.nt"
            rdfxml = stage2_bundle / "graph.rdf"
            dataset.serialize(destination=nquads, format="nquads")
            union = Graph()
            for graph in dataset.graphs():
                for triple in graph:
                    union.add(triple)
            union.serialize(destination=ntriples, format="nt")
            union.serialize(destination=rdfxml, format="xml")
            metrics = {
                "sourceCount": len(sources), "recordCount": item_count,
                "tripleCount": len(union), "quadCount": sum(len(graph) for graph in dataset.graphs()),
                "templateIds": list(composed.templates),
            }
            rdf_errors = _validate_materialized_dataset(dataset, composed, item_count)
            if not len(union):
                rdf_errors.append("empty RDF graph")
            validation = {
                "status": "valid" if not rdf_errors else "invalid",
                "errors": rdf_errors,
                "metrics": metrics,
            }
            _write_json(stage2_bundle / "validation.json", validation)
            _write_json(stage2_bundle / "manifest.json", {
                "status": "completed", "generationMode": "deterministic_scope_v2_template",
                "templateIds": list(composed.templates), **metrics,
            })
            _write_json(stage2_bundle / "review.json", {
                "verdict": "approve" if not rdf_errors else "revise",
                "issues": rdf_errors,
                "summary": "Deterministic fixed-template projection with semantic contract checks.",
            })
            _write_json(stage2_bundle / "metrics.json", metrics)
            _write_json(stage2_bundle / "projection_lineage.json", projection)
            if validation["status"] != "valid":
                raise ScopeV2TemplateError("fixed-template RDF graph is empty")
            return OntologyRunResult(
                status="completed", stage="rdf", stage1_run_id=run_id,
                stage2_run_id=run_id, stage1_bundle=str(stage1_bundle), stage2_bundle=str(stage2_bundle),
                nquads_path=str(nquads), rdfxml_path=str(rdfxml), ntriples_path=str(ntriples),
                manifest_path=str(stage2_bundle / "manifest.json"), validation_path=str(stage2_bundle / "validation.json"),
                details={"stage1Mode": "composed_template", "templateIds": list(composed.templates), "modelCalls": 0, "metrics": metrics},
            )
        except Exception as exc:
            return OntologyRunResult(
                status="failed", stage="stage1", error_code=AI_INDEX_MODELING_STAGE1_FAILED,
                error_message=str(exc), details={"stage1Mode": "composed_template"},
            )


def _materialize_dataset(
    template: ComposedScopeV2Template,
    source_details: dict[str, dict[str, Any]],
    result_path: Path,
) -> tuple[Dataset, dict[str, Any]]:
    dataset = Dataset()
    ns = Namespace(template.namespace)
    graphs = {
        "schema": dataset.graph(URIRef(template.namespace + "graph/schema")),
        "domain": dataset.graph(URIRef(template.namespace + "graph/domain")),
        "observation": dataset.graph(URIRef(template.namespace + "graph/observation")),
        "source": dataset.graph(URIRef(template.namespace + "graph/source")),
    }
    for identifier, definition in template.classes.items():
        subject = ns[identifier]
        graphs["schema"].add((subject, RDF.type, OWL.Class))
        graphs["schema"].add((subject, RDFS.label, Literal(definition.get("label") or identifier)))
        if definition.get("comment"):
            graphs["schema"].add((subject, RDFS.comment, Literal(definition["comment"])))
        parent = str(definition.get("subClassOf") or "")
        if parent in template.classes:
            graphs["schema"].add((subject, RDFS.subClassOf, ns[parent]))
    for collection, rdf_type in ((template.object_properties, OWL.ObjectProperty), (template.datatype_properties, OWL.DatatypeProperty)):
        for identifier, definition in collection.items():
            subject = ns[identifier]
            graphs["schema"].add((subject, RDF.type, rdf_type))
            if definition.get("domain") in template.classes:
                graphs["schema"].add((subject, RDFS.domain, ns[str(definition["domain"])]))
            value_range = str(definition.get("range") or "")
            if value_range in template.classes:
                graphs["schema"].add((subject, RDFS.range, ns[value_range]))
            elif value_range.startswith("xsd:"):
                graphs["schema"].add((subject, RDFS.range, URIRef(str(XSD) + value_range[4:])))

    projection_records: list[dict[str, Any]] = []
    for source, details in source_details.items():
        fragment = template.fragments[source]
        entity_spec = fragment["entity"]
        for rank, wrapped in enumerate(details.get("items", []), start=1):
            if not isinstance(wrapped, dict) or not isinstance(wrapped.get("data"), dict):
                continue
            record = wrapped["data"]
            source_id = str(wrapped.get("source_id") or _first(record, entity_spec.get("idPaths", [])) or _sha256_json(record))
            entity_iri = URIRef(template.namespace + "instance/entity/" + quote(source_id, safe="-._~:"))
            observation_iri = URIRef(template.namespace + "instance/observation/" + _sha256_json({"source": source, "id": source_id})[:24])
            raw_ref = str(wrapped.get("raw_ref") or "")
            raw_path = (result_path.parent / raw_ref).resolve() if raw_ref else result_path.resolve()
            relative_raw_path = raw_ref or result_path.name
            record_pointer = _record_json_pointer(raw_path, record)
            document_iri = URIRef(template.namespace + "instance/source-document/" + _sha256_json({"source": source, "path": relative_raw_path})[:24])
            record_iri = URIRef(template.namespace + "instance/source-record/" + _sha256_json({"raw": relative_raw_path, "pointer": record_pointer, "id": source_id})[:24])
            graphs["domain"].add((entity_iri, RDF.type, ns[str(entity_spec["classId"])]))
            graphs["observation"].add((observation_iri, RDF.type, ns[str(entity_spec["observationClassId"])]))
            graphs["observation"].add((observation_iri, ns.observesEntity, entity_iri))
            graphs["observation"].add((observation_iri, ns.observationFromRecord, record_iri))
            graphs["source"].add((record_iri, RDF.type, ns.SourceRecord))
            graphs["source"].add((document_iri, RDF.type, ns.SourceDocument))
            graphs["source"].add((record_iri, ns.recordFromDocument, document_iri))
            _literal(graphs["source"], document_iri, ns.sourceSystem, source)
            _literal(graphs["source"], document_iri, ns.sourcePath, relative_raw_path)
            _literal(graphs["source"], record_iri, ns.recordHash, _sha256_json(record))
            _literal(graphs["source"], record_iri, ns.rawRef, relative_raw_path)
            _literal(graphs["source"], record_iri, ns.recordJsonPointer, record_pointer)
            _literal(graphs["observation"], observation_iri, ns.resultRank, rank, XSD.positiveInteger)
            _literal(graphs["domain"], entity_iri, ns.sourceId, source_id)
            for key, property_id in (("title", "sourceTitle"), ("url", "sourceUrl"), ("published_at", "publishedAt")):
                value = wrapped.get(key)
                if value not in (None, ""):
                    datatype = template.datatype_properties.get(property_id, {}).get("range")
                    _literal(graphs["domain"], entity_iri, ns[property_id], value, _datatype(datatype))
            mapped: list[dict[str, Any]] = []
            for mapping in fragment.get("scalarMappings", []):
                values = _select(record, str(mapping.get("path") or ""))
                subject = observation_iri if mapping.get("subject") == "observation" else entity_iri
                target_graph = graphs["observation"] if mapping.get("subject") == "observation" else graphs["domain"]
                prop = str(mapping.get("propertyId") or "")
                if prop == "publishedAt" and wrapped.get("published_at") not in (None, ""):
                    continue
                datatype = template.datatype_properties.get(prop, {}).get("range")
                for value in _flatten(values):
                    if isinstance(value, (dict, list)) or value in (None, ""):
                        continue
                    _literal(target_graph, subject, ns[prop], value, _datatype(datatype))
                if values:
                    mapped.append({"path": mapping.get("path"), "propertyId": prop})
            for mapping in fragment.get("relationMappings", []):
                prop = str(mapping.get("propertyId") or "")
                target_class = str(mapping.get("targetClassId") or "Topic")
                for value in _flatten(_select(record, str(mapping.get("path") or ""))):
                    target_id, label = _relation_identity(value, mapping)
                    if not target_id:
                        continue
                    target_iri = URIRef(template.namespace + "instance/concept/" + quote(target_class, safe="") + "/" + _sha256_json(target_id)[:24])
                    graphs["domain"].add((target_iri, RDF.type, ns[target_class]))
                    graphs["domain"].add((entity_iri, ns[prop], target_iri))
                    label_prop = str(mapping.get("targetLabelPropertyId") or "")
                    if label_prop and label:
                        _literal(graphs["domain"], target_iri, ns[label_prop], label)
                    elif label:
                        graphs["domain"].add((target_iri, RDFS.label, Literal(label)))
                    if isinstance(value, dict):
                        for target_mapping in mapping.get("targetScalarMappings", []):
                            target_prop = str(target_mapping.get("propertyId") or "")
                            datatype = template.datatype_properties.get(target_prop, {}).get("range")
                            for target_value in _flatten(_select(value, str(target_mapping.get("path") or ""))):
                                if isinstance(target_value, (dict, list)) or target_value in (None, ""):
                                    continue
                                _literal(graphs["domain"], target_iri, ns[target_prop], target_value, _datatype(datatype))
                if _select(record, str(mapping.get("path") or "")):
                    mapped.append({"path": mapping.get("path"), "propertyId": prop})
            projection_records.append({"source": source, "sourceId": source_id, "rawRef": raw_ref, "mappings": mapped})
    return dataset, {"schemaVersion": "dataelf-scope-v2-projection-lineage.v1", "records": projection_records}


def _merge_definitions(target: dict[str, dict[str, Any]], values: Any, kind: str, template_id: str) -> None:
    if not isinstance(values, list):
        raise ScopeV2TemplateError(f"{template_id} {kind} definitions must be an array")
    for value in values:
        if not isinstance(value, dict) or not value.get("id"):
            raise ScopeV2TemplateError(f"{template_id} has an invalid {kind} definition")
        identifier = str(value["id"])
        existing = target.get(identifier)
        if existing is not None:
            if kind != "class":
                domains = existing.setdefault("_domains", [existing.get("domain")])
                if value.get("domain") not in domains:
                    domains.append(value.get("domain"))
            if kind == "class" and existing.get("kind") != value.get("kind"):
                raise ScopeV2TemplateError(f"conflicting {kind} definition {identifier!r} in {template_id}")
            if kind != "class" and existing.get("range") != value.get("range"):
                ranges = {str(existing.get("range")), str(value.get("range"))}
                if identifier == "publishedAt" and ranges <= {"xsd:string", "xsd:date", "xsd:dateTime"}:
                    existing["range"] = "xsd:dateTime"
                elif ranges <= {"xsd:string", "xsd:date", "xsd:dateTime"}:
                    existing["range"] = "xsd:string"
                else:
                    raise ScopeV2TemplateError(f"conflicting {kind} range {identifier!r} in {template_id}")
            # Source fragments commonly narrow a shared base property's domain
            # from DomainEntity/EntityObservation to one concrete subtype. Keep
            # the base definition so arbitrary source combinations remain valid.
            continue
        target[identifier] = (
            dict(value)
            if kind == "class"
            else {**value, "_domains": [value.get("domain")]}
        )


def _normalize_property_domains(
    properties: dict[str, dict[str, Any]],
    entity_classes: set[str],
    observation_classes: set[str],
) -> None:
    entity_domains = entity_classes | {"DomainEntity"}
    observation_domains = observation_classes | {"EntityObservation"}
    for definition in properties.values():
        domains = {str(value) for value in definition.pop("_domains", []) if value}
        if not domains:
            definition.pop("domain", None)
        elif domains <= entity_domains:
            definition["domain"] = "DomainEntity" if len(domains) > 1 else next(iter(domains))
        elif domains <= observation_domains:
            definition["domain"] = "EntityObservation" if len(domains) > 1 else next(iter(domains))
        elif len(domains) == 1:
            definition["domain"] = next(iter(domains))
        else:
            # An RDF property may intentionally be shared by unrelated concept
            # classes. Omitting rdfs:domain is safer than asserting a false
            # intersection type.
            definition.pop("domain", None)


def _validate_fragment_mappings(fragment: dict[str, Any]) -> None:
    properties = {
        str(item.get("id"))
        for key in ("objectProperties", "datatypeProperties")
        for item in fragment.get(key, [])
        if isinstance(item, dict)
    }
    for key in ("scalarMappings", "relationMappings"):
        values = fragment.get(key)
        if not isinstance(values, list):
            raise ScopeV2TemplateError(f"{fragment.get('templateId')} {key} must be an array")
        for mapping in values:
            if not isinstance(mapping, dict) or not str(mapping.get("path") or "").startswith("/"):
                raise ScopeV2TemplateError(f"{fragment.get('templateId')} contains an invalid {key} entry")
            if str(mapping.get("propertyId")) not in properties:
                raise ScopeV2TemplateError(f"{fragment.get('templateId')} mapping references undefined property {mapping.get('propertyId')!r}")
            for target_mapping in mapping.get("targetScalarMappings", []):
                if not isinstance(target_mapping, dict) or not str(target_mapping.get("path") or "").startswith("/"):
                    raise ScopeV2TemplateError(f"{fragment.get('templateId')} contains an invalid targetScalarMappings entry")
                if str(target_mapping.get("propertyId")) not in properties:
                    raise ScopeV2TemplateError(
                        f"{fragment.get('templateId')} target mapping references undefined property {target_mapping.get('propertyId')!r}"
                    )


def _validate_composed_template(template: ComposedScopeV2Template) -> list[str]:
    errors: list[str] = []
    required_base_domains = {
        "sourceId": "DomainEntity",
        "sourceTitle": "DomainEntity",
        "sourceUrl": "DomainEntity",
        "publishedAt": "DomainEntity",
        "rawRef": "SourceRecord",
        "recordJsonPointer": "SourceRecord",
        "resultRank": "EntityObservation",
        "sourcePath": "SourceDocument",
    }
    for property_id, expected_domain in required_base_domains.items():
        definition = template.datatype_properties.get(property_id)
        if not definition:
            errors.append(f"missing required base property {property_id}")
        elif definition.get("domain") != expected_domain:
            errors.append(f"{property_id} domain must be {expected_domain}, got {definition.get('domain')}")
    for source, fragment in template.fragments.items():
        entity_class = str(fragment.get("entity", {}).get("classId") or "")
        observation_class = str(fragment.get("entity", {}).get("observationClassId") or "")
        if template.classes.get(entity_class, {}).get("subClassOf") != "DomainEntity":
            errors.append(f"{source} entity class {entity_class} must inherit DomainEntity")
        if template.classes.get(observation_class, {}).get("subClassOf") != "EntityObservation":
            errors.append(f"{source} observation class {observation_class} must inherit EntityObservation")
    if "youtube" in template.fragments:
        fragment = template.fragments["youtube"]
        mapped_paths = {str(item.get("path")) for item in fragment.get("scalarMappings", [])}
        required_paths = {
            "/video_info/introduction",
            "/video_info/text",
            "/video_info/publisher",
            "/video_info/cover_url",
            "/blogger_info/fans_num",
        }
        missing = sorted(required_paths - mapped_paths)
        if missing:
            errors.append(f"youtube template misses real API paths: {missing}")
        obsolete = sorted(mapped_paths & {
            "/video_info/description", "/video_info/duration", "/blogger_info/follower_count"
        })
        if obsolete:
            errors.append(f"youtube template still uses obsolete API paths: {obsolete}")
    return errors


def _validate_materialized_dataset(
    dataset: Dataset,
    template: ComposedScopeV2Template,
    expected_records: int,
) -> list[str]:
    errors: list[str] = []
    quads = list(dataset.quads((None, None, None, None)))
    ns = Namespace(template.namespace)
    explicit_types: dict[Any, set[Any]] = {}
    parents: dict[Any, set[Any]] = {}
    domains: dict[Any, set[Any]] = {}
    ranges: dict[Any, set[Any]] = {}
    for subject, predicate, obj, _ in quads:
        if predicate == RDF.type:
            explicit_types.setdefault(subject, set()).add(obj)
        elif predicate == RDFS.subClassOf:
            parents.setdefault(subject, set()).add(obj)
        elif predicate == RDFS.domain:
            domains.setdefault(subject, set()).add(obj)
        elif predicate == RDFS.range:
            ranges.setdefault(subject, set()).add(obj)

    def is_a(actual: Any, expected: Any) -> bool:
        if actual == expected:
            return True
        pending = list(parents.get(actual, set()))
        visited: set[Any] = set()
        while pending:
            current = pending.pop()
            if current == expected:
                return True
            if current not in visited:
                visited.add(current)
                pending.extend(parents.get(current, set()))
        return False

    for subject, predicate, obj, graph in quads:
        if str(graph) == template.namespace + "graph/schema":
            continue
        for expected in domains.get(predicate, set()):
            if not any(is_a(actual, expected) for actual in explicit_types.get(subject, set())):
                errors.append(f"domain mismatch for {predicate}: {subject} is not {expected}")
        if isinstance(obj, URIRef):
            for expected in ranges.get(predicate, set()):
                if str(expected).startswith(str(XSD)):
                    continue
                if not any(is_a(actual, expected) for actual in explicit_types.get(obj, set())):
                    errors.append(f"range mismatch for {predicate}: {obj} is not {expected}")

    def objects_all(subject: Any, predicate: Any) -> list[Any]:
        return [obj for current, prop, obj, _ in quads if current == subject and prop == predicate]

    entity_subjects = {subject for subject, predicate, _, _ in quads if predicate == ns.sourceId}
    record_subjects = {
        subject for subject, predicate, obj, _ in quads if predicate == RDF.type and obj == ns.SourceRecord
    }
    observation_subjects = {
        subject
        for subject, values in explicit_types.items()
        if any(is_a(value, ns.EntityObservation) for value in values)
    }
    for label, actual in (
        ("entity", len(entity_subjects)),
        ("SourceRecord", len(record_subjects)),
        ("EntityObservation", len(observation_subjects)),
    ):
        if actual != expected_records:
            errors.append(f"{label} count {actual} does not match source record count {expected_records}")
    for record in record_subjects:
        pointers = [str(value) for value in objects_all(record, ns.recordJsonPointer)]
        refs = [str(value) for value in objects_all(record, ns.rawRef)]
        if len(pointers) != 1 or not pointers[0].startswith("/data/list/"):
            errors.append(f"SourceRecord {record} lacks an exact /data/list/N JSON pointer")
        if len(refs) != 1 or Path(refs[0]).is_absolute():
            errors.append(f"SourceRecord {record} rawRef must be one relative path")
    documents = {
        subject for subject, predicate, obj, _ in quads if predicate == RDF.type and obj == ns.SourceDocument
    }
    for document in documents:
        paths = [str(value) for value in objects_all(document, ns.sourcePath)]
        if len(paths) != 1 or Path(paths[0]).is_absolute():
            errors.append(f"SourceDocument {document} sourcePath must be one relative path")
    for entity in entity_subjects:
        published = objects_all(entity, ns.publishedAt)
        if len(published) > 1:
            errors.append(f"entity {entity} has duplicate publishedAt values")
    return sorted(set(errors))


def _scope_result_path(workspace: Path) -> Path:
    paths = sorted((workspace / "scope_v2").glob("*/result.json"))
    if len(paths) != 1:
        raise ScopeV2TemplateError(f"expected exactly one Scope V2 result, found {len(paths)}")
    return paths[0].resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScopeV2TemplateError(f"cannot read template/runtime JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScopeV2TemplateError(f"JSON object required: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _first(value: Any, paths: Iterable[str]) -> Any:
    for path in paths:
        selected = _select(value, str(path))
        if selected and selected[0] not in (None, ""):
            return selected[0]
    return None


def _select(value: Any, path: str) -> list[Any]:
    if not path.startswith("/"):
        return []
    states = [value]
    for raw_token in path[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        next_states: list[Any] = []
        for current in states:
            if token == "*" and isinstance(current, list):
                next_states.extend(current)
            elif isinstance(current, dict) and token in current:
                next_states.append(current[token])
            elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
                next_states.append(current[int(token)])
        states = next_states
    return states


def _flatten(values: Iterable[Any]) -> Iterable[Any]:
    for value in values:
        if isinstance(value, list):
            yield from _flatten(value)
        else:
            yield value


def _relation_identity(value: Any, mapping: dict[str, Any]) -> tuple[str, str]:
    if isinstance(value, dict):
        id_path = str(mapping.get("targetIdPath") or "")
        label_path = str(mapping.get("targetLabelPath") or "")
        identity = _first(value, [id_path]) if id_path else None
        label = _first(value, [label_path]) if label_path else None
        identity = identity or label or _sha256_json(value)
        return str(identity), str(label or "")
    if value in (None, ""):
        return "", ""
    return str(value), str(value)


def _record_json_pointer(raw_path: Path, record: dict[str, Any]) -> str:
    try:
        payload = _read_json(raw_path)
    except ScopeV2TemplateError:
        return ""
    data = payload.get("data")
    values = data.get("list") if isinstance(data, dict) else None
    if not isinstance(values, list):
        return ""
    for index, candidate in enumerate(values):
        if candidate == record:
            return f"/data/list/{index}"
    return ""


def _datatype(value: Any) -> URIRef | None:
    if not isinstance(value, str) or not value.startswith("xsd:"):
        return None
    return URIRef(str(XSD) + value[4:])


def _literal(graph: Graph, subject: URIRef | BNode, predicate: URIRef, value: Any, datatype: URIRef | None = None) -> None:
    if value in (None, ""):
        return
    try:
        graph.add((subject, predicate, Literal(value, datatype=datatype)))
    except (TypeError, ValueError):
        graph.add((subject, predicate, Literal(str(value))))


__all__ = [
    "ComposedScopeV2Template", "PrefetchedScopeV2Collector", "ScopeV2TemplateError",
    "ScopeV2TemplateOntologyRunner", "SOURCE_ENDPOINTS", "compose_scope_v2_templates",
]
