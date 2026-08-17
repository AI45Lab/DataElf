from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.checkpoints import utc_now
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import Stage2Config
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.contract import Stage1Contract
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.rdf import MaterializedGraph, RDF


VALIDATION_VERSION = "dataelf-stage2-validator/2"


def _endpoint_rows(contract: Stage1Contract, endpoint: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    documents = {str(item["documentId"]): item for item in contract.source_index["documents"]}
    for document_id, envelope in contract.raw_documents.items():
        if str(documents[document_id]["endpoint"]) != endpoint:
            continue
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        items = data.get("list") if isinstance(data.get("list"), list) else []
        rows.extend(item for item in items if isinstance(item, dict))
    return rows


def _field_supported(rows: list[dict[str, Any]], field: str) -> bool:
    return any(field in row for row in rows)


def _entity_coverage(contract: Stage1Contract) -> dict[str, dict[str, set[str]]]:
    """Return direct and union identity sets implied by the canonical raw corpus."""

    kind_to_class = {"paper": "Paper", "scholar": "Scholar", "institution": "Institution"}
    direct = {class_id: set() for class_id in kind_to_class.values()}
    union = {class_id: set() for class_id in kind_to_class.values()}
    for record in contract.source_index["records"]:
        class_id = kind_to_class.get(str(record.get("entityKind")))
        business_id = str(record.get("businessId", ""))
        if class_id and business_id:
            direct[class_id].add(business_id)
            union[class_id].add(business_id)

    reference_fields = {
        "/openapi/paper/search": {"author_ids": "Scholar", "institution_ids": "Institution"},
        "/openapi/scholar/search": {"paper_ids": "Paper", "institution_ids": "Institution"},
        "/openapi/institutions/search": {
            "related_paper_ids": "Paper",
            "related_scholar_ids": "Scholar",
        },
    }
    for endpoint, fields in reference_fields.items():
        for row in _endpoint_rows(contract, endpoint):
            for field, class_id in fields.items():
                values = row.get(field)
                if not isinstance(values, list):
                    continue
                union[class_id].update(value for value in values if isinstance(value, str) and value)
    return {class_id: {"direct": direct[class_id], "union": union[class_id]} for class_id in direct}


def competency_query_results(graph: MaterializedGraph, contract: Stage1Contract) -> list[dict[str, Any]]:
    ontology = contract.ontology
    objects = ontology["objectProperties"]
    datatypes = ontology["datatypeProperties"]
    predicate_counts: dict[str, int] = {}
    for item in graph.quads:
        predicate_counts[item.predicate] = predicate_counts.get(item.predicate, 0) + 1

    def obj(property_id: str) -> int:
        definition = objects.get(property_id)
        if not isinstance(definition, dict) or not definition.get("uri"):
            return 0
        return predicate_counts.get(str(definition["uri"]), 0)

    def data(property_id: str) -> int:
        definition = datatypes.get(property_id)
        if not isinstance(definition, dict) or not definition.get("uri"):
            return 0
        return predicate_counts.get(str(definition["uri"]), 0)

    provenance_predicates = {
        str(objects[key]["uri"])
        for key in (
            "observationFromRecord",
            "recordFromDocument",
            "recordHasFragment",
            "fragmentFromDocument",
        )
    }
    paper_scholar = contract.source_index["relationComparisons"]["Paper-Scholar"]
    scholar_institution = contract.source_index["relationComparisons"]["Scholar-Institution"]
    scholar_rows = _endpoint_rows(contract, "/openapi/scholar/search")
    institution_rows = _endpoint_rows(contract, "/openapi/institutions/search")

    cq_02_evidence = {
        "scholarInstitutionLinks": obj("scholarAffiliatedWithInstitution"),
        "scholarPaperSnapshots": obj("scholarObservationHasPaper"),
    }
    cq_02_expected = {
        "scholarInstitutionLinks": scholar_institution["authoritativeCount"],
        "scholarPaperSnapshots": paper_scholar["corroboratingCount"],
    }
    cq_02_missing = [
        field
        for field in ("institution_ids", "paper_ids")
        if not _field_supported(scholar_rows, field)
    ]
    if not scholar_rows:
        cq_02 = {
            "id": "cq_02",
            "status": "not_applicable",
            "reason": "scholar search returned no records",
            "answerEvidence": cq_02_evidence,
            "expectedEvidence": cq_02_expected,
        }
    elif cq_02_missing:
        cq_02 = {
            "id": "cq_02",
            "status": "not_applicable",
            "reason": "scholar records do not expose required fields: " + ", ".join(cq_02_missing),
            "answerEvidence": cq_02_evidence,
            "expectedEvidence": cq_02_expected,
        }
    else:
        cq_02 = {
            "id": "cq_02",
            "status": "pass" if cq_02_evidence == cq_02_expected else "fail",
            "answerEvidence": cq_02_evidence,
            "expectedEvidence": cq_02_expected,
        }

    cq_03_evidence = {
        "institutionNewsLinks": obj("hasNewsItem"),
        "fundingObservations": data("fundingTotalUsd"),
    }
    cq_03_expected = {
        "institutionNewsLinks": sum(
            len(row["news"])
            for row in institution_rows
            if isinstance(row.get("news"), list)
        ),
        "fundingObservations": sum(
            row.get("funding_total_usd") not in (None, "")
            for row in institution_rows
            if "funding_total_usd" in row
        ),
    }
    cq_03_missing = [
        field
        for field in ("news", "funding_total_usd")
        if not _field_supported(institution_rows, field)
    ]
    if not institution_rows:
        cq_03 = {
            "id": "cq_03",
            "status": "not_applicable",
            "reason": "institution search returned no records",
            "answerEvidence": cq_03_evidence,
            "expectedEvidence": cq_03_expected,
        }
    elif cq_03_missing:
        cq_03 = {
            "id": "cq_03",
            "status": "not_applicable",
            "reason": "institution records do not expose required fields: " + ", ".join(cq_03_missing),
            "answerEvidence": cq_03_evidence,
            "expectedEvidence": cq_03_expected,
        }
    else:
        cq_03 = {
            "id": "cq_03",
            "status": "pass" if cq_03_evidence == cq_03_expected else "fail",
            "answerEvidence": cq_03_evidence,
            "expectedEvidence": cq_03_expected,
        }

    return [
        {
            "id": "cq_01",
            "status": "pass" if obj("authoredBy") and obj("affiliatedWithInstitution") else "fail",
            "answerEvidence": {"paperScholarLinks": obj("authoredBy"), "paperInstitutionLinks": obj("affiliatedWithInstitution")},
        },
        cq_02,
        cq_03,
        {
            "id": "cq_04",
            "status": "pass" if obj("hasTopic") and obj("hasVenue") and data("citationCount") else "fail",
            "answerEvidence": {"topicLinks": obj("hasTopic"), "venueLinks": obj("hasVenue"), "citationObservations": data("citationCount")},
        },
        {
            "id": "cq_05",
            "status": "pass" if all(predicate_counts.get(uri, 0) for uri in provenance_predicates) else "fail",
            "answerEvidence": {"provenancePredicateCounts": {uri: predicate_counts.get(uri, 0) for uri in sorted(provenance_predicates)}},
        },
    ]


def validate_serialization_files(
    nq_path: Path,
    nt_path: Path,
    rdfxml_path: Path,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    errors: list[dict[str, str]] = []
    metrics: dict[str, Any] = {"rdflibAvailable": False}
    try:
        root = ET.parse(rdfxml_path).getroot()
        if root.tag != f"{{{RDF}}}RDF":
            errors.append({"code": "rdfxml_root", "message": "RDF/XML root is not rdf:RDF"})
    except (OSError, ET.ParseError) as exc:
        errors.append({"code": "rdfxml_parse", "message": str(exc)})
    nq_lines = sum(1 for line in nq_path.open(encoding="utf-8") if line.strip())
    nt_lines = sum(1 for line in nt_path.open(encoding="utf-8") if line.strip())
    metrics.update({"nquadsLineCount": nq_lines, "ntriplesLineCount": nt_lines})
    try:
        from rdflib import Dataset, Graph
    except ImportError:
        metrics["parserWarning"] = "rdflib is unavailable; deterministic line/XML validation was used"
        return errors, metrics
    dataset = Dataset()
    union_nt = Graph()
    union_xml = Graph()
    try:
        dataset.parse(nq_path, format="nquads")
        union_nt.parse(nt_path, format="nt")
        union_xml.parse(rdfxml_path, format="xml")
    except Exception as exc:  # pragma: no cover - library exception classes vary
        errors.append({"code": "rdflib_parse", "message": str(exc)})
        return errors, metrics
    union_nq = {(s, p, o) for s, p, o, _g in dataset.quads((None, None, None, None))}
    nt_set = set(union_nt)
    xml_set = set(union_xml)
    metrics.update(
        {
            "rdflibAvailable": True,
            "rdflibNQuadsUnionCount": len(union_nq),
            "rdflibNTriplesCount": len(nt_set),
            "rdflibRdfXmlCount": len(xml_set),
        }
    )
    if union_nq != nt_set or union_nq != xml_set:
        errors.append({"code": "serialization_equivalence", "message": "N-Quads union, N-Triples, and RDF/XML are not semantically equal"})
    return errors, metrics


def _serialization_checks(
    graph: MaterializedGraph,
    nq_path: Path,
    nt_path: Path,
    rdfxml_path: Path,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    errors, metrics = validate_serialization_files(nq_path, nt_path, rdfxml_path)
    if metrics.get("nquadsLineCount") != graph.metrics["quadCount"]:
        errors.append({"code": "nquads_count", "message": "N-Quads line count differs from materialized quad count"})
    if metrics.get("ntriplesLineCount") != graph.metrics["tripleCount"]:
        errors.append({"code": "ntriples_count", "message": "N-Triples line count differs from union triple count"})
    return errors, metrics


def validate_candidate(
    *,
    graph: MaterializedGraph,
    contract: Stage1Contract,
    config: Stage2Config,
    nq_path: Path,
    nt_path: Path,
    rdfxml_path: Path,
    plan_hashes: dict[str, str],
) -> dict[str, Any]:
    errors, serialization = _serialization_checks(graph, nq_path, nt_path, rdfxml_path)
    source = contract.source_index["metrics"]
    profiles = contract.source_index["entityProfiles"]
    coverage = _entity_coverage(contract)
    expected = {
        "sourceDocumentCount": source["documentCount"],
        "searchResponseCount": source["documentCount"],
        "emptyResponseCount": source["emptyResponseCount"],
        "sourceRecordCount": source["recordCount"],
        "sourceFragmentCount": source["fragmentCount"],
        "rawPointerReplayCount": source["fragmentCount"],
        "paperObservationCount": profiles["paper"]["observationCount"],
        "scholarObservationCount": profiles["scholar"]["observationCount"],
        "institutionObservationCount": profiles["institution"]["observationCount"],
        "paperEntityCount": len(coverage["Paper"]["union"]),
        "scholarEntityCount": len(coverage["Scholar"]["union"]),
        "institutionEntityCount": len(coverage["Institution"]["union"]),
        "paperObservedEntityCount": profiles["paper"]["businessEntityCount"],
        "scholarObservedEntityCount": profiles["scholar"]["businessEntityCount"],
        "institutionObservedEntityCount": profiles["institution"]["businessEntityCount"],
        "paperReferenceOnlyEntityCount": len(coverage["Paper"]["union"] - coverage["Paper"]["direct"]),
        "scholarReferenceOnlyEntityCount": len(coverage["Scholar"]["union"] - coverage["Scholar"]["direct"]),
        "institutionReferenceOnlyEntityCount": len(coverage["Institution"]["union"] - coverage["Institution"]["direct"]),
        "paperScholarUniqueCount": contract.source_index["relationComparisons"]["Paper-Scholar"]["authoritativeCount"],
        "paperInstitutionUniqueCount": contract.source_index["relationComparisons"]["Paper-Institution"]["authoritativeCount"],
        "scholarInstitutionUniqueCount": contract.source_index["relationComparisons"]["Scholar-Institution"]["authoritativeCount"],
    }
    for key, value in expected.items():
        if graph.metrics.get(key) != value:
            errors.append({"code": "metric_mismatch", "message": f"{key}={graph.metrics.get(key)!r}, expected {value!r}"})
    datatype_properties = contract.ontology["datatypeProperties"]
    object_properties = contract.ontology["objectProperties"]
    classes = contract.ontology["classes"]

    def predicate_count(uri: str, *, value: str | None = None) -> int:
        return sum(
            1
            for quad in graph.quads
            if quad.predicate == uri and (value is None or quad.value == value)
        )

    def type_count(class_id: str) -> int:
        return sum(
            1
            for quad in graph.quads
            if quad.predicate == RDF + "type" and quad.value == str(classes[class_id]["uri"])
        )

    record_fragment_count = sum(bool(item.get("recordId")) for item in contract.source_index["fragments"])
    root_fragment_count = sum(item.get("jsonPointer") == "" for item in contract.source_index["fragments"])
    source_contract = {
        "sourceDocumentTypes": type_count("SourceDocument"),
        "searchResponseTypes": type_count("SearchResponse"),
        "sourceRecordTypes": type_count("SourceRecord"),
        "sourceFragmentTypes": type_count("SourceFragment"),
        "fragmentJsonPointers": predicate_count(str(datatype_properties["jsonPointer"]["uri"])),
        "rootFragmentJsonPointers": predicate_count(str(datatype_properties["jsonPointer"]["uri"]), value=""),
        "fragmentValueHashes": predicate_count(str(datatype_properties["fragmentValueHash"]["uri"])),
        "fragmentValueKinds": predicate_count(str(datatype_properties["fragmentValueKind"]["uri"])),
        "fragmentFromDocuments": predicate_count(str(object_properties["fragmentFromDocument"]["uri"])),
        "documentHasFragments": predicate_count(str(object_properties["documentHasFragment"]["uri"])),
        "responseHasFragments": predicate_count(str(object_properties["responseHasFragment"]["uri"])),
        "fragmentFromRecords": predicate_count(str(object_properties["fragmentFromRecord"]["uri"])),
        "recordHasFragments": predicate_count(str(object_properties["recordHasFragment"]["uri"])),
    }
    expected_source_contract = {
        "sourceDocumentTypes": source["documentCount"],
        "searchResponseTypes": source["documentCount"],
        "sourceRecordTypes": source["recordCount"],
        "sourceFragmentTypes": source["fragmentCount"],
        "fragmentJsonPointers": source["fragmentCount"],
        "rootFragmentJsonPointers": root_fragment_count,
        "fragmentValueHashes": source["fragmentCount"],
        "fragmentValueKinds": source["fragmentCount"],
        "fragmentFromDocuments": source["fragmentCount"],
        "documentHasFragments": source["fragmentCount"],
        "responseHasFragments": source["fragmentCount"],
        "fragmentFromRecords": record_fragment_count,
        "recordHasFragments": record_fragment_count,
    }
    for key, value in expected_source_contract.items():
        if source_contract[key] != value:
            errors.append(
                {
                    "code": "source_contract_incomplete",
                    "message": f"{key}={source_contract[key]!r}, expected {value!r}",
                }
            )
    for key in ("datatypeErrorCount", "shapeErrorCount", "silentSkipCount", "defaultedFactCount", "unsupportedFactCount"):
        if graph.metrics.get(key) != 0:
            errors.append({"code": key, "message": f"{key} must be zero, found {graph.metrics.get(key)!r}"})
    if graph.metrics.get("unresolvedReferenceCount") != 0:
        errors.append({"code": "unresolved_references", "message": "current graph contains unresolved business-ID references"})
    cqs = competency_query_results(graph, contract)
    for result in cqs:
        if result["status"] == "fail":
            errors.append({"code": "competency_query", "message": f"{result['id']} is not executable over the graph"})
    return {
        "schemaVersion": "dataelf-stage2-validation.v2",
        "validatorVersion": VALIDATION_VERSION,
        "status": "valid" if not errors else "invalid",
        "validatedAt": utc_now(),
        "errors": errors,
        "warnings": [] if serialization.get("rdflibAvailable") else [{"code": "rdflib_unavailable", "message": serialization.get("parserWarning")}],
        "metrics": graph.metrics,
        "expectedMetrics": expected,
        "sourceContract": source_contract,
        "expectedSourceContract": expected_source_contract,
        "serialization": serialization,
        "competencyQueries": cqs,
        "diagnostics": graph.diagnostics,
        "inputs": {
            "contractFingerprint": contract.contract_fingerprint,
            "sourceFingerprint": contract.source_fingerprint,
            "sourceIndexSha256": contract.source_index["sourceIndexSha256"],
            "planHashes": dict(sorted(plan_hashes.items())),
        },
    }


__all__ = [
    "VALIDATION_VERSION",
    "competency_query_results",
    "validate_candidate",
    "validate_serialization_files",
]
