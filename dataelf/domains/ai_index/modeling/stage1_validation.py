from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import sha256_json
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.contracts import (
    CLASS_ID,
    COLUMN_ROLES,
    PROPERTY_ID,
    TABLE_ROLES,
    VALIDATOR_VERSION,
    XSD_RANGES,
    schema_errors,
)
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.source import evidence_result


_FORBIDDEN_10K = (
    "10-k",
    "10k",
    "financialfact",
    "companyfactcollection",
    "xbrl",
    "sec.gov",
    "accessionnumber",
    "factsourcedfromfiling",
)

_RAW_PATH_CLASSES = frozenset(
    {"semantic_promoted", "observation_promoted", "derived_with_formula", "redundant_but_source_linked", "source_only"}
)
_REQUIRED_SOURCE_CLASSES = frozenset(
    {
        "DomainEntity", "SourceDocument", "SearchResponse", "SourceRecord", "SourceFragment",
        "EntityObservation", "PaperObservation", "ScholarObservation", "InstitutionObservation",
    }
)
_REQUIRED_DOMAIN_CLASSES = frozenset({"Paper", "Scholar", "Institution", "Topic", "Venue", "Award", "NewsItem", "Authorship"})
_FORBIDDEN_PROPERTY_IDS = frozenset(
    {
        "citedbycount", "institutionauthorcount", "iscorrespondingauthor",
        "paperinstitutionisprimary", "scholarinstitutionisprimary",
    }
)


@dataclass
class ValidationReport:
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def error(self, code: str, path: str, message: str, evidence_refs: Iterable[str] = ()) -> None:
        self.errors.append(
            {"code": code, "path": path, "message": message, "evidenceRefs": list(evidence_refs)}
        )

    def warning(self, code: str, path: str, message: str, evidence_refs: Iterable[str] = ()) -> None:
        self.warnings.append(
            {"code": code, "path": path, "message": message, "evidenceRefs": list(evidence_refs)}
        )


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _array(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _refs(value: Any) -> list[str]:
    return [item for item in _array(value) if isinstance(item, str)]


def _all_evidence_refs(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"evidenceRef", "identityEvidenceRef", "joinEvidenceRef"} and isinstance(child, str):
                result.add(child)
            elif key.endswith("EvidenceRefs") and isinstance(child, list):
                result.update(item for item in child if isinstance(item, str))
            result.update(_all_evidence_refs(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_all_evidence_refs(child))
    return result


def _validate_schema(ontology: Any, grounding: Any, report: ValidationReport) -> None:
    for name, value, path in (
        ("ontology.schema.json", ontology, "/ontology"),
        ("grounding.schema.json", grounding, "/grounding"),
    ):
        for message in schema_errors(value, name):
            report.error("json_schema", path, message)


def _validate_ontology(
    ontology: dict[str, Any], grounding: dict[str, Any], evidence: dict[str, Any], config: Stage1Config, report: ValidationReport
) -> None:
    metadata = _object(ontology.get("metadata"))
    if metadata.get("id") != config.ontology.ontology_id:
        report.error("ontology_id", "/ontology/metadata/id", "ontology id differs from configuration")
    if metadata.get("namespace") != config.ontology.namespace:
        report.error("namespace", "/ontology/metadata/namespace", "namespace differs from configuration")
    if metadata.get("sourceFingerprint") != evidence.get("sourceFingerprint"):
        report.error("source_fingerprint", "/ontology/metadata/sourceFingerprint", "source fingerprint differs from evidence")
    if grounding.get("sourceFingerprint") != evidence.get("sourceFingerprint"):
        report.error("source_fingerprint", "/grounding/sourceFingerprint", "source fingerprint differs from evidence")
    classes = _object(ontology.get("classes"))
    object_properties = _object(ontology.get("objectProperties"))
    datatype_properties = _object(ontology.get("datatypeProperties"))
    seen_uris: set[str] = set()
    for identifier, element in classes.items():
        path = f"/ontology/classes/{identifier}"
        if not CLASS_ID.fullmatch(identifier):
            report.error("class_id", path, "class id must be UpperCamelCase")
        uri = _object(element).get("uri")
        if not isinstance(uri, str) or not uri.startswith(config.ontology.namespace):
            report.error("uri_namespace", f"{path}/uri", "class URI must use configured namespace")
        if isinstance(uri, str) and uri in seen_uris:
            report.error("duplicate_uri", f"{path}/uri", "URI is duplicated")
        if isinstance(uri, str):
            seen_uris.add(uri)
        for parent in _array(_object(element).get("subClassOf")):
            if not isinstance(parent, str) or parent not in classes:
                report.error("class_reference", f"{path}/subClassOf", f"unknown parent class {parent}")
    for section, elements, datatype in (
        ("objectProperties", object_properties, False),
        ("datatypeProperties", datatype_properties, True),
    ):
        for identifier, element in elements.items():
            path = f"/ontology/{section}/{identifier}"
            item = _object(element)
            if not PROPERTY_ID.fullmatch(identifier):
                report.error("property_id", path, "property id must be lowerCamelCase")
            uri = item.get("uri")
            if not isinstance(uri, str) or not uri.startswith(config.ontology.namespace):
                report.error("uri_namespace", f"{path}/uri", "property URI must use configured namespace")
            if isinstance(uri, str) and uri in seen_uris:
                report.error("duplicate_uri", f"{path}/uri", "URI is duplicated")
            if isinstance(uri, str):
                seen_uris.add(uri)
            domain = item.get("domain")
            range_id = item.get("range")
            if not isinstance(domain, str) or domain not in classes:
                report.error("domain_reference", f"{path}/domain", "property domain is not a declared class")
            if datatype:
                if not isinstance(range_id, str) or range_id not in XSD_RANGES:
                    report.error("datatype_range", f"{path}/range", "datatype range is not supported")
            elif not isinstance(range_id, str) or range_id not in classes:
                report.error("range_reference", f"{path}/range", "object property range is not a declared class")
            if not datatype:
                inverse_id = item.get("inverseOf")
                if inverse_id is not None:
                    inverse = _object(object_properties.get(inverse_id)) if isinstance(inverse_id, str) else {}
                    if not isinstance(inverse_id, str) or not inverse:
                        report.error("inverse_property_reference", f"{path}/inverseOf", f"unknown inverse property {inverse_id}")
                    elif inverse.get("domain") != item.get("range") or inverse.get("range") != item.get("domain"):
                        report.error("inverse_property_shape", f"{path}/inverseOf", "inverse property must reverse domain and range")
                    elif inverse.get("inverseOf") != identifier:
                        report.error("inverse_property_reciprocal", f"{path}/inverseOf", "inverse property declaration must be reciprocal")
                for parent_id in _array(item.get("subPropertyOf")):
                    parent = _object(object_properties.get(parent_id)) if isinstance(parent_id, str) else {}
                    if not isinstance(parent_id, str) or not parent:
                        report.error("subproperty_reference", f"{path}/subPropertyOf", f"unknown super-property {parent_id}")
                    elif parent_id == identifier:
                        report.error("subproperty_cycle", f"{path}/subPropertyOf", "property cannot be its own super-property")
    serialized = json.dumps({"ontology": ontology, "grounding": grounding}, ensure_ascii=False).lower()
    for forbidden in _FORBIDDEN_10K:
        if forbidden in serialized:
            report.error("forbidden_10k_residue", "/", f"forbidden ERBuilding 10-K residue found: {forbidden}")


def _validate_source_coverage(
    ontology: dict[str, Any], grounding: dict[str, Any], evidence: dict[str, Any], config: Stage1Config, report: ValidationReport
) -> None:
    classes = _object(ontology.get("classes"))
    objects = _object(ontology.get("objectProperties"))
    datatypes = _object(ontology.get("datatypeProperties"))
    catalog_tables = {item["name"]: item for item in evidence["catalog"]["tables"]}
    expected_columns = {
        f"{table['name']}.{column}" for table in catalog_tables.values() for column in table["columns"]
    }
    table_classifications = _object(grounding.get("tableClassifications"))
    column_classifications = _object(grounding.get("columnClassifications"))
    if set(table_classifications) != set(catalog_tables):
        for missing in sorted(set(catalog_tables) - set(table_classifications)):
            report.error("missing_table_classification", f"/grounding/tableClassifications/{missing}", "source table is not classified")
        for invented in sorted(set(table_classifications) - set(catalog_tables)):
            report.error("invented_table", f"/grounding/tableClassifications/{invented}", "classification references a nonexistent table")
    if set(column_classifications) != expected_columns:
        for missing in sorted(expected_columns - set(column_classifications)):
            report.error("missing_column_classification", f"/grounding/columnClassifications/{missing}", "source column is not classified")
        for invented in sorted(set(column_classifications) - expected_columns):
            report.error("invented_column", f"/grounding/columnClassifications/{invented}", "classification references a nonexistent column")
    for table, raw in table_classifications.items():
        item = _object(raw)
        role = item.get("role")
        if role not in TABLE_ROLES:
            report.error("table_role", f"/grounding/tableClassifications/{table}/role", f"unsupported table role {role}")
        class_ids = _array(item.get("classIds"))
        for class_id in class_ids:
            if class_id not in classes:
                report.error("table_class_reference", f"/grounding/tableClassifications/{table}/classIds", f"unknown class {class_id}")
        catalog = catalog_tables.get(table)
        if catalog and catalog["rowCount"] > 0 and role == "ignored" and not str(item.get("reason", "")).strip():
            report.error("nonempty_ignored", f"/grounding/tableClassifications/{table}", "nonempty ignored table needs a reason")
    for coordinate, raw in column_classifications.items():
        item = _object(raw)
        role = item.get("role")
        if role not in COLUMN_ROLES:
            report.error("column_role", f"/grounding/columnClassifications/{coordinate}/role", f"unsupported column role {role}")
        property_ids = _array(item.get("propertyIds"))
        for property_id in property_ids:
            if property_id not in objects and property_id not in datatypes:
                report.error("column_property_reference", f"/grounding/columnClassifications/{coordinate}/propertyIds", f"unknown property {property_id}")
        if role == "ignored" and not str(item.get("reason", "")).strip():
            report.error("ignored_without_reason", f"/grounding/columnClassifications/{coordinate}", "ignored column needs an explicit reason")
        if role == "ignored" and property_ids:
            report.error(
                "ignored_column_mapped",
                f"/grounding/columnClassifications/{coordinate}/propertyIds",
                "ignored column cannot also map ontology properties",
            )
    source_coverage = _object(grounding.get("sourceCoverage"))
    expected = {
        "tableCount": len(catalog_tables),
        "nonEmptyTableCount": sum(item["rowCount"] > 0 for item in catalog_tables.values()),
        "columnCount": len(expected_columns),
        "totalRowCount": evidence["catalog"]["totalRowCount"],
    }
    for key, value in expected.items():
        if source_coverage.get(key) != value:
            report.error("coverage_count", f"/grounding/sourceCoverage/{key}", f"expected {value}")


def _validate_evidence(
    ontology: dict[str, Any], grounding: dict[str, Any], evidence: dict[str, Any], report: ValidationReport
) -> None:
    records = _object(evidence.get("evidence"))
    for ref in sorted(_all_evidence_refs(grounding)):
        if ref not in records:
            report.error("unknown_evidence_ref", "/grounding", f"unknown evidence reference {ref}")
    classes = _object(ontology.get("classes"))
    objects = _object(ontology.get("objectProperties"))
    datatypes = _object(ontology.get("datatypeProperties"))
    class_evidence = _object(grounding.get("classEvidence"))
    object_evidence = _object(grounding.get("objectPropertyEvidence"))
    datatype_evidence = _object(grounding.get("datatypePropertyEvidence"))
    catalog = {item["name"]: item for item in evidence["catalog"]["tables"]}
    for unexpected in sorted(set(class_evidence) - set(classes)):
        report.error("invented_class_evidence", f"/grounding/classEvidence/{unexpected}", "evidence references an undeclared class")
    for unexpected in sorted(set(object_evidence) - set(objects)):
        report.error("invented_object_evidence", f"/grounding/objectPropertyEvidence/{unexpected}", "evidence references an undeclared object property")
    for unexpected in sorted(set(datatype_evidence) - set(datatypes)):
        report.error("invented_datatype_evidence", f"/grounding/datatypePropertyEvidence/{unexpected}", "evidence references an undeclared datatype property")
    for class_id in classes:
        raw_entries = class_evidence.get(class_id)
        entries = _array(raw_entries)
        if not entries:
            message = "class evidence must be a non-empty JSON array; even one entry must use [{...}], never {...}"
            if raw_entries is None:
                message = "class has no source evidence; add a non-empty JSON array of evidence entries"
            report.error("missing_class_evidence", f"/grounding/classEvidence/{class_id}", message)
        for position, raw in enumerate(entries):
            entry = _object(raw)
            if entry.get("sourceKind") == "source_index":
                path = f"/grounding/classEvidence/{class_id}/{position}"
                if not str(entry.get("indexPath", "")).strip() or not str(entry.get("formula", "")).strip():
                    report.error("class_source_index_binding", path, "source-index class evidence needs an indexPath and semantic description")
                if not _refs(entry.get("evidenceRefs")):
                    report.error("class_evidence_refs", path, "source-index class evidence needs evidence references")
                continue
            if entry.get("sourceKind") == "raw_json":
                if not str(entry.get("endpoint", "")).strip() or not str(entry.get("pathPattern", "")).strip():
                    report.error("class_raw_binding", f"/grounding/classEvidence/{class_id}/{position}", "raw class evidence needs endpoint and pathPattern")
                if not _refs(entry.get("evidenceRefs")):
                    report.error("class_evidence_refs", f"/grounding/classEvidence/{class_id}/{position}", "raw class evidence needs evidence references")
                continue
            table_name = entry.get("table")
            if table_name not in catalog:
                report.error("class_evidence_table", f"/grounding/classEvidence/{class_id}/{position}/table", "class evidence references a nonexistent table")
            if not _refs(entry.get("evidenceRefs")) and not isinstance(entry.get("identityEvidenceRef"), str):
                report.error("class_evidence_refs", f"/grounding/classEvidence/{class_id}/{position}", "class evidence needs source evidence references")
            ref = entry.get("identityEvidenceRef")
            columns = entry.get("identityColumns")
            if ref:
                record = records.get(ref)
                result = _object(_object(record).get("result"))
                if _object(record).get("type") != "identity_profile":
                    report.error("identity_evidence_type", f"/grounding/classEvidence/{class_id}/{position}", "identityEvidenceRef is not an identity profile")
                if result.get("table") != entry.get("table") or result.get("columns") != columns:
                    report.error("identity_evidence_mismatch", f"/grounding/classEvidence/{class_id}/{position}", "identity declaration differs from evidence", [str(ref)])
                semantics = entry.get("identitySemantics")
                if result.get("isRowUnique") is False and _object(classes.get(class_id)).get("kind") == "entity":
                    if semantics != "entity_merge_key":
                        report.error("duplicate_identity_semantics", f"/grounding/classEvidence/{class_id}/{position}/identitySemantics", "duplicate business ID must be an entity_merge_key", [str(ref)])
                    mapping = _object(grounding.get("entityObservationMappings")).get(class_id)
                    if not isinstance(mapping, dict):
                        report.error("missing_observation_mapping", f"/grounding/entityObservationMappings/{class_id}", "duplicate entity rows require entity plus observation mapping", [str(ref)])
            elif entry.get("identitySemantics") not in {"none", "conceptual"}:
                report.error("missing_identity_evidence", f"/grounding/classEvidence/{class_id}/{position}", "identity declaration needs identity evidence")
    for property_id in objects:
        entries = _array(object_evidence.get(property_id))
        if not entries:
            report.error("missing_object_evidence", f"/grounding/objectPropertyEvidence/{property_id}", "object property has no source evidence")
        for position, raw in enumerate(entries):
            entry = _object(raw)
            if entry.get("sourceKind") == "source_index" and not str(entry.get("indexPath", "")).strip():
                report.error(
                    "object_source_index_binding",
                    f"/grounding/objectPropertyEvidence/{property_id}/{position}",
                    "source-index object evidence needs an indexPath",
                )
            if not _refs(entry.get("evidenceRefs")) and not isinstance(entry.get("joinEvidenceRef"), str):
                report.error("object_evidence_refs", f"/grounding/objectPropertyEvidence/{property_id}/{position}", "object property evidence needs source evidence references")
            join_ref = entry.get("joinEvidenceRef")
            if join_ref:
                record = _object(records.get(join_ref))
                result = _object(record.get("result"))
                if record.get("type") != "join_profile" or result.get("valid") is not True:
                    report.error("invalid_join_evidence", f"/grounding/objectPropertyEvidence/{property_id}/{position}", "object property join evidence is not a valid join profile", [str(join_ref)])
                coordinates = (
                    entry.get("relationTable"), entry.get("sourceColumns"), entry.get("targetTable"), entry.get("targetColumns")
                )
                observed = (
                    result.get("sourceTable"), result.get("sourceColumns"), result.get("targetTable"), result.get("targetColumns")
                )
                if coordinates != observed:
                    report.error("join_evidence_mismatch", f"/grounding/objectPropertyEvidence/{property_id}/{position}", "declared join differs from profiled join", [str(join_ref)])
            elif entry.get("evidenceKind") not in {
                "row_link", "json_value", "schema_only", "raw_array_relation",
                "source_navigation", "observation_link", "reified_association",
            }:
                report.error("missing_join_evidence", f"/grounding/objectPropertyEvidence/{property_id}/{position}", "object property needs join evidence or an explicit non-join evidenceKind")
    for property_id in datatypes:
        entries = _array(datatype_evidence.get(property_id))
        if not entries:
            report.error("missing_datatype_evidence", f"/grounding/datatypePropertyEvidence/{property_id}", "datatype property has no source evidence")
        for position, raw in enumerate(entries):
            entry = _object(raw)
            if entry.get("sourceKind") == "source_index":
                path = f"/grounding/datatypePropertyEvidence/{property_id}/{position}"
                if not str(entry.get("indexPath", "")).strip() or not str(entry.get("formula", "")).strip():
                    report.error("datatype_source_index_binding", path, "source-index datatype evidence needs an indexPath and derivation description")
                if not _refs(entry.get("evidenceRefs")):
                    report.error("datatype_evidence_refs", path, "source-index datatype evidence needs evidence references")
                continue
            if entry.get("sourceKind") == "raw_json":
                path = f"/grounding/datatypePropertyEvidence/{property_id}/{position}"
                if not str(entry.get("endpoint", "")).strip() or not str(entry.get("pathPattern", "")).strip():
                    report.error("datatype_raw_binding", path, "raw datatype evidence needs endpoint and pathPattern")
                if not _refs(entry.get("evidenceRefs")):
                    report.error("datatype_evidence_refs", path, "raw datatype evidence needs evidence references")
                continue
            table_name = entry.get("table")
            column = entry.get("column")
            path = f"/grounding/datatypePropertyEvidence/{property_id}/{position}"
            if table_name not in catalog or column not in _object(catalog.get(table_name)).get("columns", []):
                report.error("datatype_evidence_coordinate", path, "datatype property evidence references a nonexistent table or column")
            if not _refs(entry.get("evidenceRefs")):
                report.error("datatype_evidence_refs", path, "datatype property evidence needs evidence references")
            classification = _object(grounding.get("columnClassifications")).get(f"{table_name}.{column}")
            if not isinstance(classification, dict) or property_id not in _array(classification.get("propertyIds")):
                report.error("datatype_column_mapping", path, "datatype property is not mapped by its cited source column")
            elif classification.get("role") not in {"foreign_key", "identity", "entity_merge_key"}:
                domain = _object(datatypes.get(property_id)).get("domain")
                table_classes = _array(_object(_object(grounding.get("tableClassifications")).get(table_name)).get("classIds"))
                if domain not in table_classes:
                    report.error(
                        "datatype_domain_table_mismatch",
                        path,
                        f"datatype property domain {domain} is incompatible with classes mapped from table {table_name}",
                    )


def _validate_observations(ontology: dict[str, Any], grounding: dict[str, Any], evidence: dict[str, Any], report: ValidationReport) -> None:
    classes = _object(ontology.get("classes"))
    objects = _object(ontology.get("objectProperties"))
    datatypes = _object(ontology.get("datatypeProperties"))
    catalog = {item["name"]: item for item in evidence["catalog"]["tables"]}
    for class_id, raw in _object(grounding.get("entityObservationMappings")).items():
        path = f"/grounding/entityObservationMappings/{class_id}"
        item = _object(raw)
        if class_id not in classes:
            report.error("observation_entity", path, "observation mapping references an unknown entity class")
        elif _object(classes.get(class_id)).get("kind") != "entity":
            report.error("observation_entity_kind", path, "observation mapping key must be an entity class")
        observation_class = item.get("observationClassId")
        if observation_class not in classes or _object(classes.get(observation_class)).get("kind") != "observation":
            report.error("observation_class", f"{path}/observationClassId", "observationClassId must reference an observation class")
        observed_property_id = item.get("observedEntityPropertyId")
        observed_property = _object(objects.get(observed_property_id))
        if observed_property_id not in objects:
            report.error("observation_link", f"{path}/observedEntityPropertyId", "observedEntityPropertyId must reference an object property")
        elif observed_property.get("domain") != observation_class or observed_property.get("range") != class_id:
            report.error(
                "observation_link_shape",
                f"{path}/observedEntityPropertyId",
                f"object property must have domain {observation_class} and range {class_id}",
            )
        source_raw_property_id = item.get("sourceRawPropertyId")
        source_raw_property = _object(datatypes.get(source_raw_property_id))
        if source_raw_property_id not in datatypes:
            report.error("source_raw_property", f"{path}/sourceRawPropertyId", "sourceRawPropertyId must reference a datatype property")
        elif source_raw_property.get("domain") != observation_class or source_raw_property.get("range") != "xsd:string":
            report.error(
                "source_raw_property_shape",
                f"{path}/sourceRawPropertyId",
                f"source_raw property must have domain {observation_class} and range xsd:string",
            )
        table = catalog.get(str(item.get("entityTable")))
        if not table:
            report.error("observation_table", f"{path}/entityTable", "entity table does not exist")
        elif item.get("sourceRawColumn") != "source_raw" or "source_raw" not in table["columns"]:
            report.error("source_raw_column", f"{path}/sourceRawColumn", "mapping must preserve the source_raw column")
        else:
            classification = _object(grounding.get("columnClassifications")).get(f"{item.get('entityTable')}.source_raw")
            if not isinstance(classification, dict) or source_raw_property_id not in _array(classification.get("propertyIds")):
                report.error("source_raw_column_mapping", f"{path}/sourceRawPropertyId", "source_raw column classification must map the observation source_raw datatype property")
        locator_fields = set(_array(item.get("rowLocatorFields")))
        required = (
            {"documentId", "jsonPointer", "recordHash"}
            if evidence.get("sourceType") == "ai_index_raw"
            else {"relativeFile", "dataRowNumber", "canonicalRowHash"}
        )
        if locator_fields != required:
            report.error("row_locator", f"{path}/rowLocatorFields", f"row locator must contain exactly {sorted(required)}")


def _validate_hints_and_cqs(
    ontology: dict[str, Any], grounding: dict[str, Any], evidence: dict[str, Any], config: Stage1Config, report: ValidationReport
) -> None:
    domain = evidence_result(evidence, evidence["domainHintsEvidenceRef"])
    expected_hints = {f"entity:{name}" for name in domain["entities"]} | {
        f"relation:{name}" for name in domain["relations"]
    }
    resolutions = _object(grounding.get("domainHintResolutions"))
    if set(resolutions) != expected_hints:
        for missing in sorted(expected_hints - set(resolutions)):
            report.error("missing_hint_resolution", f"/grounding/domainHintResolutions/{missing}", "domain hint is unresolved")
        for invented in sorted(set(resolutions) - expected_hints):
            report.error("invented_hint_resolution", f"/grounding/domainHintResolutions/{invented}", "resolution has no declared hint")
    all_elements = set(_object(ontology.get("classes"))) | set(_object(ontology.get("objectProperties"))) | set(_object(ontology.get("datatypeProperties")))
    records = _object(evidence.get("evidence"))
    for name, raw in resolutions.items():
        item = _object(raw)
        status = item.get("status")
        refs = _refs(item.get("evidenceRefs"))
        if status == "implemented":
            ids = _array(item.get("ontologyElementIds"))
            if not ids or any(identifier not in all_elements for identifier in ids):
                report.error("hint_implementation", f"/grounding/domainHintResolutions/{name}", "implemented hint must reference existing ontology elements")
            if name.startswith("relation:"):
                relation = domain["relations"].get(name.removeprefix("relation:"), {})
                required_refs = {relation.get("sourceJoinEvidenceRef"), relation.get("targetJoinEvidenceRef")} - {None}
                if not required_refs.issubset(set(refs)):
                    report.error("hint_join_evidence", f"/grounding/domainHintResolutions/{name}/evidenceRefs", "implemented relation needs every available declared source join profile evidence", required_refs)
                if not any(identifier in _object(ontology.get("objectProperties")) for identifier in ids):
                    report.error("hint_relation_property", f"/grounding/domainHintResolutions/{name}/ontologyElementIds", "implemented relation must reference an object property")
            elif name.startswith("entity:"):
                entity_name = name.removeprefix("entity:")
                hint = domain["entities"].get(entity_name, {})
                class_ids = [identifier for identifier in ids if identifier in _object(ontology.get("classes"))]
                if not class_ids:
                    report.error("hint_entity_class", f"/grounding/domainHintResolutions/{name}/ontologyElementIds", "implemented entity must reference a class")
                expected_identity_ref = hint.get("identityEvidenceRef")
                matched_identity = False
                for class_id in class_ids:
                    for entry in _array(_object(grounding.get("classEvidence")).get(class_id)):
                        item_entry = _object(entry)
                        if (
                            item_entry.get("table") == hint.get("table")
                            and item_entry.get("identityColumns") == [hint.get("idField")]
                            and item_entry.get("identityEvidenceRef") == expected_identity_ref
                        ):
                            matched_identity = True
                if not matched_identity:
                    report.error("hint_entity_identity", f"/grounding/domainHintResolutions/{name}", "implemented entity must use the declared table/id identity profile", [str(expected_identity_ref)] if expected_identity_ref else [])
        elif status == "omitted":
            if not str(item.get("reason", "")).strip() or not refs:
                report.error("hint_omission", f"/grounding/domainHintResolutions/{name}", "omission needs a reason and evidence")
        else:
            report.error("hint_status", f"/grounding/domainHintResolutions/{name}/status", "status must be implemented or omitted")
        for ref in refs:
            if ref not in records:
                report.error("unknown_evidence_ref", f"/grounding/domainHintResolutions/{name}", f"unknown evidence reference {ref}")
    questions = _array(grounding.get("competencyQuestions"))
    expected_questions = list(config.ontology.competency_questions)
    if len(questions) != len(expected_questions):
        report.error("cq_count", "/grounding/competencyQuestions", f"expected {len(expected_questions)} configured questions")
    question_ids: list[str] = []
    for index, expected in enumerate(expected_questions, start=1):
        match = next((item for item in questions if isinstance(item, dict) and item.get("question") == expected), None)
        if not match:
            report.error("missing_cq", "/grounding/competencyQuestions", f"configured question is missing: {expected}")
        elif not isinstance(match.get("id"), str):
            report.error("cq_id", "/grounding/competencyQuestions", "question id is missing")
        else:
            question_ids.append(match["id"])
    coverage = _object(grounding.get("cqCoverage"))
    if set(coverage) != set(question_ids):
        report.error("cq_coverage_keys", "/grounding/cqCoverage", "CQ coverage keys must exactly match configured question ids")
    for question_id, raw in coverage.items():
        item = _object(raw)
        if item.get("status") not in {"covered", "partial", "not_covered"}:
            report.error("cq_status", f"/grounding/cqCoverage/{question_id}/status", "unsupported CQ coverage status")
        if not _array(item.get("ontologyElementIds")) or not _refs(item.get("evidenceRefs")):
            report.error("cq_support", f"/grounding/cqCoverage/{question_id}", "CQ coverage needs ontology elements and evidence")
        for identifier in _array(item.get("ontologyElementIds")):
            if identifier not in all_elements:
                report.error("cq_element_reference", f"/grounding/cqCoverage/{question_id}/ontologyElementIds", f"unknown ontology element {identifier}")
        if item.get("status") == "partial":
            report.warning("cq_partial", f"/grounding/cqCoverage/{question_id}", "competency question is only partially covered")


def _has_object_shape(objects: dict[str, Any], domain: str, range_id: str) -> bool:
    return any(
        _object(item).get("domain") == domain and _object(item).get("range") == range_id
        for item in objects.values()
    )


def _binding_patterns(binding: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for item in _array(binding.get("rawPathPatterns")):
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict):
            result.add(f"{item.get('endpoint', '')}|{item.get('pathPattern', '')}")
    return result


def _validate_v2_semantics(
    ontology: dict[str, Any], grounding: dict[str, Any], evidence: dict[str, Any], report: ValidationReport
) -> None:
    if evidence.get("sourceType") != "ai_index_raw":
        return
    if ontology.get("schemaVersion") != "dataelf-ontology.v2" or grounding.get("schemaVersion") != "dataelf-grounding.v2":
        report.error("v2_schema_version", "/", "raw-backed Stage 1 requires ontology.v2 and grounding.v2")
    if evidence.get("formatVersion") != 2:
        report.error("evidence_version", "/evidence/formatVersion", "Stage 1 requires evidence formatVersion 2")
    if _array(evidence.get("sourceReplayErrors")):
        report.error("source_replay", "/evidence/sourceReplayErrors", "raw source replay has errors")
    source_index = _object(evidence.get("sourceIndex"))
    lineage = _object(evidence.get("normalizationLineage"))
    source_ref = evidence.get("sourceIndexEvidenceRef")
    lineage_ref = evidence.get("normalizationLineageEvidenceRef")
    if not source_index or not lineage or not isinstance(source_ref, str) or not isinstance(lineage_ref, str):
        report.error("missing_v2_contract", "/evidence", "source index and normalization lineage are required")
        return
    for schema_name, value, path in (
        ("source_index.schema.json", source_index, "/evidence/sourceIndex"),
        ("normalization_lineage.schema.json", lineage, "/evidence/normalizationLineage"),
    ):
        for message in schema_errors(value, schema_name):
            report.error("json_schema", path, message)
    if source_index.get("sourceIndexSha256") != sha256_json(
        {key: value for key, value in source_index.items() if key != "sourceIndexSha256"}
    ):
        report.error("source_index_hash", "/evidence/sourceIndex/sourceIndexSha256", "source index canonical hash mismatch")
    if lineage.get("lineageSha256") != sha256_json(
        {key: value for key, value in lineage.items() if key != "lineageSha256"}
    ):
        report.error("lineage_hash", "/evidence/normalizationLineage/lineageSha256", "normalization lineage canonical hash mismatch")
    if lineage.get("sourceIndexSha256") != source_index.get("sourceIndexSha256"):
        report.error("lineage_source_index", "/evidence/normalizationLineage/sourceIndexSha256", "lineage is not bound to this source index")

    classes = _object(ontology.get("classes"))
    objects = _object(ontology.get("objectProperties"))
    datatypes = _object(ontology.get("datatypeProperties"))
    all_elements = set(classes) | set(objects) | set(datatypes)
    for class_id in sorted((_REQUIRED_SOURCE_CLASSES | _REQUIRED_DOMAIN_CLASSES) - set(classes)):
        report.error("required_class", f"/ontology/classes/{class_id}", "required three-layer class is missing")
    endpoints = {str(item.get("endpoint")) for item in _array(source_index.get("documents")) if isinstance(item, dict)}
    if not any("funding-profile" in endpoint for endpoint in endpoints):
        if "FundingEvent" in classes:
            report.error("unsupported_funding_event", "/ontology/classes/FundingEvent", "FundingEvent has no raw endpoint instances and must be omitted")
        resolutions = _object(grounding.get("domainHintResolutions"))
        for hint in (
            "entity:FundingEvent", "relation:Paper.YEARLY_METRIC", "relation:Scholar.YEARLY_METRIC",
            "relation:Institution.HAS_FUNDING_ROUND",
        ):
            if _object(resolutions.get(hint)).get("status") != "omitted":
                report.error("unsupported_hint_implementation", f"/grounding/domainHintResolutions/{hint}", "hint has no raw instance/endpoint support and must be omitted")
    for child, parent in (
        ("Paper", "DomainEntity"), ("Scholar", "DomainEntity"), ("Institution", "DomainEntity"),
        ("PaperObservation", "EntityObservation"), ("ScholarObservation", "EntityObservation"),
        ("InstitutionObservation", "EntityObservation"),
    ):
        if child in classes and parent not in _array(_object(classes.get(child)).get("subClassOf")):
            report.error("required_subclass", f"/ontology/classes/{child}/subClassOf", f"{child} must be a subclass of {parent}")

    for domain, range_id in (
        ("SearchResponse", "SourceDocument"), ("SearchResponse", "SourceRecord"),
        ("SearchResponse", "SourceFragment"), ("SourceDocument", "SourceFragment"),
        ("SourceRecord", "SourceDocument"), ("SourceFragment", "SourceRecord"),
        ("SourceFragment", "SourceDocument"),
        ("EntityObservation", "SourceRecord"), ("EntityObservation", "DomainEntity"),
    ):
        if not _has_object_shape(objects, domain, range_id):
            report.error("source_navigation_shape", "/ontology/objectProperties", f"missing navigation {domain} -> {range_id}")

    required_access_datatypes = {
        "sourcePath": {"SourceDocument", "SourceRecord", "SourceFragment"},
        "jsonPointer": {"SourceRecord", "SourceFragment"},
        "sourceSha256": {"SourceDocument"},
        "recordHash": {"SourceRecord"},
        "resultRank": {"SourceRecord", "EntityObservation"},
        "endpoint": {"SearchResponse", "SourceDocument"},
        "sourceSystem": {"SourceDocument"},
        "fragmentValueKind": {"SourceFragment"},
        "fragmentValueHash": {"SourceFragment"},
    }
    for name, allowed_domains in required_access_datatypes.items():
        matches = [item for identifier, item in datatypes.items() if identifier == name or identifier.lower().endswith(name.lower())]
        if not matches or not any(_object(item).get("domain") in allowed_domains for item in matches):
            report.error("source_access_property", "/ontology/datatypeProperties", f"missing {name} on an appropriate Source/Observation class")
    json_pointer_domains = {
        _object(item).get("domain")
        for identifier, item in datatypes.items()
        if identifier.lower().endswith("jsonpointer")
    }
    for required_domain in ("SourceRecord", "SourceFragment"):
        if required_domain not in json_pointer_domains:
            report.error(
                "source_json_pointer_domain",
                "/ontology/datatypeProperties",
                f"{required_domain} needs its own exact JSON Pointer access property",
            )

    raw_classifications = _object(grounding.get("rawPathClassifications"))
    profiles = _object(source_index.get("pathProfiles"))
    if set(raw_classifications) != set(profiles):
        for missing in sorted(set(profiles) - set(raw_classifications)):
            report.error("unclassified_raw_path", f"/grounding/rawPathClassifications/{missing}", "raw path profile is not classified")
        for invented in sorted(set(raw_classifications) - set(profiles)):
            report.error("invented_raw_path", f"/grounding/rawPathClassifications/{invented}", "raw path classification has no source profile")
    for key, raw in raw_classifications.items():
        item = _object(raw)
        classification = item.get("classification")
        expected = _object(profiles.get(key)).get("classification")
        if classification not in _RAW_PATH_CLASSES or classification == "ignored":
            report.error("raw_path_class", f"/grounding/rawPathClassifications/{key}", "unsupported raw path classification")
        if expected is not None and classification != expected:
            report.error("raw_path_class_mismatch", f"/grounding/rawPathClassifications/{key}", f"expected controller classification {expected}")

    normalization_refs = set(_refs(grounding.get("normalizationEvidenceRefs")))
    if lineage_ref not in normalization_refs:
        report.error("normalization_evidence", "/grounding/normalizationEvidenceRefs", "normalization lineage evidence is not cited", [lineage_ref])

    bindings = _object(grounding.get("sourceBindings"))
    for missing in sorted(all_elements - set(bindings)):
        report.error("missing_source_binding", f"/grounding/sourceBindings/{missing}", "ontology element has no source/navigation binding")
    bound_patterns: set[str] = set()
    for identifier, raw in bindings.items():
        path = f"/grounding/sourceBindings/{identifier}"
        item = _object(raw)
        if identifier not in all_elements:
            report.error("invented_source_binding", path, "binding references an undeclared ontology element")
        semantic_refs = _refs(item.get("semanticEvidenceRefs"))
        source_refs = _refs(item.get("sourceEvidenceRefs"))
        if not semantic_refs or not source_refs or source_ref not in source_refs:
            report.error("incomplete_source_binding", path, "binding needs semantic evidence and raw source-index evidence", [source_ref])
        if not _array(item.get("navigation")):
            report.error("missing_navigation", path, "binding needs a Domain/Observation/SourceRecord/SourceDocument navigation chain")
        bound_patterns.update(_binding_patterns(item))
    for key, profile in profiles.items():
        classification = _object(profile).get("classification")
        pattern = _object(profile).get("pathPattern")
        if classification in {"semantic_promoted", "observation_promoted"}:
            if key not in bound_patterns:
                report.error("promoted_path_unbound", f"/grounding/rawPathClassifications/{key}", "promoted raw path is not bound to an ontology element")

    access_paths = _object(grounding.get("sourceAccessPaths"))
    def class_compatible(actual: Any, expected: Any, visited: set[str] | None = None) -> bool:
        if not isinstance(actual, str) or not isinstance(expected, str):
            return False
        if actual == expected:
            return True
        visited = set() if visited is None else visited
        if actual in visited:
            return False
        visited.add(actual)
        return any(class_compatible(parent, expected, visited) for parent in _array(_object(classes.get(actual)).get("subClassOf")))

    for class_id in sorted(classes):
        if class_id not in access_paths:
            report.error("missing_source_access_path", f"/grounding/sourceAccessPaths/{class_id}", "class has no documented navigation to raw source")
            continue
        access = _object(access_paths.get(class_id))
        path = f"/grounding/sourceAccessPaths/{class_id}"
        if access.get("startClassId") != class_id or access.get("direction") != "domain_to_raw":
            report.error("source_access_direction", path, "source access path must start at its class and navigate domain-to-raw")
        current_class: Any = class_id
        for position, raw_step in enumerate(_array(access.get("steps"))):
            step = str(raw_step)
            inverse = step.startswith("^")
            property_id = step[1:] if inverse else step
            prop = _object(objects.get(property_id))
            if not prop:
                report.error("source_access_property_step", f"{path}/steps/{position}", f"unknown object property {property_id}")
                continue
            expected_current = prop.get("range") if inverse else prop.get("domain")
            next_class = prop.get("domain") if inverse else prop.get("range")
            if not class_compatible(current_class, expected_current):
                report.error(
                    "source_access_domain_range",
                    f"{path}/steps/{position}",
                    f"{step} cannot be applied to {current_class}; expected {expected_current}",
                )
            current_class = next_class
        if current_class != access.get("terminalClassId") or current_class != "SourceDocument":
            report.error("source_access_terminal", path, "domain-to-raw path must terminate at SourceDocument")
        locator_ids = set(_refs(access.get("locatorPropertyIds")))
        for locator_id in sorted(locator_ids):
            if locator_id not in datatypes:
                report.error("source_access_locator", f"{path}/locatorPropertyIds", f"unknown locator property {locator_id}")
        if not {"sourcePath", "sourceSha256"}.issubset(locator_ids):
            report.error("source_access_document_locator", f"{path}/locatorPropertyIds", "path must expose sourcePath and sourceSha256")
        if class_id != "SourceDocument" and "jsonPointer" not in locator_ids:
            report.error("source_access_pointer_locator", f"{path}/locatorPropertyIds", "non-document paths must expose an exact JSON Pointer")
        if source_ref not in _refs(access.get("evidenceRefs")):
            report.error("source_access_evidence", f"{path}/evidenceRefs", "source path must cite the source index", [source_ref])
        if class_id == "SearchResponse":
            steps = _array(access.get("steps"))
            if "responseHasRecord" in steps or "responseHasFragment" not in steps:
                report.error(
                    "empty_response_fragment_navigation",
                    f"{path}/steps",
                    "SearchResponse raw navigation must use responseHasFragment and must not require a SourceRecord",
                )

    for question_id, raw_coverage in _object(grounding.get("cqCoverage")).items():
        item = _object(raw_coverage)
        plan = _object(item.get("queryPlan"))
        plan_key = "paths" if item.get("status") == "covered" else "fallbackPaths"
        paths = _array(plan.get(plan_key))
        if item.get("status") == "covered" and not paths:
            report.error("cq_executable_plan", f"/grounding/cqCoverage/{question_id}/queryPlan", "covered CQ needs executable property paths")
        declared_elements = set(_array(item.get("ontologyElementIds")))
        for path_index, raw_path in enumerate(paths):
            query_path = _object(raw_path)
            path_location = f"/grounding/cqCoverage/{question_id}/queryPlan/{plan_key}/{path_index}"
            current_class: Any = query_path.get("startClassId")
            if current_class not in classes:
                report.error("cq_query_start", path_location, f"unknown start class {current_class}")
                continue
            used_elements = {str(current_class)}
            terminated_datatype: str | None = None
            for step_index, raw_step in enumerate(_array(query_path.get("steps"))):
                step = str(raw_step)
                inverse = step.startswith("^")
                property_id = step[1:] if inverse else step
                used_elements.add(property_id)
                if property_id in objects:
                    if terminated_datatype is not None:
                        report.error("cq_query_after_datatype", f"{path_location}/steps/{step_index}", "datatype property must terminate a CQ path")
                        continue
                    prop = _object(objects[property_id])
                    expected_current = prop.get("range") if inverse else prop.get("domain")
                    next_class = prop.get("domain") if inverse else prop.get("range")
                    if not class_compatible(current_class, expected_current):
                        report.error(
                            "cq_query_domain_range",
                            f"{path_location}/steps/{step_index}",
                            f"{step} cannot be applied to {current_class}; expected {expected_current}",
                        )
                    current_class = next_class
                elif property_id in datatypes:
                    prop = _object(datatypes[property_id])
                    if inverse or not class_compatible(current_class, prop.get("domain")):
                        report.error(
                            "cq_query_datatype_domain",
                            f"{path_location}/steps/{step_index}",
                            f"datatype {property_id} cannot be applied to {current_class}",
                        )
                    terminated_datatype = str(prop.get("range"))
                else:
                    report.error("cq_query_property", f"{path_location}/steps/{step_index}", f"unknown property {property_id}")
            if not used_elements.issubset(declared_elements):
                report.error("cq_query_elements", path_location, "ontologyElementIds must include every class/property used by the query plan")
            if terminated_datatype is not None:
                if query_path.get("resultDatatype") != terminated_datatype:
                    report.error("cq_query_result_datatype", path_location, f"expected resultDatatype {terminated_datatype}")
            elif query_path.get("resultClassId") != current_class:
                report.error("cq_query_result_class", path_location, f"expected resultClassId {current_class}")

    coverage = _object(grounding.get("sourceCoverage"))
    metrics = _object(source_index.get("metrics"))
    expected_coverage = {
        "documentCount": metrics.get("documentCount"),
        "nonEmptyResponseCount": metrics.get("nonEmptyResponseCount"),
        "emptyResponseCount": metrics.get("emptyResponseCount"),
        "recordObservationCount": metrics.get("recordCount"),
        "fragmentCount": metrics.get("fragmentCount"),
        "rawPathPatternCount": metrics.get("pathPatternCount"),
        "unclassifiedRawPathCount": 0,
        "sourceIndexSha256": source_index.get("sourceIndexSha256"),
    }
    for key, expected in expected_coverage.items():
        if coverage.get(key) != expected:
            report.error("raw_coverage_count", f"/grounding/sourceCoverage/{key}", f"expected {expected}")

    entity_resolution = _object(grounding.get("entityResolutionMappings"))
    for kind, class_id in (("paper", "Paper"), ("scholar", "Scholar"), ("institution", "Institution")):
        expected = _object(_object(source_index.get("entityProfiles")).get(kind))
        actual = _object(entity_resolution.get(class_id))
        if not actual:
            report.error("missing_entity_resolution", f"/grounding/entityResolutionMappings/{class_id}", "entity resolution mapping is required")
            continue
        for key in ("businessEntityCount", "observationCount", "mergePolicy", "conflictingEntityCount"):
            if actual.get(key) != expected.get(key):
                report.error("entity_resolution_mismatch", f"/grounding/entityResolutionMappings/{class_id}/{key}", f"expected {expected.get(key)!r}")
        if actual.get("conflictPolicy") != "observation_only":
            report.error("entity_conflict_policy", f"/grounding/entityResolutionMappings/{class_id}/conflictPolicy", "conflicting values must remain observation-only")
        if _object(actual.get("stableFieldConsensus")) != _object(expected.get("fieldConsensus")):
            report.error(
                "entity_field_consensus",
                f"/grounding/entityResolutionMappings/{class_id}/stableFieldConsensus",
                "stable-field consensus and conflict disposition must exactly match the source index",
            )
    conflict_domain_mappings = {
        ("paper", "title"): ("title", "Paper", "PaperObservation"),
        ("paper", "abstract"): ("abstract", "Paper", "PaperObservation"),
        ("paper", "published_at"): ("publishedAt", "Paper", "PaperObservation"),
        ("scholar", "name"): ("scholarName", "Scholar", "ScholarObservation"),
        ("scholar", "email"): ("email", "Scholar", "ScholarObservation"),
        ("scholar", "homepage"): ("homepage", "Scholar", "ScholarObservation"),
        ("institution", "name"): ("institutionName", "Institution", "InstitutionObservation"),
        ("institution", "country"): ("country", "Institution", "InstitutionObservation"),
        ("institution", "region"): ("region", "Institution", "InstitutionObservation"),
    }
    for (kind, field), (property_id, entity_class, observation_class) in conflict_domain_mappings.items():
        consensus = _object(
            _object(_object(_object(source_index.get("entityProfiles")).get(kind)).get("fieldConsensus")).get(field)
        )
        if int(consensus.get("conflictingEntityCount", 0) or 0) > 0:
            if _object(datatypes.get(property_id)).get("domain") != entity_class:
                report.error(
                    "conflicting_value_on_entity",
                    f"/ontology/datatypeProperties/{property_id}/domain",
                    f"canonical {field} remains on {entity_class} under require_consensus; conflicting values use {observation_class}",
                )

    observation_value_specs = {
        "title": ("observedPaperTitle", "PaperObservation", "Paper", "/openapi/paper/search", "/data/list/*/title"),
        "abstract": ("observedPaperAbstract", "PaperObservation", "Paper", "/openapi/paper/search", "/data/list/*/abstract"),
        "publishedAt": ("observedPaperPublishedAt", "PaperObservation", "Paper", "/openapi/paper/search", "/data/list/*/published_at"),
        "scholarName": ("observedScholarName", "ScholarObservation", "Scholar", "/openapi/scholar/search", "/data/list/*/name"),
        "homepage": ("observedScholarHomepage", "ScholarObservation", "Scholar", "/openapi/scholar/search", "/data/list/*/homepage"),
        "email": ("observedScholarEmail", "ScholarObservation", "Scholar", "/openapi/scholar/search", "/data/list/*/email"),
        "institutionName": ("observedInstitutionName", "InstitutionObservation", "Institution", "/openapi/institutions/search", "/data/list/*/name"),
        "country": ("observedInstitutionCountry", "InstitutionObservation", "Institution", "/openapi/institutions/search", "/data/list/*/country"),
        "region": ("observedInstitutionRegion", "InstitutionObservation", "Institution", "/openapi/institutions/search", "/data/list/*/region"),
    }
    observation_values = _object(grounding.get("observationValueMappings"))
    for canonical_id, (observed_id, observation_class, entity_class, endpoint, path_pattern) in observation_value_specs.items():
        if canonical_id not in datatypes:
            continue
        observed = _object(datatypes.get(observed_id))
        path = f"/grounding/observationValueMappings/{canonical_id}"
        mapping = _object(observation_values.get(canonical_id))
        if observed.get("domain") != observation_class or observed.get("range") != _object(datatypes[canonical_id]).get("range"):
            report.error("observation_value_property", f"/ontology/datatypeProperties/{observed_id}", "stable raw value needs a typed observation-scoped property with the canonical datatype")
        expected_mapping = {
            "entityClassId": entity_class,
            "observationClassId": observation_class,
            "canonicalPropertyId": canonical_id,
            "observationPropertyId": observed_id,
            "endpoint": endpoint,
            "pathPattern": path_pattern,
            "observationProjection": "always_preserve_per_record",
            "canonicalProjection": "require_consensus_by_business_id",
            "conflictDisposition": "observation_only",
        }
        for field, expected_value in expected_mapping.items():
            if mapping.get(field) != expected_value:
                report.error("observation_value_mapping", f"{path}/{field}", f"expected {expected_value!r}")
        if source_ref not in _refs(mapping.get("evidenceRefs")):
            report.error("observation_value_evidence", path, "observation value mapping must cite the source index", [source_ref])

    relation_snapshot_specs = {
        "observationHasTopic": ("EntityObservation", "Topic"),
        "observationHasVenue": ("EntityObservation", "Venue"),
        "observationHasAward": ("EntityObservation", "Award"),
        "paperObservationHasInstitution": ("PaperObservation", "Institution"),
        "scholarObservationHasInstitution": ("ScholarObservation", "Institution"),
        "scholarObservationHasPaper": ("ScholarObservation", "Paper"),
        "institutionObservationHasNewsItem": ("InstitutionObservation", "NewsItem"),
        "institutionObservationHasRelatedPaper": ("InstitutionObservation", "Paper"),
        "institutionObservationHasRelatedScholar": ("InstitutionObservation", "Scholar"),
        "observationHasAuthorship": ("PaperObservation", "Authorship"),
    }
    relation_snapshot_paths = {
        "observationHasTopic": {
            "/openapi/paper/search|/data/list/*/fields/*",
            "/openapi/scholar/search|/data/list/*/fields/*",
            "/openapi/institutions/search|/data/list/*/fields/*",
        },
        "observationHasVenue": {
            "/openapi/paper/search|/data/list/*/venue",
            "/openapi/scholar/search|/data/list/*/venues/*",
        },
        "observationHasAward": {
            "/openapi/paper/search|/data/list/*/awards/*",
            "/openapi/scholar/search|/data/list/*/awards/*",
        },
        "paperObservationHasInstitution": {"/openapi/paper/search|/data/list/*/institution_ids/*"},
        "scholarObservationHasInstitution": {"/openapi/scholar/search|/data/list/*/institution_ids/*"},
        "scholarObservationHasPaper": {"/openapi/scholar/search|/data/list/*/paper_ids/*"},
        "institutionObservationHasNewsItem": {"/openapi/institutions/search|/data/list/*/news/*"},
        "institutionObservationHasRelatedPaper": {"/openapi/institutions/search|/data/list/*/related_paper_ids/*"},
        "institutionObservationHasRelatedScholar": {"/openapi/institutions/search|/data/list/*/related_scholar_ids/*"},
        "observationHasAuthorship": {"/openapi/paper/search|/data/list/*/author_ids/*"},
    }
    relation_snapshots = _object(grounding.get("relationSnapshotMappings"))
    covered_relation_paths: set[str] = set()
    concept_label_properties = {
        "Topic": "topicName",
        "Venue": "venueName",
        "Award": "awardName",
    }
    for property_id, (domain, range_id) in relation_snapshot_specs.items():
        # A snapshot contract is mandatory only when this acquisition contains
        # at least one instance path for it.  An absent optional API array is
        # represented by source-index path absence, not by a phantom property
        # and an unexecutable mapping.
        if not (relation_snapshot_paths[property_id] & set(profiles)):
            continue
        prop = _object(objects.get(property_id))
        mapping = _object(relation_snapshots.get(property_id))
        path = f"/grounding/relationSnapshotMappings/{property_id}"
        if prop.get("domain") != domain or prop.get("range") != range_id:
            report.error("relation_snapshot_property", f"/ontology/objectProperties/{property_id}", f"expected {domain} -> {range_id}")
        if mapping.get("observationClassId") != domain or mapping.get("targetClassId") != range_id or mapping.get("observationPropertyId") != property_id:
            report.error("relation_snapshot_mapping", path, "relationship array needs a typed per-response snapshot mapping")
        label_property_id = concept_label_properties.get(range_id)
        if label_property_id is not None:
            label_property = _object(datatypes.get(label_property_id))
            if label_property.get("domain") != range_id or label_property.get("range") != "xsd:string":
                report.error(
                    "relation_snapshot_concept_label",
                    f"/ontology/datatypeProperties/{label_property_id}",
                    f"{property_id} requires {label_property_id} with shape {range_id} -> xsd:string",
                )
        shortcut_id = mapping.get("domainShortcutPropertyId")
        if shortcut_id is not None:
            shortcut = _object(objects.get(shortcut_id)) if isinstance(shortcut_id, str) else {}
            if not shortcut:
                report.error(
                    "relation_snapshot_shortcut",
                    f"{path}/domainShortcutPropertyId",
                    "domain shortcut must reference a declared object property",
                )
            elif shortcut.get("range") != range_id:
                report.error(
                    "relation_snapshot_shortcut_range",
                    f"{path}/domainShortcutPropertyId",
                    f"domain shortcut must target {range_id}",
                )
        if mapping.get("missingnessPolicy") not in {
            "absent_array_or_field_means_unknown_not_false", "absent_array_means_unknown_not_false"
        }:
            report.error("relation_snapshot_missingness", f"{path}/missingnessPolicy", "missing relationship arrays must remain unknown")
        if source_ref not in _refs(mapping.get("evidenceRefs")):
            report.error("relation_snapshot_evidence", path, "relation snapshot must cite the source index", [source_ref])
        for source_path in _array(mapping.get("sourcePaths")):
            item = _object(source_path)
            covered_relation_paths.add(f"{item.get('endpoint', '')}|{item.get('pathPattern', '')}")
    required_relation_paths = {
        "/openapi/paper/search|/data/list/*/author_ids/*",
        "/openapi/paper/search|/data/list/*/fields/*",
        "/openapi/paper/search|/data/list/*/venue",
        "/openapi/paper/search|/data/list/*/awards/*",
        "/openapi/paper/search|/data/list/*/institution_ids/*",
        "/openapi/scholar/search|/data/list/*/fields/*",
        "/openapi/scholar/search|/data/list/*/venues/*",
        "/openapi/scholar/search|/data/list/*/awards/*",
        "/openapi/scholar/search|/data/list/*/institution_ids/*",
        "/openapi/scholar/search|/data/list/*/paper_ids/*",
        "/openapi/institutions/search|/data/list/*/fields/*",
        "/openapi/institutions/search|/data/list/*/news/*",
        "/openapi/institutions/search|/data/list/*/related_paper_ids/*",
        "/openapi/institutions/search|/data/list/*/related_scholar_ids/*",
    }
    available_relation_paths = required_relation_paths & set(profiles)
    for missing in sorted(available_relation_paths - covered_relation_paths):
        report.error("relation_snapshot_path", "/grounding/relationSnapshotMappings", f"raw relationship path lacks an observation snapshot: {missing}")

    for property_id in ("observesPaper", "observesScholar", "observesInstitution"):
        if _array(_object(objects.get(property_id)).get("subPropertyOf")) != ["observesEntity"]:
            report.error("observation_subproperty", f"/ontology/objectProperties/{property_id}/subPropertyOf", f"{property_id} must be a sub-property of observesEntity")
    for left, right in (
        ("hasAuthorship", "authorshipOfPaper"),
        ("recordHasFragment", "fragmentFromRecord"),
        ("documentHasFragment", "fragmentFromDocument"),
        ("observationHasAuthorship", "authorshipFromObservation"),
    ):
        if _object(objects.get(left)).get("inverseOf") != right or _object(objects.get(right)).get("inverseOf") != left:
            report.error("required_inverse_property", f"/ontology/objectProperties/{left}", f"{left} and {right} must declare reciprocal inverseOf")

    iri_contract = _object(grounding.get("iriGenerationMappings"))
    if iri_contract.get("contractVersion") != "dataelf-iri-generation.v1" or iri_contract.get("baseNamespace") != _object(ontology.get("metadata")).get("namespace"):
        report.error("iri_generation_contract", "/grounding/iriGenerationMappings", "deterministic IRI contract version and base namespace are required")
    iri_classes = _object(iri_contract.get("classMappings"))
    if set(iri_classes) != set(classes):
        report.error("iri_class_coverage", "/grounding/iriGenerationMappings/classMappings", "IRI generation rules must cover every ontology class exactly")
    for class_id, raw_mapping in iri_classes.items():
        mapping = _object(raw_mapping)
        path = f"/grounding/iriGenerationMappings/classMappings/{class_id}"
        if mapping.get("instantiationPolicy") == "abstract_no_direct_instances":
            if class_id not in {"DomainEntity", "EntityObservation"}:
                report.error("iri_abstract_class", path, "only declared abstract layer classes may suppress direct instances")
        elif not str(mapping.get("template", "")).startswith("{baseNamespace}instance/") or not _array(mapping.get("identityInputs")):
            report.error("iri_mapping", path, "concrete class needs a deterministic template and identity inputs")
        if not str(mapping.get("collisionPolicy", "")).strip() or source_ref not in _refs(mapping.get("evidenceRefs")):
            report.error("iri_collision_evidence", path, "IRI mapping needs an explicit collision policy and source evidence")
    for required_class, token in (
        ("Paper", "businessIdEncoded"), ("Scholar", "businessIdEncoded"), ("Institution", "businessIdEncoded"),
        ("SourceFragment", "jsonPointerToken"), ("SourceRecord", "jsonPointerToken"),
        ("Authorship", "authorArrayIndex"), ("NewsItem", "newsPointerToken"),
    ):
        if token not in str(_object(iri_classes.get(required_class)).get("template", "")):
            report.error("iri_identity_input", f"/grounding/iriGenerationMappings/classMappings/{required_class}/template", f"template must retain {token}")

    shacl_contract = _object(grounding.get("shaclContract"))
    if shacl_contract.get("contractVersion") != "dataelf-shacl-contract.v1" or shacl_contract.get("artifact") != "shacl.ttl":
        report.error("shacl_contract", "/grounding/shaclContract", "deterministic shacl.ttl contract is required")
    required_constraint_kinds = {
        "required_business_keys", "exact_observation_endpoints", "exact_source_ownership",
        "positive_author_order", "datatype_lexical_space", "inverse_properties",
        "observation_subproperties", "authoredBy_authorship_consistency",
    }
    if set(_array(shacl_contract.get("requiredConstraintKinds"))) != required_constraint_kinds:
        report.error("shacl_constraint_coverage", "/grounding/shaclContract/requiredConstraintKinds", "SHACL contract lacks a mandatory executable constraint kind")
    if source_ref not in _refs(shacl_contract.get("evidenceRefs")):
        report.error("shacl_evidence", "/grounding/shaclContract/evidenceRefs", "SHACL contract must cite source evidence", [source_ref])
    entity_observation = _object(grounding.get("entityObservationMappings"))
    for entity_class, observation_class, specific_property, endpoint in (
        ("Paper", "PaperObservation", "observesPaper", "/openapi/paper/search"),
        ("Scholar", "ScholarObservation", "observesScholar", "/openapi/scholar/search"),
        ("Institution", "InstitutionObservation", "observesInstitution", "/openapi/institutions/search"),
    ):
        item = _object(entity_observation.get(entity_class))
        expected = {
            "observationClassId": observation_class,
            "observedEntityPropertyId": specific_property,
            "genericObservedEntityPropertyId": "observesEntity",
            "sourceRecordClassId": "SourceRecord",
            "sourceDocumentClassId": "SourceDocument",
            "endpoint": endpoint,
            "businessIdPathPattern": "/data/list/*/id",
        }
        for field, expected_value in expected.items():
            if item.get(field) != expected_value:
                report.error(
                    "entity_observation_mapping",
                    f"/grounding/entityObservationMappings/{entity_class}/{field}",
                    f"expected {expected_value}",
                )
        if item.get("rowLocatorFields") != ["documentId", "jsonPointer", "recordHash"]:
            report.error(
                "entity_observation_locator",
                f"/grounding/entityObservationMappings/{entity_class}/rowLocatorFields",
                "raw observation locator must use documentId, jsonPointer and recordHash",
            )
        if source_ref not in _refs(item.get("evidenceRefs")):
            report.error("entity_observation_evidence", f"/grounding/entityObservationMappings/{entity_class}", "mapping must cite the source index", [source_ref])

    response_mappings = _object(grounding.get("responseObservationMappings"))
    for class_id in ("PaperObservation", "ScholarObservation", "InstitutionObservation"):
        item = _object(response_mappings.get(class_id))
        if item.get("responseClassId") != "SearchResponse" or item.get("sourceRecordClassId") != "SourceRecord":
            report.error("response_observation_mapping", f"/grounding/responseObservationMappings/{class_id}", "observation must preserve response, source record and within-response rank")
        for field, expected_property in (
            ("responsePropertyId", "observationInResponse"),
            ("recordPropertyId", "observationFromRecord"),
            ("genericObservedEntityPropertyId", "observesEntity"),
        ):
            if item.get(field) != expected_property or expected_property not in objects:
                report.error(
                    "response_navigation_property",
                    f"/grounding/responseObservationMappings/{class_id}/{field}",
                    f"observation mapping must use declared {expected_property}",
                )
        if item.get("resultRankFormula") != "data.list array_index + 1" or item.get("resultRankBase") != 1:
            report.error(
                "response_rank_formula",
                f"/grounding/responseObservationMappings/{class_id}",
                "result rank must preserve the one-based source-index formula",
            )
        if source_ref not in _refs(item.get("evidenceRefs")):
            report.error("response_mapping_evidence", f"/grounding/responseObservationMappings/{class_id}", "response mapping must cite the source index", [source_ref])
        rank_property = item.get("resultRankPropertyId")
        if rank_property not in datatypes:
            report.error("response_rank_property", f"/grounding/responseObservationMappings/{class_id}/resultRankPropertyId", "result rank property is missing")
        elif _object(datatypes.get(rank_property)).get("range") != "xsd:positiveInteger":
            report.error("response_rank_base", f"/ontology/datatypeProperties/{rank_property}/range", "source resultRank is one-based and must be a positive integer")

    association = _object(grounding.get("associationMappings")).get("Authorship")
    association = _object(association)
    if not association:
        report.error("missing_authorship_mapping", "/grounding/associationMappings/Authorship", "Authorship must be reified")
    else:
        endpoint_properties = _array(association.get("endpointPropertyIds"))
        if len(endpoint_properties) != 2 or any(identifier not in objects for identifier in endpoint_properties):
            report.error("association_endpoints", "/grounding/associationMappings/Authorship/endpointPropertyIds", "Authorship needs valid Paper and Scholar endpoint properties")
        qualifiers = _object(association.get("qualifiers"))
        for qualifier, formula in (("authorOrder", "array_index + 1"), ("isFirstAuthor", "array_index == 0")):
            item = _object(qualifiers.get(qualifier))
            if item.get("propertyId") not in datatypes or item.get("formula") != formula:
                report.error("association_qualifier", f"/grounding/associationMappings/Authorship/qualifiers/{qualifier}", f"qualifier needs formula {formula}")
        if "authorOrder" in datatypes and _object(datatypes["authorOrder"]).get("range") != "xsd:positiveInteger":
            report.error("author_order_range", "/ontology/datatypeProperties/authorOrder/range", "one-based authorOrder must be a positive integer")

    for identifier in objects | datatypes:
        if any(identifier.lower().endswith(token) for token in _FORBIDDEN_PROPERTY_IDS):
            report.error("unsupported_property", f"/ontology", f"unsupported or duplicate property {identifier}")
    mutable_tokens = ("citationcount", "papercount", "scholarcount", "funding", "hotness", "impact")
    for identifier, raw in datatypes.items():
        domain = _object(raw).get("domain")
        if domain in {"Paper", "Scholar", "Institution"} and any(token in identifier.lower() for token in mutable_tokens):
            report.error("mutable_metric_on_entity", f"/ontology/datatypeProperties/{identifier}/domain", "mutable metric must be attached to an observation")

    column_classifications = _object(grounding.get("columnClassifications"))
    lineage_mappings = _object(lineage.get("mappings"))
    for coordinate, mapping_value in lineage_mappings.items():
        mapping = _object(mapping_value)
        if mapping.get("transform") == "omitted" and int(mapping.get("outputNonBlankCount", 0) or 0) > 0:
            report.error(
                "unexplained_normalization_output",
                f"/evidence/normalizationLineage/mappings/{coordinate}",
                "a non-blank normalized output cannot be classified as omitted; declare its raw path, derivation, or constant default",
                [lineage_ref],
            )
        if mapping.get("transform") == "constant_default":
            if not str(mapping.get("formula", "")).strip() or mapping.get("defaultedCount") != mapping.get("outputNonBlankCount"):
                report.error(
                    "constant_default_lineage",
                    f"/evidence/normalizationLineage/mappings/{coordinate}",
                    "constant defaults need an explicit formula and exact defaultedCount",
                    [lineage_ref],
                )
        if mapping.get("transform") == "fallback_alias" and not str(mapping.get("deprecationNote", "")).strip():
            report.error(
                "undocumented_fallback_alias",
                f"/evidence/normalizationLineage/mappings/{coordinate}/deprecationNote",
                "legacy fallback aliases must be explicitly deprecated and forbidden from ontology promotion",
                [lineage_ref],
            )
    for coordinate, raw in column_classifications.items():
        property_ids = _array(_object(raw).get("propertyIds"))
        mapping = _object(lineage_mappings.get(coordinate))
        if property_ids and mapping.get("ontologyEligible") is False:
            report.error("unsafe_normalization_mapping", f"/grounding/columnClassifications/{coordinate}", "fallback/default/opaque normalized column is ineligible for ontology promotion", [lineage_ref])
        if property_ids and mapping.get("transform") == "derived" and not str(mapping.get("formula", "")).strip():
            report.error("derived_without_formula", f"/grounding/columnClassifications/{coordinate}", "derived value lacks a formula", [lineage_ref])

    for table, profile_ref in _object(_object(evidence.get("toolIndex")).get("profiles")).items():
        profile = evidence_result(evidence, profile_ref)
        for column, details in _object(profile.get("columns")).items():
            coordinate = f"{table}.{column}"
            property_ids = _array(_object(column_classifications.get(coordinate)).get("propertyIds"))
            if _object(details).get("inferredType") == "jsonArray":
                for property_id in property_ids:
                    if _object(datatypes.get(property_id)).get("range") == "xsd:string":
                        report.error("opaque_business_array", f"/grounding/columnClassifications/{coordinate}", "non-empty business array cannot be promoted as opaque JSON text")

    datatype_raw_bindings: dict[str, dict[str, str]] = {}
    for property_id, entries in _object(grounding.get("datatypePropertyEvidence")).items():
        for raw in _array(entries):
            item = _object(raw)
            if item.get("sourceKind") != "raw_json":
                continue
            key = f"{item.get('endpoint', '')}|{item.get('pathPattern', '')}"
            datatype_raw_bindings.setdefault(key, {})[property_id] = str(item.get("formula", "")).strip()
    for raw_binding, property_formulas in datatype_raw_bindings.items():
        property_ids = set(property_formulas)
        if len(property_ids) > 1:
            formulas = [formula for formula in property_formulas.values() if formula]
            if len(formulas) == len(property_ids) and len(set(formulas)) == len(property_ids):
                continue
            report.error(
                "duplicate_semantic_raw_binding",
                "/grounding/datatypePropertyEvidence",
                f"one raw value is promoted as multiple datatype semantics: {sorted(property_ids)}",
                [source_ref],
            )

    relation_authority = _object(grounding.get("relationAuthority"))
    for relation_name, comparison in _object(source_index.get("relationComparisons")).items():
        item = _object(relation_authority.get(relation_name))
        if (
            item.get("authority") != _object(comparison).get("authority")
            or item.get("corroboration") != _object(comparison).get("corroboration")
            or item.get("differenceStrategy") != _object(comparison).get("differenceStrategy")
        ):
            report.error("relation_authority", f"/grounding/relationAuthority/{relation_name}", "authority, corroboration and difference strategy must match source comparison")
        if relation_name != "Paper-Scholar" and item.get("corroborationSemantics") != "non_exhaustive_highlight":
            report.error("highlight_semantics", f"/grounding/relationAuthority/{relation_name}/corroborationSemantics", "institution related_* relations must be marked non-exhaustive highlights")


def validate_candidate(
    ontology: Any,
    grounding: Any,
    evidence: dict[str, Any],
    config: Stage1Config,
) -> dict[str, Any]:
    report = ValidationReport()
    _validate_schema(ontology, grounding, report)
    if isinstance(ontology, dict) and isinstance(grounding, dict):
        _validate_ontology(ontology, grounding, evidence, config, report)
        _validate_source_coverage(ontology, grounding, evidence, config, report)
        _validate_evidence(ontology, grounding, evidence, report)
        _validate_observations(ontology, grounding, evidence, report)
        _validate_hints_and_cqs(ontology, grounding, evidence, config, report)
        _validate_v2_semantics(ontology, grounding, evidence, report)
    result = {
        "schemaVersion": "dataelf-validation.v1",
        "validatorVersion": VALIDATOR_VERSION,
        "status": "valid" if not report.errors else "invalid",
        "errors": report.errors,
        "warnings": report.warnings,
        "metrics": {
            "errorCount": len(report.errors),
            "warningCount": len(report.warnings),
            "tableCount": evidence.get("catalog", {}).get("tableCount"),
            "columnCount": sum(len(item.get("columns", [])) for item in evidence.get("catalog", {}).get("tables", [])),
        },
        "inputs": {
            "ontologySha256": sha256_json(ontology),
            "groundingSha256": sha256_json(grounding),
            "evidenceSha256": sha256_json(evidence),
        },
    }
    return result


def validate_review(review: Any, evidence: dict[str, Any], config: Stage1Config) -> list[str]:
    errors = schema_errors(review, "review.schema.json")
    if not isinstance(review, dict):
        return errors
    if evidence.get("sourceType") == "ai_index_raw":
        if review.get("schemaVersion") != "dataelf-ontology-review.v2":
            errors.append("/schemaVersion: raw-backed Stage 1 requires dataelf-ontology-review.v2")
        required_checks = {
            "informationCompleteness", "sourceNavigability", "missingnessSemantics", "associationEndpoints",
            "observationMetrics", "multivalueConcepts", "relationAuthority", "competencyQuestionExecutability",
            "instanceIdentity", "constraintExecutability",
        }
        if set(_object(review.get("checks"))) != required_checks:
            errors.append("/checks: all ten Stage 1 review checks are required")
    known_refs = set(_object(evidence.get("evidence")))
    for ref in _refs(review.get("checkedEvidenceRefs")):
        if ref not in known_refs:
            errors.append(f"/checkedEvidenceRefs: unknown evidence reference {ref}")
    for index, issue in enumerate(_array(review.get("issues"))):
        for ref in _refs(_object(issue).get("evidenceRefs")):
            if ref not in known_refs:
                errors.append(f"/issues/{index}/evidenceRefs: unknown evidence reference {ref}")
    for name, raw in _object(review.get("checks")).items():
        item = _object(raw)
        for ref in _refs(item.get("evidenceRefs")):
            if ref not in known_refs:
                errors.append(f"/checks/{name}/evidenceRefs: unknown evidence reference {ref}")
        if review.get("verdict") == "approve" and item.get("status") != "pass":
            errors.append(f"/checks/{name}/status: approve requires every mandatory check to pass")
    blocking = [
        item
        for item in _array(review.get("issues"))
        if _object(item).get("severity") in config.quality.blocking_severities
    ]
    if review.get("verdict") == "approve" and blocking:
        errors.append("/verdict: approve cannot contain blocking-severity issues")
    if review.get("verdict") == "revise" and not _array(review.get("issues")):
        errors.append("/issues: revise requires at least one structured issue")
    return errors
