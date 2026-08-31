from __future__ import annotations

import base64
import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import sha256_json
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import Stage2Config
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.contract import Stage1Contract, _pointer


RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
PROV = "http://www.w3.org/ns/prov#"
XSD = "http://www.w3.org/2001/XMLSchema#"
OWL = "http://www.w3.org/2002/07/owl#"


@dataclass(frozen=True, order=True)
class Quad:
    graph: str
    subject: str
    predicate: str
    value: str
    object_kind: str = "iri"
    datatype: str = ""


@dataclass(frozen=True)
class MaterializedGraph:
    quads: tuple[Quad, ...]
    metrics: dict[str, Any]
    diagnostics: dict[str, Any]
    projection_lineage: dict[str, Any]
    unresolved_references: list[dict[str, Any]]
    reference_only_entities: list[dict[str, Any]]

    @property
    def triples(self) -> tuple[Quad, ...]:
        seen: set[tuple[str, str, str, str, str]] = set()
        result: list[Quad] = []
        for item in self.quads:
            key = (item.subject, item.predicate, item.value, item.object_kind, item.datatype)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return tuple(result)


def _pointer_token(pointer: str) -> str:
    return base64.urlsafe_b64encode(pointer.encode("utf-8")).decode("ascii").rstrip("=")


def _business_id(value: str) -> str:
    return quote(value, safe="-._~")


def _concept_key(value: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return normalized, sha256_json(normalized)


class IriFactory:
    def __init__(self, contract: Stage1Contract) -> None:
        self.contract = contract
        self.namespace = str(contract.ontology["metadata"]["namespace"])
        self.rules = contract.grounding["iriGenerationMappings"]["classMappings"]

    def entity(self, class_id: str, business_id: str) -> str:
        slug = {"Paper": "paper", "Scholar": "scholar", "Institution": "institution"}[class_id]
        return f"{self.namespace}instance/{slug}/{_business_id(business_id)}"

    def concept(self, class_id: str, value: str) -> tuple[str, str]:
        normalized, digest = _concept_key(value)
        slug = {"Topic": "topic", "Venue": "venue", "Award": "award"}[class_id]
        return f"{self.namespace}instance/{slug}/{digest}", normalized

    def source_document(self, document_id: str) -> str:
        return f"{self.namespace}instance/source-document/{document_id}"

    def response(self, response_id: str) -> str:
        return f"{self.namespace}instance/search-response/{response_id}"

    def source_record(self, document: dict[str, Any], pointer: str) -> str:
        return (
            f"{self.namespace}instance/source-record/{document['documentId']}/{document['sha256']}/"
            f"{_pointer_token(pointer)}"
        )

    def source_fragment(self, document: dict[str, Any], pointer: str) -> str:
        return (
            f"{self.namespace}instance/source-fragment/{document['documentId']}/{document['sha256']}/"
            f"{_pointer_token(pointer)}"
        )

    def observation(self, kind: str, document: dict[str, Any], pointer: str) -> str:
        return (
            f"{self.namespace}instance/observation/{kind}/{document['documentId']}/{document['sha256']}/"
            f"{_pointer_token(pointer)}"
        )

    def authorship(self, document: dict[str, Any], record_pointer: str, index: int) -> str:
        return (
            f"{self.namespace}instance/authorship/{document['documentId']}/{document['sha256']}/"
            f"{_pointer_token(record_pointer)}/{index}"
        )

    def news(self, document: dict[str, Any], pointer: str) -> str:
        return (
            f"{self.namespace}instance/news/{document['documentId']}/{document['sha256']}/"
            f"{_pointer_token(pointer)}"
        )


def _tokens(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise ValueError(f"relative JSON path must start with '/': {path}")
    return [item.replace("~1", "/").replace("~0", "~") for item in path[1:].split("/")]


def _escape_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _select(value: Any, path: str) -> list[tuple[Any, str]]:
    states: list[tuple[Any, str]] = [(value, "")]
    for token in _tokens(path):
        next_states: list[tuple[Any, str]] = []
        for current, pointer in states:
            if token == "*" and isinstance(current, list):
                next_states.extend((child, f"{pointer}/{index}") for index, child in enumerate(current))
            elif isinstance(current, dict) and token in current:
                next_states.append((current[token], f"{pointer}/{_escape_pointer_token(token)}"))
            elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
                next_states.append((current[int(token)], f"{pointer}/{token}"))
        states = next_states
    return states


def _datatype_uri(range_id: str) -> str:
    return XSD + range_id.removeprefix("xsd:") if range_id.startswith("xsd:") else ""


def _lexical(value: Any, range_id: str) -> tuple[str, str]:
    datatype = _datatype_uri(range_id)
    if value is None or value == "":
        raise ValueError("missing")
    if range_id in {"xsd:integer", "xsd:positiveInteger", "xsd:nonNegativeInteger"}:
        if isinstance(value, bool):
            raise ValueError("boolean is not an integer metric")
        number = int(value)
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("non-integral number")
        if range_id == "xsd:positiveInteger" and number <= 0:
            raise ValueError("not positive")
        if range_id == "xsd:nonNegativeInteger" and number < 0:
            raise ValueError("negative")
        return str(number), datatype
    if range_id == "xsd:decimal":
        if isinstance(value, bool):
            raise ValueError("boolean is not a decimal metric")
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("invalid decimal") from exc
        if not number.is_finite():
            raise ValueError("non-finite decimal")
        return format(number, "f"), datatype
    if range_id == "xsd:boolean":
        if not isinstance(value, bool):
            raise ValueError("boolean qualifier must be raw/derived boolean")
        return "true" if value else "false", datatype
    text = str(value)
    if range_id == "xsd:date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("invalid xsd:date lexical value")
    if range_id == "xsd:anyURI" and not urlsplit(text).scheme:
        raise ValueError("absolute URI required")
    return text, datatype


def materialize(
    plans: dict[str, dict[str, Any]],
    contract: Stage1Contract,
    config: Stage2Config,
) -> MaterializedGraph:
    ontology = contract.ontology
    classes = ontology["classes"]
    object_properties = ontology["objectProperties"]
    datatype_properties = ontology["datatypeProperties"]
    namespace = str(ontology["metadata"]["namespace"])
    graphs = {
        "schema": namespace + "graph/schema",
        "source": namespace + "graph/source",
        "observation": namespace + "graph/observation",
        "domain": namespace + "graph/domain",
    }
    iri_factory = IriFactory(contract)
    quads: set[Quad] = set()
    datatype_errors: list[dict[str, Any]] = []
    shape_errors: list[dict[str, Any]] = []
    consensus_conflicts: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    reference_only_contexts: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    projection_events: list[dict[str, Any]] = []

    def iri(graph: str, subject: str, predicate: str, value: str) -> None:
        if subject and predicate and value:
            quads.add(Quad(graph, subject, predicate, value, "iri", ""))

    def literal(
        graph: str,
        subject: str,
        property_uri: str,
        value: Any,
        range_id: str,
        context: str,
        *,
        allow_empty: bool = False,
    ) -> None:
        try:
            if allow_empty and value == "" and range_id == "xsd:string":
                lexical, datatype = "", _datatype_uri(range_id)
            else:
                lexical, datatype = _lexical(value, range_id)
        except (TypeError, ValueError, OverflowError) as exc:
            if str(exc) != "missing":
                datatype_errors.append({"context": context, "property": property_uri, "value": value, "error": str(exc)})
            return
        quads.add(Quad(graph, subject, property_uri, lexical, "literal", datatype))

    def class_uri(class_id: str) -> str:
        return str(classes[class_id]["uri"])

    def object_uri(property_id: str) -> str:
        return str(object_properties[property_id]["uri"])

    def data_property(property_id: str) -> tuple[str, str]:
        definition = datatype_properties[property_id]
        return str(definition["uri"]), str(definition["range"])

    # Schema graph: the reviewed Stage 1 TBox is emitted with the ABox.
    ontology_node = namespace + "ontology"
    iri(graphs["schema"], ontology_node, RDF + "type", OWL + "Ontology")
    for property_id, value in ((RDFS + "label", ontology["metadata"].get("title")), (RDFS + "comment", ontology["metadata"].get("description")), (OWL + "versionInfo", ontology["metadata"].get("version"))):
        literal(graphs["schema"], ontology_node, property_id, value, "xsd:string", "ontology metadata")
    for class_id, definition in sorted(classes.items()):
        uri = str(definition["uri"])
        iri(graphs["schema"], uri, RDF + "type", OWL + "Class")
        literal(graphs["schema"], uri, RDFS + "label", definition.get("label"), "xsd:string", class_id)
        literal(graphs["schema"], uri, RDFS + "comment", definition.get("comment"), "xsd:string", class_id)
        for parent in definition.get("subClassOf", []):
            if parent in classes:
                iri(graphs["schema"], uri, RDFS + "subClassOf", class_uri(parent))
    for section, rdf_type in ((object_properties, OWL + "ObjectProperty"), (datatype_properties, OWL + "DatatypeProperty")):
        for property_id, definition in sorted(section.items()):
            uri = str(definition["uri"])
            iri(graphs["schema"], uri, RDF + "type", rdf_type)
            literal(graphs["schema"], uri, RDFS + "label", definition.get("label"), "xsd:string", property_id)
            literal(graphs["schema"], uri, RDFS + "comment", definition.get("comment"), "xsd:string", property_id)
            if definition.get("domain") in classes:
                iri(graphs["schema"], uri, RDFS + "domain", class_uri(str(definition["domain"])))
            range_id = str(definition.get("range", ""))
            if range_id in classes:
                iri(graphs["schema"], uri, RDFS + "range", class_uri(range_id))
            elif range_id.startswith("xsd:"):
                iri(graphs["schema"], uri, RDFS + "range", _datatype_uri(range_id))

    documents = {str(item["documentId"]): item for item in contract.source_index["documents"]}
    records = {str(item["recordId"]): item for item in contract.source_index["records"]}
    document_nodes: dict[str, str] = {}
    response_nodes: dict[str, str] = {}
    record_nodes: dict[str, str] = {}
    fragment_nodes: dict[tuple[str, str], str] = {}

    # Source layer is controller-owned and exhaustive, including empty responses.
    for document_id, document in sorted(documents.items()):
        document_node = iri_factory.source_document(document_id)
        response_node = iri_factory.response(str(document["responseId"]))
        document_nodes[document_id] = document_node
        response_nodes[str(document["responseId"])] = response_node
        iri(graphs["source"], document_node, RDF + "type", class_uri("SourceDocument"))
        iri(graphs["source"], response_node, RDF + "type", class_uri("SearchResponse"))
        iri(graphs["source"], response_node, object_uri("responseFromDocument"), document_node)
        for property_id, value in (
            ("sourcePath", document.get("relativeFile")),
            ("sourceSha256", document.get("sha256")),
            ("endpoint", document.get("endpoint")),
            ("method", document.get("method")),
            ("mode", document.get("mode")),
            ("traceId", document.get("traceId")),
            ("sourceSystem", "ai_index"),
        ):
            uri, range_id = data_property(property_id)
            literal(graphs["source"], document_node, uri, value, range_id, f"document:{document_id}")
        request = document.get("request") if isinstance(document.get("request"), dict) else {}
        # Current Stage 1 contracts preserve domain and sub-domain as distinct
        # request dimensions.  Older draft bundles exposed only requestTopic;
        # keep those readable without requiring the deprecated property in new
        # published contracts.
        request_dimensions = (
            ("requestDomain", request.get("domains")),
            ("requestSubDomain", request.get("sub_domains")),
        )
        emitted_request_dimension = False
        for property_id, raw_values in request_dimensions:
            if property_id not in datatype_properties:
                continue
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            uri, range_id = data_property(property_id)
            for value in values:
                literal(graphs["source"], response_node, uri, value, range_id, f"response:{document['responseId']}")
                if value not in (None, ""):
                    emitted_request_dimension = True
        if not emitted_request_dimension and "requestTopic" in datatype_properties:
            topics = request.get("sub_domains") or request.get("topics") or request.get("topic")
            topic_values = topics if isinstance(topics, list) else [topics]
            uri, range_id = data_property("requestTopic")
            for value in topic_values:
                literal(graphs["source"], response_node, uri, value, range_id, f"response:{document['responseId']}")
        for property_id, value in (
            ("sortType", request.get("sort_type")),
            ("requestPage", request.get("page")),
            ("requestSize", request.get("size")),
            ("resultCount", document.get("resultCount")),
        ):
            uri, range_id = data_property(property_id)
            literal(graphs["source"], response_node, uri, value, range_id, f"response:{document['responseId']}")

    for record_id, record in sorted(records.items()):
        document = documents[str(record["documentId"])]
        node = iri_factory.source_record(document, str(record["jsonPointer"]))
        record_nodes[record_id] = node
        iri(graphs["source"], node, RDF + "type", class_uri("SourceRecord"))
        iri(graphs["source"], node, object_uri("recordFromDocument"), document_nodes[str(record["documentId"])])
        iri(graphs["source"], response_nodes[str(record["responseId"])], object_uri("responseHasRecord"), node)
        for property_id, value in (("recordJsonPointer", record.get("jsonPointer")), ("recordHash", record.get("recordHash"))):
            uri, range_id = data_property(property_id)
            literal(graphs["source"], node, uri, value, range_id, f"record:{record_id}")

    for fragment in contract.source_index["fragments"]:
        document = documents[str(fragment["documentId"])]
        pointer = str(fragment["jsonPointer"])
        node = iri_factory.source_fragment(document, pointer)
        fragment_nodes[(str(fragment["documentId"]), pointer)] = node
        iri(graphs["source"], node, RDF + "type", class_uri("SourceFragment"))
        iri(graphs["source"], node, object_uri("fragmentFromDocument"), document_nodes[str(fragment["documentId"])])
        iri(graphs["source"], document_nodes[str(fragment["documentId"])], object_uri("documentHasFragment"), node)
        iri(graphs["source"], response_nodes[str(fragment["responseId"])], object_uri("responseHasFragment"), node)
        if fragment.get("recordId"):
            record_node = record_nodes[str(fragment["recordId"])]
            iri(graphs["source"], node, object_uri("fragmentFromRecord"), record_node)
            iri(graphs["source"], record_node, object_uri("recordHasFragment"), node)
        for property_id, value in (
            ("jsonPointer", pointer),
            ("fragmentValueKind", fragment.get("valueKind")),
            ("fragmentValueHash", fragment.get("valueHash")),
        ):
            uri, range_id = data_property(property_id)
            # RFC 6901 defines the empty string as the pointer to the complete
            # document.  It is a locator, not a missing source value.
            literal(
                graphs["source"],
                node,
                uri,
                value,
                range_id,
                f"fragment:{fragment['fragmentId']}",
                allow_empty=property_id == "jsonPointer",
            )

    entity_kind_to_class = {"paper": "Paper", "scholar": "Scholar", "institution": "Institution"}
    entity_id_property = {"Paper": "paperId", "Scholar": "scholarId", "Institution": "institutionId"}
    authoritative_ids: dict[str, set[str]] = defaultdict(set)
    for record in records.values():
        class_id = entity_kind_to_class[str(record["entityKind"])]
        authoritative_ids[class_id].add(str(record["businessId"]))
    typed_entities: set[tuple[str, str]] = set()
    consensus: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    concept_labels: dict[tuple[str, str], set[str]] = defaultdict(set)

    def ensure_entity(class_id: str, business_id: str, context: dict[str, Any] | None = None) -> str:
        node = iri_factory.entity(class_id, business_id)
        if (class_id, business_id) not in typed_entities:
            typed_entities.add((class_id, business_id))
            iri(graphs["domain"], node, RDF + "type", class_uri(class_id))
            property_id = entity_id_property[class_id]
            uri, range_id = data_property(property_id)
            literal(graphs["domain"], node, uri, business_id, range_id, f"identity:{class_id}:{business_id}")
        if business_id not in authoritative_ids[class_id] and context is not None:
            # Search endpoints are independently paginated. A paper in the
            # first page can therefore reference a scholar or institution
            # outside that entity endpoint's first page. The raw JSON pointer
            # makes this a grounded reference-only entity, not an unresolved
            # reference. Keep every distinct source locator for auditability.
            source = dict(context)
            reference_only_contexts[(class_id, business_id)][json.dumps(source, sort_keys=True)] = source
        return node

    for record_id, record in sorted(records.items()):
        document = documents[str(record["documentId"])]
        envelope = contract.raw_documents[str(record["documentId"])]
        raw_record = _pointer(envelope, str(record["jsonPointer"]))
        if not isinstance(raw_record, dict):
            shape_errors.append({"recordId": record_id, "error": "record pointer did not resolve to an object"})
            continue
        endpoint = str(document["endpoint"])
        plan = plans[endpoint]
        entity_config = plan["entity"]
        class_id = str(entity_config["classId"])
        observation_class = str(entity_config["observationClassId"])
        business_id = str(record["businessId"])
        entity_node = ensure_entity(class_id, business_id)
        kind = str(record["entityKind"])
        observation_node = iri_factory.observation(kind, document, str(record["jsonPointer"]))
        iri(graphs["observation"], observation_node, RDF + "type", class_uri(observation_class))
        iri(graphs["observation"], observation_node, object_uri(str(entity_config["genericObservedEntityPropertyId"])), entity_node)
        iri(graphs["observation"], observation_node, object_uri(str(entity_config["observedEntityPropertyId"])), entity_node)
        iri(graphs["observation"], observation_node, object_uri("observationFromRecord"), record_nodes[record_id])
        iri(graphs["observation"], observation_node, object_uri("observationInResponse"), response_nodes[str(record["responseId"])])
        for property_id, value in (
            ("resultRank", record.get("resultRank")),
            (str(entity_config["sourceRawPropertyId"]), document.get("relativeFile")),
        ):
            uri, range_id = data_property(property_id)
            literal(graphs["observation"], observation_node, uri, value, range_id, f"observation:{record_id}")

        for operation in plan["operations"]:
            op = str(operation["op"])
            if op == "authority_projection":
                projection_events.append(
                    {
                        "coverageKey": operation["coverageKey"],
                        "relationKey": operation["relationKey"],
                        "endpoint": endpoint,
                        "policy": operation["differenceStrategy"],
                    }
                )
                continue
            selected = _select(raw_record, str(operation.get("sourcePath", "")))
            if op == "copy_scalar":
                property_id = str(operation["propertyId"])
                uri, range_id = data_property(property_id)
                for value, _relative_pointer in selected:
                    if isinstance(value, (dict, list)):
                        shape_errors.append({"recordId": record_id, "operation": operation["id"], "error": "copy_scalar selected a container"})
                    else:
                        literal(graphs["observation"], observation_node, uri, value, range_id, f"{record_id}:{operation['id']}")
            elif op == "require_consensus":
                property_id = str(operation["propertyId"])
                uri, range_id = data_property(property_id)
                for value, _relative_pointer in selected:
                    if value is None or value == "" or isinstance(value, (dict, list)):
                        continue
                    try:
                        consensus[(entity_node, property_id)].append(_lexical(value, range_id))
                    except (TypeError, ValueError, OverflowError) as exc:
                        datatype_errors.append({"context": f"{record_id}:{operation['id']}", "property": uri, "value": value, "error": str(exc)})
            elif op == "concept_by_normalized_value":
                target_class = str(operation["targetClassId"])
                label_property = str(operation["labelPropertyId"])
                observation_property = object_uri(str(operation["observationPropertyId"]))
                shortcut = operation.get("domainShortcutPropertyId")
                for value, relative_pointer in selected:
                    if not isinstance(value, str) or not value.strip():
                        shape_errors.append({"recordId": record_id, "operation": operation["id"], "error": "concept path selected non-string"})
                        continue
                    target, normalized = iri_factory.concept(target_class, value)
                    iri(graphs["domain"], target, RDF + "type", class_uri(target_class))
                    concept_labels[(target_class, target)].add(value)
                    iri(graphs["observation"], observation_node, observation_property, target)
                    if shortcut:
                        iri(graphs["domain"], entity_node, object_uri(str(shortcut)), target)
                    fragment = fragment_nodes.get((str(record["documentId"]), str(record["jsonPointer"]) + relative_pointer))
                    if fragment:
                        iri(graphs["domain"], target, PROV + "wasDerivedFrom", fragment)
                    projection_events.append({"coverageKey": operation["coverageKey"], "recordId": record_id, "normalizedKey": normalized})
            elif op == "reference_by_business_id":
                target_class = str(operation["targetClassId"])
                observation_property = object_uri(str(operation["observationPropertyId"]))
                shortcut = operation.get("domainShortcutPropertyId")
                for value, relative_pointer in selected:
                    if not isinstance(value, str) or not value:
                        shape_errors.append({"recordId": record_id, "operation": operation["id"], "error": "business reference is not a nonempty string"})
                        continue
                    absolute_pointer = str(record["jsonPointer"]) + relative_pointer
                    target = ensure_entity(
                        target_class,
                        value,
                        {
                            "sourceDocumentId": record["documentId"],
                            "jsonPointer": absolute_pointer,
                            "operationId": operation["id"],
                            "sourcePath": operation.get("sourcePath"),
                            "identityRole": "verbatim_business_id_reference",
                        },
                    )
                    iri(graphs["observation"], observation_node, observation_property, target)
                    if shortcut:
                        iri(graphs["domain"], entity_node, object_uri(str(shortcut)), target)
            elif op == "object_fields":
                observation_property = object_uri(str(operation["observationPropertyId"]))
                shortcut = operation.get("domainShortcutPropertyId")
                for value, relative_pointer in selected:
                    if not isinstance(value, dict):
                        shape_errors.append({"recordId": record_id, "operation": operation["id"], "error": "object_fields selected non-object"})
                        continue
                    absolute_pointer = str(record["jsonPointer"]) + relative_pointer
                    target = iri_factory.news(document, absolute_pointer)
                    iri(graphs["domain"], target, RDF + "type", class_uri(str(operation["targetClassId"])))
                    iri(graphs["observation"], observation_node, observation_property, target)
                    if shortcut:
                        iri(graphs["domain"], entity_node, object_uri(str(shortcut)), target)
                    for field, property_id in operation["fieldProperties"].items():
                        uri, range_id = data_property(str(property_id))
                        literal(graphs["domain"], target, uri, value.get(field), range_id, f"{record_id}:{absolute_pointer}/{field}")
                    fragment = fragment_nodes.get((str(record["documentId"]), absolute_pointer))
                    if fragment:
                        # New Stage 1 contracts expose an explicit executable
                        # NewsItem provenance edge.  Keep draft/legacy bundles
                        # readable while they transition to the new contract.
                        if "newsItemFromFragment" in object_properties:
                            iri(graphs["domain"], target, object_uri("newsItemFromFragment"), fragment)
                        iri(graphs["domain"], target, PROV + "wasDerivedFrom", fragment)
            elif op == "reify_array_membership":
                observation_property = object_uri(str(operation["observationPropertyId"]))
                for value, relative_pointer in selected:
                    if not isinstance(value, str) or not value or not relative_pointer.rsplit("/", 1)[-1].isdigit():
                        shape_errors.append({"recordId": record_id, "operation": operation["id"], "error": "membership path did not select an indexed business ID"})
                        continue
                    index = int(relative_pointer.rsplit("/", 1)[-1])
                    absolute_pointer = str(record["jsonPointer"]) + relative_pointer
                    target = ensure_entity(
                        str(operation["memberClassId"]),
                        value,
                        {
                            "sourceDocumentId": record["documentId"],
                            "jsonPointer": absolute_pointer,
                            "operationId": operation["id"],
                            "sourcePath": operation.get("sourcePath"),
                            "identityRole": "verbatim_business_id_reference",
                        },
                    )
                    membership = iri_factory.authorship(document, str(record["jsonPointer"]), index)
                    iri(graphs["observation"], membership, RDF + "type", class_uri(str(operation["membershipClassId"])))
                    iri(graphs["observation"], observation_node, observation_property, membership)
                    if operation.get("inverseObservationPropertyId"):
                        iri(graphs["observation"], membership, object_uri(str(operation["inverseObservationPropertyId"])), observation_node)
                    iri(graphs["observation"], membership, object_uri(str(operation["paperPropertyId"])), entity_node)
                    iri(graphs["observation"], membership, object_uri(str(operation["memberPropertyId"])), target)
                    iri(graphs["domain"], entity_node, object_uri(str(operation["shortcutPropertyId"])), target)
                    iri(graphs["domain"], entity_node, object_uri(str(operation["entityMembershipPropertyId"])), membership)
                    qualifiers = operation.get("qualifiers", {})
                    for qualifier, definition in qualifiers.items():
                        property_id = str(definition["propertyId"])
                        derived = index + 1 if qualifier == "authorOrder" else index == 0
                        uri, range_id = data_property(property_id)
                        literal(graphs["observation"], membership, uri, derived, range_id, f"{record_id}:{operation['id']}:{qualifier}")
                    fragment = fragment_nodes.get((str(record["documentId"]), absolute_pointer))
                    if fragment:
                        iri(graphs["observation"], membership, PROV + "wasDerivedFrom", fragment)
            elif op in {"explode_array", "derived_formula"}:
                shape_errors.append({"recordId": record_id, "operation": operation["id"], "error": f"{op} has no executable contract in this endpoint plan"})

    # Stable fields are projected only when every observed nonempty lexical value agrees.
    for (entity_node, property_id), values in sorted(consensus.items()):
        unique = sorted(set(values))
        if len(unique) == 1:
            uri, _range_id = data_property(property_id)
            lexical, datatype = unique[0]
            quads.add(Quad(graphs["domain"], entity_node, uri, lexical, "literal", datatype))
            projection_events.append({"policy": "require_consensus", "entity": entity_node, "propertyId": property_id, "status": "projected", "observationCount": len(values)})
        elif len(unique) > 1:
            consensus_conflicts.append({"entity": entity_node, "propertyId": property_id, "values": [value for value, _ in unique]})
            projection_events.append({"policy": "require_consensus", "entity": entity_node, "propertyId": property_id, "status": "observation_only_conflict", "observationCount": len(values)})
    for (class_id, node), labels in sorted(concept_labels.items()):
        if len(labels) == 1:
            property_id = {"Topic": "topicName", "Venue": "venueName", "Award": "awardName"}[class_id]
            uri, range_id = data_property(property_id)
            literal(graphs["domain"], node, uri, next(iter(labels)), range_id, f"concept:{node}")
        else:
            consensus_conflicts.append({"entity": node, "propertyId": "displayLabel", "values": sorted(labels)})

    ordered = tuple(sorted(quads))
    type_counts: Counter[str] = Counter(item.value for item in ordered if item.predicate == RDF + "type" and item.object_kind == "iri")
    predicate_counts: Counter[str] = Counter(item.predicate for item in ordered)
    graph_counts: Counter[str] = Counter(item.graph for item in ordered)
    reference_only_entities = [
        {
            "targetClassId": class_id,
            "businessId": business_id,
            "referenceCount": len(sources),
            "sources": [sources[key] for key in sorted(sources)],
        }
        for (class_id, business_id), sources in sorted(reference_only_contexts.items())
    ]
    reference_only_counts = Counter(item["targetClassId"] for item in reference_only_entities)
    observed_counts = {class_id: len(ids) for class_id, ids in authoritative_ids.items()}
    metrics = {
        "quadCount": len(ordered),
        "tripleCount": len({(q.subject, q.predicate, q.value, q.object_kind, q.datatype) for q in ordered}),
        "graphCounts": dict(sorted(graph_counts.items())),
        "sourceDocumentCount": type_counts[class_uri("SourceDocument")],
        "searchResponseCount": type_counts[class_uri("SearchResponse")],
        "emptyResponseCount": contract.source_index["metrics"]["emptyResponseCount"],
        "sourceRecordCount": type_counts[class_uri("SourceRecord")],
        "sourceFragmentCount": type_counts[class_uri("SourceFragment")],
        "paperObservationCount": type_counts[class_uri("PaperObservation")],
        "scholarObservationCount": type_counts[class_uri("ScholarObservation")],
        "institutionObservationCount": type_counts[class_uri("InstitutionObservation")],
        "paperEntityCount": type_counts[class_uri("Paper")],
        "scholarEntityCount": type_counts[class_uri("Scholar")],
        "institutionEntityCount": type_counts[class_uri("Institution")],
        "paperObservedEntityCount": observed_counts.get("Paper", 0),
        "scholarObservedEntityCount": observed_counts.get("Scholar", 0),
        "institutionObservedEntityCount": observed_counts.get("Institution", 0),
        "paperReferenceOnlyEntityCount": reference_only_counts["Paper"],
        "scholarReferenceOnlyEntityCount": reference_only_counts["Scholar"],
        "institutionReferenceOnlyEntityCount": reference_only_counts["Institution"],
        "referenceOnlyEntityCount": len(reference_only_entities),
        "referenceOnlySourceCount": sum(item["referenceCount"] for item in reference_only_entities),
        "authorshipObservationCount": type_counts[class_uri("Authorship")],
        "paperScholarUniqueCount": predicate_counts[object_uri("authoredBy")],
        "paperInstitutionObservationCount": predicate_counts[object_uri("paperObservationHasInstitution")],
        "paperInstitutionUniqueCount": predicate_counts[object_uri("affiliatedWithInstitution")],
        "scholarInstitutionObservationCount": predicate_counts[object_uri("scholarObservationHasInstitution")],
        "scholarInstitutionUniqueCount": predicate_counts[object_uri("scholarAffiliatedWithInstitution")],
        "rawPointerReplayCount": len(fragment_nodes),
        "unresolvedReferenceCount": len(unresolved),
        "datatypeErrorCount": len(datatype_errors),
        "shapeErrorCount": len(shape_errors),
        "consensusConflictCount": len(consensus_conflicts),
        "silentSkipCount": 0,
        "defaultedFactCount": 0,
        "unsupportedFactCount": len(shape_errors),
    }
    diagnostics = {
        "datatypeErrors": datatype_errors,
        "shapeErrors": shape_errors,
        "consensusConflicts": consensus_conflicts,
    }
    lineage = {
        "schemaVersion": "dataelf-stage2-projection-lineage.v2",
        "contractFingerprint": contract.contract_fingerprint,
        "events": projection_events,
    }
    return MaterializedGraph(
        ordered,
        metrics,
        diagnostics,
        lineage,
        sorted(unresolved, key=lambda item: json.dumps(item, sort_keys=True)),
        reference_only_entities,
    )


def _escape_literal(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\b", "\\b")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\f", "\\f")
        .replace('"', '\\"')
    )


def _term(item: Quad) -> str:
    if item.object_kind == "iri":
        return f"<{item.value}>"
    result = f'"{_escape_literal(item.value)}"'
    return result + (f"^^<{item.datatype}>" if item.datatype else "")


def nquads(graph: MaterializedGraph) -> str:
    return "".join(
        f"<{item.subject}> <{item.predicate}> {_term(item)} <{item.graph}> .\n" for item in graph.quads
    )


def ntriples(graph: MaterializedGraph) -> str:
    return "".join(f"<{item.subject}> <{item.predicate}> {_term(item)} .\n" for item in graph.triples)


def _qname(uri: str, ontology_namespace: str, vocabulary: str) -> str:
    for namespace in (RDF, RDFS, PROV, XSD, OWL, ontology_namespace, vocabulary):
        if uri.startswith(namespace):
            local = uri.removeprefix(namespace)
            if local and re.fullmatch(r"[A-Za-z_][A-Za-z0-9._-]*", local):
                return f"{{{namespace}}}{local}"
    raise ValueError(f"RDF/XML predicate URI cannot be converted to a safe QName: {uri}")


def rdfxml(graph: MaterializedGraph, ontology_namespace: str, vocabulary: str) -> bytes:
    for prefix, namespace in (("rdf", RDF), ("rdfs", RDFS), ("prov", PROV), ("xsd", XSD), ("owl", OWL), ("ai", ontology_namespace), ("d2", vocabulary)):
        ET.register_namespace(prefix, namespace)
    root = ET.Element(ET.QName(RDF, "RDF"))
    grouped: dict[str, list[Quad]] = defaultdict(list)
    for item in graph.triples:
        grouped[item.subject].append(item)
    for subject in sorted(grouped):
        description = ET.SubElement(root, ET.QName(RDF, "Description"), {ET.QName(RDF, "about"): subject})
        for item in sorted(grouped[subject]):
            element = ET.SubElement(description, _qname(item.predicate, ontology_namespace, vocabulary))
            if item.object_kind == "iri":
                element.set(ET.QName(RDF, "resource"), item.value)
            else:
                if item.datatype:
                    element.set(ET.QName(RDF, "datatype"), item.datatype)
                element.text = item.value
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


__all__ = [
    "MaterializedGraph",
    "Quad",
    "IriFactory",
    "materialize",
    "nquads",
    "ntriples",
    "rdfxml",
]
