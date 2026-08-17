from __future__ import annotations

from copy import deepcopy
from typing import Any

from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.source import evidence_result
from dataelf.domains.ai_index.modeling import stage1_prompts
from dataelf.domains.ai_index.modeling import stage1_shacl, stage1_validation


AI_INDEX_SOURCE_ENDPOINT_TARGETS = {
    "/openapi/paper/search": ("Paper", "PaperObservation"),
    "/openapi/scholar/search": ("Scholar", "ScholarObservation"),
    "/openapi/institutions/search": ("Institution", "InstitutionObservation"),
}


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _array(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _refs(value: Any) -> list[str]:
    return list(dict.fromkeys(item for item in _array(value) if isinstance(item, str)))


def _comment(item: dict[str, Any], fallback: str) -> str:
    return str(item.get("comment") or item.get("description") or item.get("note") or fallback)


_OBJECT_PROPERTY_RENAMES = {
    "responseClassId": "observationInResponse",
    # Model-generated names that have the exact domain/range semantics required
    # by the controller-owned Authorship contract.
    "authorshipPaper": "authorshipOfPaper",
    "authorshipScholar": "authoredByScholar",
    # The Source layer is a controller-owned contract.  Models often choose
    # these equally clear English variants, while the deterministic grounding
    # and Stage 2 extractor intentionally use one stable vocabulary.
    "observedInRecord": "observationFromRecord",
    "responseToDocument": "responseFromDocument",
    "recordOfDocument": "recordFromDocument",
    "fragmentOfRecord": "fragmentFromRecord",
    "fragmentOfDocument": "fragmentFromDocument",
    "scholarAffiliatedWith": "scholarAffiliatedWithInstitution",
    "documentFragment": "documentHasFragment",
    "fragmentDocument": "fragmentFromDocument",
    "fragmentRecord": "fragmentFromRecord",
    "observationOfRecord": "observationFromRecord",
    "recordDocument": "recordFromDocument",
    "responseDocument": "responseFromDocument",
    "responseFragment": "responseHasFragment",
    "responseRecord": "responseHasRecord",
    # Full generators sometimes describe the same stable relations with a
    # noun-first label.  The controller contract has one canonical identifier
    # for each of these shapes, so retaining both only creates duplicate raw
    # bindings and needlessly consumes a repair round.
    "observedEntity": "observesEntity",
    "sourceRecord": "observationFromRecord",
    "responseRecords": "responseHasRecord",
    "responseFragments": "responseHasFragment",
    "documentFragments": "documentHasFragment",
    "recordFragments": "recordHasFragment",
    "hasScholar": "authoredByScholar",
    "hasPaper": "authorshipOfPaper",
}

_DATATYPE_PROPERTY_RENAMES = {
    # The v2 source-navigation contract intentionally exposes one canonical
    # fragment locator name.  Some models produce this unambiguous longer name.
    "fragmentJsonPointer": "jsonPointer",
    # Canonical request/provenance names used by source bindings.  Keeping the
    # aliases as separate properties would promote one raw path twice, which
    # the v2 contract correctly rejects as ambiguous semantics.
    "requestDomains": "requestDomain",
    "requestSubDomains": "requestSubDomain",
    "requestSortType": "sortType",
    "source": "sourceSystem",
    # Domain-value aliases observed from the full Pi generator.  Both aliases
    # bind exactly the same raw scalar as the canonical v2 property; retaining
    # both would create two competing meanings for one JSON value.
    "paperAbstract": "abstract",
    "paperVenue": "venueName",
    "venueKey": "venueName",
    "awardKey": "awardName",
    "awardTitle": "awardName",
    "requestSubTopic": "requestSubDomain",
    "httpMethod": "method",
    "apiMode": "mode",
    "name": "scholarName",
    "previousHotnessHalfYear": "hotnessPreviousHalfYear",
}


_CANONICAL_OBJECT_PROPERTY_SHAPES: dict[str, tuple[str, str]] = {
    "authoredBy": ("Paper", "Scholar"),
    "hasAuthorship": ("Paper", "Authorship"),
    "authorshipOfPaper": ("Authorship", "Paper"),
    "authoredByScholar": ("Authorship", "Scholar"),
    "affiliatedWithInstitution": ("Paper", "Institution"),
    "scholarAffiliatedWithInstitution": ("Scholar", "Institution"),
    "hasTopic": ("DomainEntity", "Topic"),
    "hasVenue": ("DomainEntity", "Venue"),
    "hasAward": ("DomainEntity", "Award"),
    "hasNewsItem": ("Institution", "NewsItem"),
    "hasRelatedPaper": ("Institution", "Paper"),
    "hasRelatedScholar": ("Institution", "Scholar"),
    "observesEntity": ("EntityObservation", "DomainEntity"),
    "observationFromRecord": ("EntityObservation", "SourceRecord"),
    "observationInResponse": ("EntityObservation", "SearchResponse"),
    "responseFromDocument": ("SearchResponse", "SourceDocument"),
    "responseHasRecord": ("SearchResponse", "SourceRecord"),
    "responseHasFragment": ("SearchResponse", "SourceFragment"),
    "recordFromDocument": ("SourceRecord", "SourceDocument"),
    "recordHasFragment": ("SourceRecord", "SourceFragment"),
    "fragmentFromRecord": ("SourceFragment", "SourceRecord"),
    "documentHasFragment": ("SourceDocument", "SourceFragment"),
    "fragmentFromDocument": ("SourceFragment", "SourceDocument"),
}


_CANONICAL_DATATYPE_PROPERTY_SHAPES: dict[str, tuple[str, str]] = {
    "paperId": ("Paper", "xsd:string"),
    "title": ("Paper", "xsd:string"),
    "abstract": ("Paper", "xsd:string"),
    "publishedAt": ("Paper", "xsd:date"),
    "venueName": ("Venue", "xsd:string"),
    "scholarId": ("Scholar", "xsd:string"),
    "scholarName": ("Scholar", "xsd:string"),
    "homepage": ("Scholar", "xsd:anyURI"),
    "email": ("Scholar", "xsd:string"),
    "institutionId": ("Institution", "xsd:string"),
    "institutionName": ("Institution", "xsd:string"),
    "country": ("Institution", "xsd:string"),
    "region": ("Institution", "xsd:string"),
    "topicName": ("Topic", "xsd:string"),
    "awardName": ("Award", "xsd:string"),
    "newsTitle": ("NewsItem", "xsd:string"),
    "newsSource": ("NewsItem", "xsd:string"),
    "newsDate": ("NewsItem", "xsd:date"),
    "authorOrder": ("Authorship", "xsd:positiveInteger"),
    "isFirstAuthor": ("Authorship", "xsd:boolean"),
    "citationCount": ("PaperObservation", "xsd:integer"),
    "scholarPaperCount": ("ScholarObservation", "xsd:integer"),
    "resultRank": ("EntityObservation", "xsd:positiveInteger"),
    "hotnessDay": ("EntityObservation", "xsd:integer"),
    "hotnessWeek": ("EntityObservation", "xsd:integer"),
    "hotnessMonth": ("EntityObservation", "xsd:integer"),
    "hotnessHalfYear": ("EntityObservation", "xsd:integer"),
    "hotnessPreviousHalfYear": ("EntityObservation", "xsd:integer"),
}


_STABLE_OBSERVATION_VALUES: dict[str, tuple[str, str, str, str, str]] = {
    # observed property -> canonical property, observation class, entity class, endpoint, raw path
    "observedPaperTitle": ("title", "PaperObservation", "Paper", "/openapi/paper/search", "/data/list/*/title"),
    "observedPaperAbstract": ("abstract", "PaperObservation", "Paper", "/openapi/paper/search", "/data/list/*/abstract"),
    "observedPaperPublishedAt": ("publishedAt", "PaperObservation", "Paper", "/openapi/paper/search", "/data/list/*/published_at"),
    "observedScholarName": ("scholarName", "ScholarObservation", "Scholar", "/openapi/scholar/search", "/data/list/*/name"),
    "observedScholarHomepage": ("homepage", "ScholarObservation", "Scholar", "/openapi/scholar/search", "/data/list/*/homepage"),
    "observedScholarEmail": ("email", "ScholarObservation", "Scholar", "/openapi/scholar/search", "/data/list/*/email"),
    "observedInstitutionName": ("institutionName", "InstitutionObservation", "Institution", "/openapi/institutions/search", "/data/list/*/name"),
    "observedInstitutionCountry": ("country", "InstitutionObservation", "Institution", "/openapi/institutions/search", "/data/list/*/country"),
    "observedInstitutionRegion": ("region", "InstitutionObservation", "Institution", "/openapi/institutions/search", "/data/list/*/region"),
}


_RELATION_SNAPSHOTS: dict[str, tuple[str, str, str, str, str]] = {
    # observation property -> observation class, target class, domain shortcut, endpoint, raw path
    "observationHasTopic": ("EntityObservation", "Topic", "hasTopic", "*", "/data/list/*/fields/*"),
    "observationHasVenue": ("EntityObservation", "Venue", "hasVenue", "*", "/data/list/*/venue"),
    "observationHasAward": ("EntityObservation", "Award", "hasAward", "*", "/data/list/*/awards/*"),
    "paperObservationHasInstitution": ("PaperObservation", "Institution", "affiliatedWithInstitution", "/openapi/paper/search", "/data/list/*/institution_ids/*"),
    "scholarObservationHasInstitution": ("ScholarObservation", "Institution", "scholarAffiliatedWithInstitution", "/openapi/scholar/search", "/data/list/*/institution_ids/*"),
    "scholarObservationHasPaper": ("ScholarObservation", "Paper", "", "/openapi/scholar/search", "/data/list/*/paper_ids/*"),
    "institutionObservationHasNewsItem": ("InstitutionObservation", "NewsItem", "hasNewsItem", "/openapi/institutions/search", "/data/list/*/news/*"),
    "institutionObservationHasRelatedPaper": ("InstitutionObservation", "Paper", "hasRelatedPaper", "/openapi/institutions/search", "/data/list/*/related_paper_ids/*"),
    "institutionObservationHasRelatedScholar": ("InstitutionObservation", "Scholar", "hasRelatedScholar", "/openapi/institutions/search", "/data/list/*/related_scholar_ids/*"),
}

_FORBIDDEN_PROPERTY_SUFFIXES = (
    "citedbycount",
    "institutionauthorcount",
    "iscorrespondingauthor",
    "paperinstitutionisprimary",
    "scholarinstitutionisprimary",
)


def candidate_from_semantic_plan(
    plan: dict[str, Any], config: Stage1Config, source_fingerprint: str
) -> dict[str, Any]:
    """Materialize the canonical AI Index semantic core from a compact model plan.

    The raw-backed v2 grounding is controller-owned and is still produced by
    :func:`normalize_candidate_contract`.  This compact input is used by model
    transports that are reliable for small tool calls but impractical for the
    hundreds of kilobytes of deterministic grounding maps.
    """

    required_policies = {
        "fundingEventPolicy": "omit_without_raw_instances",
        "stableValuePolicy": "require_consensus",
        "mutableMetricLayer": "observation",
        "relationshipArrayPolicy": "observation_snapshot_before_projection",
    }
    for field, expected in required_policies.items():
        if plan.get(field) != expected:
            raise ValueError(f"semantic plan {field} must be {expected}")

    namespace = config.ontology.namespace
    class_specs: dict[str, tuple[str, list[str]]] = {
        "DomainEntity": ("entity", []),
        "Paper": ("entity", ["DomainEntity"]),
        "Scholar": ("entity", ["DomainEntity"]),
        "Institution": ("entity", ["DomainEntity"]),
        "Topic": ("concept", []),
        "Venue": ("concept", []),
        "Award": ("concept", []),
        "NewsItem": ("concept", []),
        "Authorship": ("association", []),
        "EntityObservation": ("observation", []),
        "PaperObservation": ("observation", ["EntityObservation"]),
        "ScholarObservation": ("observation", ["EntityObservation"]),
        "InstitutionObservation": ("observation", ["EntityObservation"]),
        "SourceDocument": ("provenance", []),
        "SearchResponse": ("provenance", []),
        "SourceRecord": ("provenance", []),
        "SourceFragment": ("provenance", []),
    }
    class_comments = {
        "Paper": str(plan.get("paperSemantics") or "A paper identified by the AI Index business identifier."),
        "Scholar": str(plan.get("scholarSemantics") or "A scholar identified by the AI Index business identifier."),
        "Institution": str(plan.get("institutionSemantics") or "An institution identified by the AI Index business identifier."),
        "Authorship": str(plan.get("authorshipSemantics") or "A source-specific ordered authorship assertion."),
        "SourceDocument": str(plan.get("provenanceSemantics") or "A replayable raw AI Index response document."),
    }
    classes: dict[str, Any] = {}
    for identifier, (kind, parents) in class_specs.items():
        value: dict[str, Any] = {
            "uri": namespace + identifier,
            "label": identifier,
            "comment": class_comments.get(identifier, f"AI Index {identifier} semantic class."),
            "kind": kind,
        }
        if parents:
            value["subClassOf"] = parents
        classes[identifier] = value

    object_specs = {
        "authoredBy": ("Paper", "Scholar"),
        "hasAuthorship": ("Paper", "Authorship"),
        "authorshipOfPaper": ("Authorship", "Paper"),
        "authoredByScholar": ("Authorship", "Scholar"),
        "affiliatedWithInstitution": ("Paper", "Institution"),
        "scholarAffiliatedWithInstitution": ("Scholar", "Institution"),
        "hasTopic": ("DomainEntity", "Topic"),
        "hasVenue": ("DomainEntity", "Venue"),
        "hasAward": ("DomainEntity", "Award"),
        "hasNewsItem": ("Institution", "NewsItem"),
        "hasRelatedPaper": ("Institution", "Paper"),
        "hasRelatedScholar": ("Institution", "Scholar"),
        "observesEntity": ("EntityObservation", "DomainEntity"),
        "observationFromRecord": ("EntityObservation", "SourceRecord"),
        "observationInResponse": ("EntityObservation", "SearchResponse"),
        "responseFromDocument": ("SearchResponse", "SourceDocument"),
        "responseHasRecord": ("SearchResponse", "SourceRecord"),
        "responseHasFragment": ("SearchResponse", "SourceFragment"),
        "recordFromDocument": ("SourceRecord", "SourceDocument"),
        "recordHasFragment": ("SourceRecord", "SourceFragment"),
        "fragmentFromRecord": ("SourceFragment", "SourceRecord"),
        "fragmentFromDocument": ("SourceFragment", "SourceDocument"),
    }
    object_properties = {
        identifier: {
            "uri": namespace + identifier,
            "label": identifier,
            "comment": f"AI Index relationship {identifier}.",
            "domain": domain,
            "range": range_id,
        }
        for identifier, (domain, range_id) in object_specs.items()
    }

    datatype_specs = {
        "paperId": ("Paper", "xsd:string"),
        "title": ("Paper", "xsd:string"),
        "abstract": ("Paper", "xsd:string"),
        "publishedAt": ("Paper", "xsd:date"),
        "venueName": ("Venue", "xsd:string"),
        "scholarId": ("Scholar", "xsd:string"),
        "scholarName": ("Scholar", "xsd:string"),
        "homepage": ("Scholar", "xsd:anyURI"),
        "email": ("Scholar", "xsd:string"),
        "institutionId": ("Institution", "xsd:string"),
        "institutionName": ("Institution", "xsd:string"),
        "country": ("Institution", "xsd:string"),
        "region": ("Institution", "xsd:string"),
        "topicName": ("Topic", "xsd:string"),
        "awardName": ("Award", "xsd:string"),
        "newsTitle": ("NewsItem", "xsd:string"),
        "newsSource": ("NewsItem", "xsd:string"),
        "newsDate": ("NewsItem", "xsd:date"),
        "authorOrder": ("Authorship", "xsd:positiveInteger"),
        "isFirstAuthor": ("Authorship", "xsd:boolean"),
        "citationCount": ("PaperObservation", "xsd:integer"),
        "resultRank": ("EntityObservation", "xsd:positiveInteger"),
        "recordHash": ("SourceRecord", "xsd:string"),
        "sourceSha256": ("SourceDocument", "xsd:string"),
        "endpoint": ("SourceDocument", "xsd:string"),
        "method": ("SourceDocument", "xsd:string"),
        "mode": ("SourceDocument", "xsd:string"),
        "traceId": ("SourceDocument", "xsd:string"),
        "requestTopic": ("SearchResponse", "xsd:string"),
        "sortType": ("SearchResponse", "xsd:string"),
        "requestPage": ("SearchResponse", "xsd:integer"),
        "requestSize": ("SearchResponse", "xsd:integer"),
        "resultCount": ("SearchResponse", "xsd:integer"),
        "sourcePath": ("SourceDocument", "xsd:string"),
        "jsonPointer": ("SourceFragment", "xsd:string"),
        "paperCount": ("InstitutionObservation", "xsd:integer"),
        "scholarCount": ("InstitutionObservation", "xsd:integer"),
        "fundingTotalUsd": ("InstitutionObservation", "xsd:integer"),
        "impactFunding": ("InstitutionObservation", "xsd:decimal"),
        "impactIndustry": ("InstitutionObservation", "xsd:decimal"),
        "impactNews": ("InstitutionObservation", "xsd:decimal"),
        "impactPaper": ("InstitutionObservation", "xsd:decimal"),
        "impactTalent": ("InstitutionObservation", "xsd:decimal"),
        "hotnessDay": ("EntityObservation", "xsd:integer"),
        "hotnessWeek": ("EntityObservation", "xsd:integer"),
        "hotnessMonth": ("EntityObservation", "xsd:integer"),
        "hotnessHalfYear": ("EntityObservation", "xsd:integer"),
        "hotnessPreviousHalfYear": ("EntityObservation", "xsd:integer"),
    }
    datatype_properties = {
        identifier: {
            "uri": namespace + identifier,
            "label": identifier,
            "comment": f"AI Index value {identifier}.",
            "domain": domain,
            "range": range_id,
        }
        for identifier, (domain, range_id) in datatype_specs.items()
    }
    return {
        "ontology": {
            "schemaVersion": "dataelf-ontology.v2",
            "metadata": {
                "id": config.ontology.ontology_id,
                "namespace": namespace,
                "title": config.ontology.title,
                "description": str(plan.get("description") or "Three-layer raw-grounded AI Index ontology."),
                "version": "2.1.0",
                "sourceFingerprint": source_fingerprint,
            },
            "classes": classes,
            "objectProperties": object_properties,
            "datatypeProperties": datatype_properties,
        },
        "grounding": {},
    }


def _ensure_rdf_ready_ontology(ontology: dict[str, Any], namespace: str) -> None:
    """Add controller-owned Source, observation snapshot and OWL relation contracts."""

    classes = _object(ontology.get("classes"))
    objects = _object(ontology.get("objectProperties"))
    datatypes = _object(ontology.get("datatypeProperties"))

    for child, parent in (
        ("Paper", "DomainEntity"),
        ("Scholar", "DomainEntity"),
        ("Institution", "DomainEntity"),
        ("PaperObservation", "EntityObservation"),
        ("ScholarObservation", "EntityObservation"),
        ("InstitutionObservation", "EntityObservation"),
    ):
        if child not in classes or parent not in classes:
            continue
        parents = [item for item in _array(_object(classes[child]).get("subClassOf")) if isinstance(item, str)]
        if parent not in parents:
            parents.append(parent)
        classes[child]["subClassOf"] = parents

    def add_object(identifier: str, domain: str, range_id: str, comment: str, **extra: Any) -> None:
        if domain not in classes or range_id not in classes:
            return
        existing = _object(objects.get(identifier))
        value = {
            "uri": namespace + identifier,
            "label": identifier,
            "comment": comment,
            "domain": domain,
            "range": range_id,
        }
        value.update(existing)
        # These declarations are controller-owned.  Preserve useful model
        # labels/comments, but never allow a guessed shape to break the raw
        # navigation contract.
        value.update({"uri": namespace + identifier, "domain": domain, "range": range_id})
        value.update(extra)
        objects[identifier] = value

    def add_datatype(identifier: str, domain: str, range_id: str, comment: str) -> None:
        if domain not in classes:
            return
        existing = _object(datatypes.get(identifier))
        value = {
            "uri": namespace + identifier,
            "label": identifier,
            "comment": comment,
            "domain": domain,
            "range": range_id,
        }
        value.update(existing)
        value.update({"uri": namespace + identifier, "domain": domain, "range": range_id})
        datatypes[identifier] = value

    # Stage 2 and every generated sourceAccessPath share this exact provenance
    # vocabulary.  It is deterministic plumbing rather than model-authored
    # domain semantics, so make it complete even when a model omits or renames
    # one of the links.
    for identifier, domain, range_id in (
        ("observesEntity", "EntityObservation", "DomainEntity"),
        ("hasAuthorship", "Paper", "Authorship"),
        ("authorshipOfPaper", "Authorship", "Paper"),
        ("observationFromRecord", "EntityObservation", "SourceRecord"),
        ("observationInResponse", "EntityObservation", "SearchResponse"),
        ("responseFromDocument", "SearchResponse", "SourceDocument"),
        ("responseHasRecord", "SearchResponse", "SourceRecord"),
        ("recordFromDocument", "SourceRecord", "SourceDocument"),
        ("recordHasFragment", "SourceRecord", "SourceFragment"),
        ("fragmentFromRecord", "SourceFragment", "SourceRecord"),
        ("fragmentFromDocument", "SourceFragment", "SourceDocument"),
        ("newsItemFromFragment", "NewsItem", "SourceFragment"),
    ):
        add_object(
            identifier,
            domain,
            range_id,
            f"Canonical controller-owned Source navigation {domain} to {range_id}.",
        )

    add_object(
        "documentHasFragment", "SourceDocument", "SourceFragment",
        "Inverse navigation from a raw document to every indexed fragment, including fragments of empty responses.",
        inverseOf="fragmentFromDocument",
    )
    add_object(
        "responseHasFragment", "SearchResponse", "SourceFragment",
        "Links a response directly to every fragment in its source document, including request/envelope fragments when the response has no records.",
    )
    add_object(
        "observationHasAuthorship", "PaperObservation", "Authorship",
        "Links one paper response observation to its source-specific, ordered Authorship snapshots.",
        inverseOf="authorshipFromObservation",
    )
    add_object(
        "authorshipFromObservation", "Authorship", "PaperObservation",
        "Links an Authorship snapshot to the exact paper observation whose author array produced it.",
        inverseOf="observationHasAuthorship",
    )
    add_object(
        "scholarAffiliatedWithInstitution", "Scholar", "Institution",
        "Stable Scholar to Institution shortcut projected from observation-scoped raw relationship evidence.",
    )
    # Stage 2 deliberately executes a small, stable projection vocabulary.
    # A model may additionally choose narrower names such as paperHasVenue or
    # scholarHasVenue, but the controller-owned relation snapshot mappings and
    # RDF metrics refer to these canonical shortcuts.  Always declare them
    # here; _prune_absent_relation_snapshots removes optional shortcuts whose
    # raw paths are absent from this acquisition.
    for identifier, (domain, range_id) in (
        ("authoredBy", ("Paper", "Scholar")),
        ("affiliatedWithInstitution", ("Paper", "Institution")),
        ("hasTopic", ("DomainEntity", "Topic")),
        ("hasVenue", ("DomainEntity", "Venue")),
        ("hasAward", ("DomainEntity", "Award")),
        ("hasNewsItem", ("Institution", "NewsItem")),
        ("hasRelatedPaper", ("Institution", "Paper")),
        ("hasRelatedScholar", ("Institution", "Scholar")),
    ):
        add_object(
            identifier,
            domain,
            range_id,
            f"Canonical controller-owned domain projection {domain} to {range_id}.",
        )
    for identifier, (domain, range_id, _shortcut, _endpoint, _path) in _RELATION_SNAPSHOTS.items():
        add_object(
            identifier, domain, range_id,
            "Observation-scoped relationship snapshot; preserves membership changes across API responses before any domain projection.",
        )

    inverse_pairs = {
        "hasAuthorship": "authorshipOfPaper",
        "authorshipOfPaper": "hasAuthorship",
        "recordHasFragment": "fragmentFromRecord",
        "fragmentFromRecord": "recordHasFragment",
        "documentHasFragment": "fragmentFromDocument",
        "fragmentFromDocument": "documentHasFragment",
        "observationHasAuthorship": "authorshipFromObservation",
        "authorshipFromObservation": "observationHasAuthorship",
    }
    for identifier, inverse_id in inverse_pairs.items():
        if identifier in objects and inverse_id in objects:
            objects[identifier]["inverseOf"] = inverse_id
    # An inverse annotation is optional outside the controller-owned pairs, but
    # if present the Stage 1 contract requires it to be shape-compatible and
    # reciprocal.  Drop inconsistent model annotations rather than repeatedly
    # asking the model to repair grounding that this adapter rebuilds.
    for identifier, item in objects.items():
        inverse_id = item.get("inverseOf")
        if not isinstance(inverse_id, str):
            continue
        inverse = _object(objects.get(inverse_id))
        if (
            not inverse
            or inverse.get("domain") != item.get("range")
            or inverse.get("range") != item.get("domain")
            or inverse.get("inverseOf") != identifier
        ):
            item.pop("inverseOf", None)
    for identifier in ("observesPaper", "observesScholar", "observesInstitution"):
        if identifier in objects and "observesEntity" in objects:
            objects[identifier]["subPropertyOf"] = ["observesEntity"]

    add_datatype(
        "sourceSystem", "SourceDocument", "xsd:string",
        "Source system label read from the top-level /source raw field; for this dataset the observed value is ai_index.",
    )
    add_datatype(
        "citationCount", "PaperObservation", "xsd:integer",
        "Paper citation count captured for one paper-search response observation; it is never projected onto ScholarObservation or InstitutionObservation.",
    )
    # Stage 2 materializes normalized-value concept nodes for every observed
    # Topic, Venue and Award relation.  Their display properties are executable
    # RDF plumbing just like the canonical relation shortcuts above: a model may
    # omit one, but the controller must declare it whenever the class exists.
    # The later evidence pass still removes labels whose raw relation path is
    # absent from this acquisition, so this does not invent unsupported facts.
    for identifier, domain in (
        ("topicName", "Topic"),
        ("venueName", "Venue"),
        ("awardName", "Award"),
    ):
        add_datatype(
            identifier,
            domain,
            "xsd:string",
            f"Canonical normalized display value for a {domain} concept derived from an observed relationship value.",
        )
    for identifier, domain, range_id, comment in (
        ("sourceSha256", "SourceDocument", "xsd:string", "SHA-256 of the complete indexed raw response document."),
        ("sourcePath", "SourceDocument", "xsd:string", "Workspace-relative path of the indexed raw response document."),
        ("endpoint", "SourceDocument", "xsd:string", "AI Index endpoint recorded by the raw collector."),
        ("method", "SourceDocument", "xsd:string", "HTTP method recorded by the raw collector."),
        ("mode", "SourceDocument", "xsd:string", "Collector mode recorded by the raw collector."),
        ("traceId", "SourceDocument", "xsd:string", "Request trace identifier recorded by the raw collector."),
        ("recordHash", "SourceRecord", "xsd:string", "SHA-256 of the canonical raw record value."),
        ("recordJsonPointer", "SourceRecord", "xsd:string", "Exact RFC 6901 pointer to the raw record."),
        ("jsonPointer", "SourceFragment", "xsd:string", "Exact RFC 6901 pointer to the raw fragment."),
        ("resultRank", "EntityObservation", "xsd:positiveInteger", "One-based position of the record within its response."),
        ("requestDomain", "SearchResponse", "xsd:string", "Domain filter supplied to the AI Index request."),
        ("requestSubDomain", "SearchResponse", "xsd:string", "Sub-domain filter supplied to the AI Index request."),
        ("sortType", "SearchResponse", "xsd:string", "Sort type supplied to the AI Index request."),
        ("requestPage", "SearchResponse", "xsd:integer", "Page supplied to the AI Index request."),
        ("requestSize", "SearchResponse", "xsd:integer", "Page size supplied to the AI Index request."),
        ("resultCount", "SearchResponse", "xsd:integer", "Total result count reported by the AI Index response."),
    ):
        add_datatype(identifier, domain, range_id, comment)
    # The old compact-plan fallback conflated domain and sub-domain arrays.
    # The controller now always declares the two exact raw semantics above, so
    # retaining requestTopic would leave a redundant property with no unique
    # raw evidence (or bind the same value twice).
    datatypes.pop("requestTopic", None)
    add_datatype(
        "fragmentValueKind", "SourceFragment", "xsd:string",
        "JSON value kind recorded by source_index for this exact fragment pointer.",
    )
    add_datatype(
        "fragmentValueHash", "SourceFragment", "xsd:string",
        "SHA-256 of the canonical JSON value at this exact fragment pointer; values remain replayable from the bound raw file.",
    )
    for observed_id, (canonical_id, observation_class, _entity_class, _endpoint, _path) in _STABLE_OBSERVATION_VALUES.items():
        canonical = _object(datatypes.get(canonical_id))
        if not canonical:
            continue
        add_datatype(
            observed_id,
            observation_class,
            str(canonical.get("range")),
            f"Per-response observed value corresponding to {canonical_id}; always preserved even when canonical require_consensus projection is omitted.",
        )

    # These aliases are explicitly forbidden by the stable contract.  Models
    # occasionally reintroduce prefixed variants such as paperCitedByCount;
    # remove them deterministically instead of spending a full repair round on
    # a controller-owned naming rule.
    for properties in (objects, datatypes):
        for identifier in list(properties):
            if any(identifier.lower().endswith(suffix) for suffix in _FORBIDDEN_PROPERTY_SUFFIXES):
                properties.pop(identifier, None)

    ontology["objectProperties"] = objects
    ontology["datatypeProperties"] = datatypes


def _replace_property_reference(value: Any) -> Any:
    """Rename only ontology-property references, never contract field names."""

    if isinstance(value, list):
        return [_replace_property_reference(item) for item in value]
    if isinstance(value, dict):
        return {key: _replace_property_reference(item) for key, item in value.items()}
    if isinstance(value, str):
        property_renames = {**_OBJECT_PROPERTY_RENAMES, **_DATATYPE_PROPERTY_RENAMES}
        if value in property_renames:
            return property_renames[value]
        for old, new in property_renames.items():
            if value == f"ontologyElement:{old}":
                return f"ontologyElement:{new}"
    return value


def _normalize_grounding_property_names(grounding: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(grounding)
    object_evidence = _object(result.get("objectPropertyEvidence"))
    for old, new in _OBJECT_PROPERTY_RENAMES.items():
        if old in object_evidence:
            object_evidence.setdefault(new, object_evidence[old])
            object_evidence.pop(old, None)
    result["objectPropertyEvidence"] = object_evidence
    datatype_evidence = _object(result.get("datatypePropertyEvidence"))
    for old, new in _DATATYPE_PROPERTY_RENAMES.items():
        if old in datatype_evidence:
            datatype_evidence.setdefault(new, datatype_evidence[old])
            datatype_evidence.pop(old, None)
    result["datatypePropertyEvidence"] = datatype_evidence
    for section in ("accessPaths", "sourceAccessPaths"):
        if section in result:
            result[section] = _replace_property_reference(result[section])
    return result


def _normalize_ontology(candidate: dict[str, Any], config: Stage1Config, source_fingerprint: str) -> dict[str, Any]:
    raw = _object(candidate.get("ontology"))
    metadata = _object(raw.get("metadata"))
    classes: dict[str, Any] = {}
    for identifier, value in _object(raw.get("classes")).items():
        item = _object(value)
        normalized = {
            "uri": config.ontology.namespace + identifier,
            "label": str(item.get("label") or identifier),
            "comment": _comment(item, f"Source-grounded {identifier} class."),
            "kind": item.get("kind", "concept"),
        }
        parents = [parent for parent in _array(item.get("subClassOf")) if isinstance(parent, str)]
        if parents:
            normalized["subClassOf"] = parents
        classes[identifier] = normalized
    objects: dict[str, Any] = {}
    for identifier, value in _object(raw.get("objectProperties")).items():
        item = _object(value)
        normalized_identifier = _OBJECT_PROPERTY_RENAMES.get(identifier, identifier)
        # A declaration already using the canonical name always wins over an
        # alias, independent of JSON insertion order.
        if normalized_identifier in objects and identifier != normalized_identifier:
            continue
        canonical_shape = _CANONICAL_OBJECT_PROPERTY_SHAPES.get(normalized_identifier)
        domain = canonical_shape[0] if canonical_shape else item.get("domain")
        range_id = canonical_shape[1] if canonical_shape else item.get("range")
        if isinstance(domain, list) and len(domain) == 1 and isinstance(domain[0], str):
            domain = domain[0]
        if isinstance(range_id, list) and len(range_id) == 1 and isinstance(range_id[0], str):
            range_id = range_id[0]
        objects[normalized_identifier] = {
            "uri": config.ontology.namespace + normalized_identifier,
            "label": str(
                "observation in response"
                if identifier == "responseClassId"
                else item.get("label") or normalized_identifier
            ),
            "comment": (
                "Links an entity observation to the API search response that established its within-response rank."
                if identifier == "responseClassId"
                else _comment(item, f"Source-grounded {normalized_identifier} relationship.")
            ),
            "domain": domain,
            "range": range_id,
        }
        if isinstance(item.get("inverseOf"), str):
            objects[normalized_identifier]["inverseOf"] = _OBJECT_PROPERTY_RENAMES.get(
                str(item["inverseOf"]), str(item["inverseOf"])
            )
        parents = [
            _OBJECT_PROPERTY_RENAMES.get(parent, parent)
            for parent in _array(item.get("subPropertyOf"))
            if isinstance(parent, str)
        ]
        if parents:
            objects[normalized_identifier]["subPropertyOf"] = parents
    datatypes: dict[str, Any] = {}
    for identifier, value in _object(raw.get("datatypeProperties")).items():
        item = _object(value)
        normalized_identifier = _DATATYPE_PROPERTY_RENAMES.get(identifier, identifier)
        if normalized_identifier in datatypes and identifier != normalized_identifier:
            continue
        canonical_shape = _CANONICAL_DATATYPE_PROPERTY_SHAPES.get(normalized_identifier)
        domain = canonical_shape[0] if canonical_shape else item.get("domain")
        range_id = canonical_shape[1] if canonical_shape else item.get("range") or item.get("datatype")
        if isinstance(domain, list) and len(domain) == 1 and isinstance(domain[0], str):
            domain = domain[0]
        if isinstance(range_id, list) and len(range_id) == 1 and isinstance(range_id[0], str):
            range_id = range_id[0]
        if normalized_identifier == "venueName" and "Venue" in classes:
            domain = "Venue"
        if normalized_identifier == "sourcePath" and "SourceDocument" in classes:
            domain = "SourceDocument"
        datatypes[normalized_identifier] = {
            "uri": config.ontology.namespace + normalized_identifier,
            "label": str(item.get("label") or normalized_identifier),
            "comment": _comment(item, f"Source-grounded {normalized_identifier} value."),
            "domain": domain,
            "range": range_id,
        }
        if normalized_identifier == "resultRank":
            datatypes[normalized_identifier]["range"] = "xsd:positiveInteger"
            datatypes[normalized_identifier]["comment"] = (
                "One-based rank of a record within its SearchResponse, computed as data.list array_index + 1."
            )
    observation_links = {
        "observesPaper": ("PaperObservation", "Paper"),
        "observesScholar": ("ScholarObservation", "Scholar"),
        "observesInstitution": ("InstitutionObservation", "Institution"),
    }
    for identifier, (domain, range_id) in observation_links.items():
        objects.setdefault(
            identifier,
            {
                "uri": config.ontology.namespace + identifier,
                "label": identifier,
                "comment": f"Links a {domain} to the merged {range_id} entity.",
                "domain": domain,
                "range": range_id,
            },
        )
    for entity in ("Paper", "Scholar", "Institution"):
        identifier = entity[0].lower() + entity[1:] + "ObservationSourceRaw"
        observation = entity + "Observation"
        datatypes.setdefault(
            identifier,
            {
                "uri": config.ontology.namespace + identifier,
                "label": f"{entity} observation source raw",
                "comment": "Workspace-relative raw AI Index response path retained by the normalized evidence view.",
                "domain": observation,
                "range": "xsd:string",
            },
        )
    datatypes.setdefault(
        "recordJsonPointer",
        {
            "uri": config.ontology.namespace + "recordJsonPointer",
            "label": "record JSON pointer",
            "comment": "Exact RFC 6901 pointer to this SourceRecord within its SourceDocument.",
            "domain": "SourceRecord",
            "range": "xsd:string",
        },
    )
    if "authorOrder" in datatypes:
        datatypes["authorOrder"]["range"] = "xsd:positiveInteger"
    if "newsDate" in datatypes:
        datatypes["newsDate"]["range"] = "xsd:date"
    if "sourcePath" in datatypes:
        datatypes["sourcePath"]["comment"] = "Workspace-relative path of the indexed raw API response document."
    if "recordHash" in datatypes:
        datatypes["recordHash"]["comment"] = (
            "SHA-256 of the canonical SourceRecord value for replay integrity; it never deduplicates observations."
        )
    if "SourceDocument" in classes:
        classes["SourceDocument"]["comment"] = "A complete indexed raw API or fixture response document."
    ontology = {
        "schemaVersion": "dataelf-ontology.v2",
        "metadata": {
            "id": config.ontology.ontology_id,
            "namespace": config.ontology.namespace,
            "title": str(metadata.get("title") or config.ontology.title),
            "description": str(metadata.get("description") or "Three-layer raw-grounded AI Index ontology."),
            "version": "2.1.0",
            "sourceFingerprint": source_fingerprint,
        },
        "classes": classes,
        "objectProperties": objects,
        "datatypeProperties": datatypes,
    }
    _ensure_rdf_ready_ontology(ontology, config.ontology.namespace)
    return ontology


def _class_evidence(
    grounding: dict[str, Any], ontology: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    entity_sources = {
        "Paper": ("papers", "paper_id"),
        "Scholar": ("scholars", "scholar_id"),
        "Institution": ("institutions", "institution_id"),
    }
    source_ref = evidence["sourceIndexEvidenceRef"]
    catalog_ref = evidence["catalogEvidenceRef"]
    identities = _object(_object(evidence.get("toolIndex")).get("identities"))
    raw_section = _object(grounding.get("classEvidence"))
    source_index_classes = {
        "DomainEntity": ("entityProfiles", "Union of raw-backed Paper, Scholar and Institution business entities."),
        "EntityObservation": ("records[*]", "One observation per non-empty API response record."),
        "SourceDocument": ("documents[*]", "One indexed raw API response document."),
        "SearchResponse": ("documents[*].request + documents[*].resultCount", "Request and response-envelope observation."),
        "SourceRecord": ("records[*]", "Exact indexed /data/list/N record."),
        "SourceFragment": ("fragments[*]", "Exact indexed envelope, record, object, array or scalar fragment."),
    }
    promoted_class_paths = {
        "Topic": [
            ("/openapi/paper/search", "/data/list/*/fields/*"),
            ("/openapi/scholar/search", "/data/list/*/fields/*"),
            ("/openapi/institutions/search", "/data/list/*/fields/*"),
        ],
        "Venue": [
            ("/openapi/paper/search", "/data/list/*/venue"),
            ("/openapi/scholar/search", "/data/list/*/venues/*"),
        ],
        "Award": [
            ("/openapi/paper/search", "/data/list/*/awards/*"),
            ("/openapi/scholar/search", "/data/list/*/awards/*"),
        ],
        "NewsItem": [("/openapi/institutions/search", "/data/list/*/news/*")],
    }
    for class_id in ontology["classes"]:
        entries: list[dict[str, Any]] = []
        if class_id in source_index_classes:
            index_path, description = source_index_classes[class_id]
            entries.append(
                {
                    "sourceKind": "source_index",
                    "indexPath": index_path,
                    "formula": description,
                    "evidenceRefs": [source_ref],
                }
            )
        for raw in _array(raw_section.get(class_id)):
            item = _object(raw)
            if class_id not in source_index_classes and item.get("sourceKind") == "raw_json":
                entries.append(
                    {
                        "sourceKind": "raw_json",
                        "endpoint": str(item.get("endpoint") or "*"),
                        "pathPattern": str(item.get("pathPattern") or "$document"),
                        "evidenceRefs": _refs(item.get("evidenceRefs")) or [source_ref],
                    }
                )
        existing = {(item.get("endpoint"), item.get("pathPattern")) for item in entries}
        for endpoint, path_pattern in promoted_class_paths.get(class_id, []):
            if (endpoint, path_pattern) not in existing:
                entries.append(
                    {
                        "sourceKind": "raw_json",
                        "endpoint": endpoint,
                        "pathPattern": path_pattern,
                        "evidenceRefs": [source_ref],
                    }
                )
        if class_id in entity_sources:
            table, column = entity_sources[class_id]
            identity_ref = identities.get(f"{table}|{column}")
            entries.append(
                {
                    "table": table,
                    "identityColumns": [column],
                    "identitySemantics": "entity_merge_key",
                    "identityEvidenceRef": identity_ref,
                    "evidenceRefs": [ref for ref in (identity_ref, source_ref) if isinstance(ref, str)],
                }
            )
        elif not entries:
            entries.append(
                {
                    "table": "papers",
                    "identityColumns": [],
                    "identitySemantics": "conceptual",
                    "evidenceRefs": [catalog_ref, source_ref],
                }
            )
        result[class_id] = entries
    return result


def _raw_path_observed(evidence: dict[str, Any], endpoint: str, path_pattern: str) -> bool:
    profiles = _object(_object(evidence.get("sourceIndex")).get("pathProfiles"))
    if endpoint == "*":
        return any(
            _object(item).get("pathPattern") == path_pattern
            and int(_object(item).get("occurrenceCount") or 0) > 0
            for item in profiles.values()
        )
    item = _object(profiles.get(f"{endpoint}|{path_pattern}"))
    return int(item.get("occurrenceCount") or 0) > 0


def _prune_absent_relation_snapshots(ontology: dict[str, Any], evidence: dict[str, Any]) -> None:
    objects = _object(ontology.get("objectProperties"))
    explicit_paths = {
        "observationHasTopic": [
            ("/openapi/paper/search", "/data/list/*/fields/*"),
            ("/openapi/scholar/search", "/data/list/*/fields/*"),
            ("/openapi/institutions/search", "/data/list/*/fields/*"),
        ],
        "observationHasVenue": [
            ("/openapi/paper/search", "/data/list/*/venue"),
            ("/openapi/scholar/search", "/data/list/*/venues/*"),
        ],
        "observationHasAward": [
            ("/openapi/paper/search", "/data/list/*/awards/*"),
            ("/openapi/scholar/search", "/data/list/*/awards/*"),
        ],
    }
    for property_id, (_observation, _target, shortcut, endpoint, path_pattern) in _RELATION_SNAPSHOTS.items():
        paths = explicit_paths.get(property_id, [(endpoint, path_pattern)])
        if not any(_raw_path_observed(evidence, item_endpoint, item_path) for item_endpoint, item_path in paths):
            objects.pop(property_id, None)
            if shortcut:
                objects.pop(shortcut, None)

def _property_evidence(
    grounding: dict[str, Any], ontology: dict[str, Any], evidence: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_ref = evidence["sourceIndexEvidenceRef"]
    endpoints = sorted(
        {
            str(item.get("endpoint"))
            for item in _array(_object(evidence.get("sourceIndex")).get("documents"))
            if isinstance(item, dict) and str(item.get("endpoint", "")).strip()
        }
    )
    raw_objects = _object(grounding.get("objectPropertyEvidence"))
    promoted_object_paths = {
        "hasTopic": [
            ("/openapi/paper/search", "/data/list/*/fields/*"),
            ("/openapi/scholar/search", "/data/list/*/fields/*"),
            ("/openapi/institutions/search", "/data/list/*/fields/*"),
        ],
        "hasVenue": [
            ("/openapi/paper/search", "/data/list/*/venue"),
            ("/openapi/scholar/search", "/data/list/*/venues/*"),
        ],
        "hasAward": [
            ("/openapi/paper/search", "/data/list/*/awards/*"),
            ("/openapi/scholar/search", "/data/list/*/awards/*"),
        ],
        "hasNewsItem": [("/openapi/institutions/search", "/data/list/*/news/*")],
        "observationHasAuthorship": [("/openapi/paper/search", "/data/list/*/author_ids/*")],
        "authorshipFromObservation": [("/openapi/paper/search", "/data/list/*/author_ids/*")],
        "observationHasTopic": [
            ("/openapi/paper/search", "/data/list/*/fields/*"),
            ("/openapi/scholar/search", "/data/list/*/fields/*"),
            ("/openapi/institutions/search", "/data/list/*/fields/*"),
        ],
        "observationHasVenue": [
            ("/openapi/paper/search", "/data/list/*/venue"),
            ("/openapi/scholar/search", "/data/list/*/venues/*"),
        ],
        "observationHasAward": [
            ("/openapi/paper/search", "/data/list/*/awards/*"),
            ("/openapi/scholar/search", "/data/list/*/awards/*"),
        ],
        "paperObservationHasInstitution": [
            ("/openapi/paper/search", "/data/list/*/institution_ids/*")
        ],
        "scholarObservationHasInstitution": [
            ("/openapi/scholar/search", "/data/list/*/institution_ids/*")
        ],
        "scholarObservationHasPaper": [
            ("/openapi/scholar/search", "/data/list/*/paper_ids/*")
        ],
        "institutionObservationHasNewsItem": [
            ("/openapi/institutions/search", "/data/list/*/news/*")
        ],
        "institutionObservationHasRelatedPaper": [
            ("/openapi/institutions/search", "/data/list/*/related_paper_ids/*")
        ],
        "institutionObservationHasRelatedScholar": [
            ("/openapi/institutions/search", "/data/list/*/related_scholar_ids/*")
        ],
    }
    object_result: dict[str, Any] = {}
    for property_id in ontology["objectProperties"]:
        entries: list[dict[str, Any]] = []
        for raw in _array(raw_objects.get(property_id)):
            item = _object(raw)
            if item.get("sourceKind") == "raw_json":
                path = str(item.get("pathPattern") or "/data/list/*")
                endpoint = str(item.get("endpoint") or "*")
                if not _raw_path_observed(evidence, endpoint, path):
                    continue
                entries.append(
                    {
                        "sourceKind": "raw_json",
                        "endpoint": endpoint,
                        "pathPattern": path,
                        "evidenceKind": "raw_array_relation" if "/*" in path else "source_navigation",
                        "evidenceRefs": _refs(item.get("evidenceRefs")) or [source_ref],
                    }
                )
        existing = {(item.get("endpoint"), item.get("pathPattern")) for item in entries}
        for endpoint, path_pattern in promoted_object_paths.get(property_id, []):
            if not _raw_path_observed(evidence, endpoint, path_pattern):
                continue
            if (endpoint, path_pattern) not in existing:
                entries.append(
                    {
                        "sourceKind": "raw_json",
                        "endpoint": endpoint,
                        "pathPattern": path_pattern,
                        "evidenceKind": "raw_array_relation" if "/*" in path_pattern else "source_navigation",
                        "evidenceRefs": [source_ref],
                    }
                )
        if not entries:
            entries = [{"evidenceKind": "source_navigation", "evidenceRefs": [source_ref]}]
        object_result[property_id] = entries
    source_index_objects = {
        "observesEntity": "entityProfiles + records[*].businessId",
        "observationFromRecord": "records[*]",
        "responseFromDocument": "documents[*]",
        "responseHasRecord": "documents[*].documentId -> records[*].documentId",
        "recordFromDocument": "records[*].documentId -> documents[*].documentId",
        "recordHasFragment": "records[*].documentId/jsonPointer -> fragments[*]",
        "fragmentFromRecord": "fragments[*].recordId -> records[*].recordId",
        "fragmentFromDocument": "fragments[*].documentId -> documents[*].documentId",
        "newsItemFromFragment": "fragments[*].jsonPointer -> nested news object IRI",
        "documentHasFragment": "documents[*].documentId -> fragments[*].documentId",
        "responseHasFragment": "documents[*].responseId -> fragments[*].responseId",
        "observationInResponse": "records[*].documentId -> documents[*].documentId",
        "observesPaper": "records[kind=paper].businessId -> entityProfiles.paper",
        "observesScholar": "records[kind=scholar].businessId -> entityProfiles.scholar",
        "observesInstitution": "records[kind=institution].businessId -> entityProfiles.institution",
    }
    for property_id, index_path in source_index_objects.items():
        if property_id in ontology["objectProperties"]:
            object_result[property_id] = [
                {
                    "sourceKind": "source_index",
                    "indexPath": index_path,
                    "evidenceKind": "source_navigation" if "observes" not in property_id.lower() else "observation_link",
                    "evidenceRefs": [source_ref],
                }
            ]

    raw_datatypes = _object(grounding.get("datatypePropertyEvidence"))
    promoted_datatype_paths = {
        "paperId": [("/openapi/paper/search", "/data/list/*/id")],
        "title": [("/openapi/paper/search", "/data/list/*/title")],
        "abstract": [("/openapi/paper/search", "/data/list/*/abstract")],
        "publishedAt": [("/openapi/paper/search", "/data/list/*/published_at")],
        "scholarId": [("/openapi/scholar/search", "/data/list/*/id")],
        "scholarName": [("/openapi/scholar/search", "/data/list/*/name")],
        "homepage": [("/openapi/scholar/search", "/data/list/*/homepage")],
        "email": [("/openapi/scholar/search", "/data/list/*/email")],
        "institutionId": [("/openapi/institutions/search", "/data/list/*/id")],
        "institutionName": [("/openapi/institutions/search", "/data/list/*/name")],
        "country": [("/openapi/institutions/search", "/data/list/*/country")],
        "region": [("/openapi/institutions/search", "/data/list/*/region")],
        "topicName": promoted_object_paths["hasTopic"],
        "venueName": promoted_object_paths["hasVenue"],
        "awardName": promoted_object_paths["hasAward"],
        "newsTitle": [("/openapi/institutions/search", "/data/list/*/news/*/title")],
        "newsSource": [("/openapi/institutions/search", "/data/list/*/news/*/source")],
        "newsDate": [("/openapi/institutions/search", "/data/list/*/news/*/date")],
        "authorOrder": [("/openapi/paper/search", "/data/list/*/author_ids/*")],
        "isFirstAuthor": [("/openapi/paper/search", "/data/list/*/author_ids/*")],
        "citationCount": [("/openapi/paper/search", "/data/list/*/citation_count")],
        "scholarPaperCount": [
            ("/openapi/scholar/search", "/data/list/*/paper_count"),
            ("/openapi/scholar/search", "/raw/data/list/*/paper_count"),
        ],
        "paperCount": [("/openapi/institutions/search", "/data/list/*/paper_count")],
        "scholarCount": [("/openapi/institutions/search", "/data/list/*/scholar_count")],
        "fundingTotalUsd": [("/openapi/institutions/search", "/data/list/*/funding_total_usd")],
        "impactFunding": [("/openapi/institutions/search", "/data/list/*/impact/funding")],
        "impactIndustry": [("/openapi/institutions/search", "/data/list/*/impact/industry")],
        "impactNews": [("/openapi/institutions/search", "/data/list/*/impact/news")],
        "impactPaper": [("/openapi/institutions/search", "/data/list/*/impact/paper")],
        "impactTalent": [("/openapi/institutions/search", "/data/list/*/impact/talent")],
        "sourceSystem": [(endpoint, "/source") for endpoint in endpoints],
    }
    for observed_id, (_canonical, _observation, _entity, endpoint, path_pattern) in _STABLE_OBSERVATION_VALUES.items():
        promoted_datatype_paths[observed_id] = [(endpoint, path_pattern)]
    for property_id, path_pattern in {
        "endpoint": "/endpoint",
        "method": "/method",
        "mode": "/mode",
        "traceId": "/trace_id",
        "sortType": "/request/sort_type",
        "requestPage": "/request/page",
        "requestSize": "/request/size",
        "resultCount": "/data/total",
        "hotnessDay": "/data/list/*/hotness/day",
        "hotnessWeek": "/data/list/*/hotness/week",
        "hotnessMonth": "/data/list/*/hotness/month",
        "hotnessHalfYear": "/data/list/*/hotness/half_year",
        "hotnessPreviousHalfYear": "/data/list/*/hotness/previous_half_year",
    }.items():
        promoted_datatype_paths[property_id] = [(endpoint, path_pattern) for endpoint in endpoints]
    request_paths = {
        "requestDomain": ("/request/domains", "/request/domains/*"),
        "requestSubDomain": ("/request/sub_domains", "/request/sub_domains/*"),
    }
    for property_id, path_patterns in request_paths.items():
        if property_id in ontology["datatypeProperties"]:
            promoted_datatype_paths[property_id] = [
                (endpoint, path_pattern)
                for endpoint in endpoints
                for path_pattern in path_patterns
            ]
    if "requestTopic" in ontology["datatypeProperties"]:
        # requestTopic is the compact-plan fallback.  Avoid giving two declared
        # properties the same raw semantics when the model chose the more
        # precise requestDomain/requestSubDomain split.
        topic_patterns = [
            path_pattern
            for property_id, path_patterns in request_paths.items()
            if property_id not in ontology["datatypeProperties"]
            for path_pattern in path_patterns
        ]
        promoted_datatype_paths["requestTopic"] = [
            (endpoint, path_pattern)
            for endpoint in endpoints
            for path_pattern in topic_patterns
        ]
    datatype_result: dict[str, Any] = {}
    for property_id in ontology["datatypeProperties"]:
        entries = []
        for raw in _array(raw_datatypes.get(property_id)):
            item = _object(raw)
            if item.get("sourceKind") == "raw_json":
                entries.append(
                    {
                        "sourceKind": "raw_json",
                        "endpoint": str(item.get("endpoint") or "*"),
                        "pathPattern": str(item.get("pathPattern") or "$document"),
                        "evidenceRefs": _refs(item.get("evidenceRefs")) or [source_ref],
                    }
                )
        existing = {(item.get("endpoint"), item.get("pathPattern")) for item in entries}
        for endpoint, path_pattern in promoted_datatype_paths.get(property_id, []):
            if (endpoint, path_pattern) not in existing:
                entries.append(
                    {
                        "sourceKind": "raw_json",
                        "endpoint": endpoint,
                        "pathPattern": path_pattern,
                        "evidenceRefs": [source_ref],
                    }
                )
        datatype_result[property_id] = entries
    source_index_datatypes = {
        "sourcePath": ("documents[*].relativeFile", "workspace-relative raw document path"),
        "jsonPointer": ("records[*].jsonPointer or fragments[*].jsonPointer", "RFC 6901 pointer recorded by the source index"),
        "sourceSha256": ("documents[*].sha256", "SHA-256 of the complete raw document bytes"),
        "recordHash": ("records[*].recordHash", "SHA-256 of canonical JSON record value"),
        "resultRank": ("records[*].resultRank", "one-based response rank computed as data.list array_index + 1"),
        "recordJsonPointer": ("records[*].jsonPointer", "exact RFC 6901 pointer to the indexed data.list record"),
        "fragmentValueKind": ("fragments[*].valueKind", "JSON value kind at the exact replayed fragment pointer"),
        "fragmentValueHash": ("fragments[*].valueHash", "SHA-256 of canonical JSON at the exact replayed fragment pointer"),
    }
    for property_id, (index_path, formula) in source_index_datatypes.items():
        if property_id in ontology["datatypeProperties"]:
            datatype_result[property_id] = [
                {
                    "sourceKind": "source_index",
                    "indexPath": index_path,
                    "formula": formula,
                    "evidenceRefs": [source_ref],
                }
            ]
    qualifier_formulas = {"authorOrder": "array_index + 1", "isFirstAuthor": "array_index == 0"}
    for property_id, formula in qualifier_formulas.items():
        for entry in datatype_result.get(property_id, []):
            entry["formula"] = formula
    for entry in datatype_result.get("scholarPaperCount", []):
        if entry.get("sourceKind") != "raw_json":
            continue
        entry["formula"] = (
            "length(paper_ids)"
            if str(entry.get("pathPattern", "")).endswith("/paper_ids/*")
            else "direct raw paper_count integer value"
        )
    for observed_id, (canonical_id, _observation, _entity, _endpoint, _path) in _STABLE_OBSERVATION_VALUES.items():
        for entry in datatype_result.get(observed_id, []):
            entry["formula"] = "per-record observation value; no cross-response merge"
        for entry in datatype_result.get(canonical_id, []):
            entry["formula"] = "require_consensus by business ID; omit canonical triple when observations disagree"
    profile_refs = _object(_object(evidence.get("toolIndex")).get("profiles"))
    for entity, table in (("Paper", "papers"), ("Scholar", "scholars"), ("Institution", "institutions")):
        property_id = entity[0].lower() + entity[1:] + "ObservationSourceRaw"
        datatype_result[property_id] = [
            {"table": table, "column": "source_raw", "evidenceRefs": [profile_refs[table]]}
        ]
    return object_result, datatype_result


def _deduplicate_datatype_raw_bindings(
    ontology: dict[str, Any], datatype_evidence: dict[str, Any]
) -> None:
    """Resolve ambiguous model aliases without inventing transformation formulas.

    The validator permits several properties to read one raw scalar only when
    every property declares a distinct non-empty formula (for example a stable
    consensus value and its per-response observation).  For an ambiguous group
    prefer the stable controller vocabulary and remove only the conflicting raw
    entry from aliases.  An alias with other unique evidence remains declared.
    """

    preferred = {
        *_DATATYPE_PROPERTY_RENAMES.values(),
        "paperId", "title", "abstract", "publishedAt",
        "scholarId", "scholarName", "homepage", "email",
        "institutionId", "institutionName", "country", "region",
        "topicName", "venueName", "awardName", "newsTitle", "newsSource", "newsDate",
        "authorOrder", "isFirstAuthor", "citationCount", "paperCount", "scholarCount",
        "sourceSystem", "endpoint", "method", "mode", "traceId",
        "requestDomain", "requestSubDomain", "sortType", "requestPage", "requestSize",
        "resultCount", "sourcePath", "sourceSha256", "recordHash", "recordJsonPointer",
        "jsonPointer", "resultRank", "fragmentValueKind", "fragmentValueHash",
        *_STABLE_OBSERVATION_VALUES.keys(),
    }
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for property_id, entries in datatype_evidence.items():
        for raw in _array(entries):
            item = _object(raw)
            if item.get("sourceKind") != "raw_json":
                continue
            key = (str(item.get("endpoint") or ""), str(item.get("pathPattern") or ""))
            groups.setdefault(key, []).append((property_id, str(item.get("formula") or "").strip()))

    for key, owners in groups.items():
        if len(owners) < 2:
            continue
        formulas = [formula for _identifier, formula in owners]
        if all(formulas) and len(set(formulas)) == len(formulas):
            continue
        winner_id, winner_formula = min(
            owners,
            key=lambda item: (
                0 if item[0] in preferred else 1,
                0 if item[1] else 1,
                item[0],
            ),
        )
        for property_id, formula in owners:
            if property_id == winner_id:
                continue
            # A genuinely different, explicit transform may coexist with a
            # formula-bearing canonical property.  Blank/duplicate formulas do
            # not establish distinct semantics and are removed.
            if winner_formula and formula and formula != winner_formula:
                continue
            datatype_evidence[property_id] = [
                raw
                for raw in _array(datatype_evidence.get(property_id))
                if not (
                    _object(raw).get("sourceKind") == "raw_json"
                    and str(_object(raw).get("endpoint") or "") == key[0]
                    and str(_object(raw).get("pathPattern") or "") == key[1]
                )
            ]

    datatypes = _object(ontology.get("datatypeProperties"))
    for identifier in list(datatypes):
        if _array(datatype_evidence.get(identifier)):
            continue
        datatypes.pop(identifier, None)
        datatype_evidence.pop(identifier, None)


def _table_classifications(ontology: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    class_map = {
        "papers": ["Paper", "PaperObservation"], "scholars": ["Scholar", "ScholarObservation"],
        "institutions": ["Institution", "InstitutionObservation"], "paper_author": ["Authorship"],
        "paper_institution": ["Paper", "Institution"], "scholar_institution": ["Scholar", "Institution"],
        "paper_awards": ["Paper", "Award"], "scholar_awards": ["Scholar", "Award"],
        "scholar_venues": ["Scholar", "Venue"],
    }
    profile_refs = _object(_object(evidence.get("toolIndex")).get("profiles"))
    result: dict[str, Any] = {}
    declared = set(ontology["classes"])
    for table in evidence["catalog"]["tables"]:
        name = table["name"]
        classes = [item for item in class_map.get(name, []) if item in declared]
        role = "metadata" if not table["rowCount"] else ("association" if name not in {"papers", "scholars", "institutions"} else "observation")
        result[name] = {
            "role": role,
            "classIds": classes,
            "reason": "Empty schema-only evidence view." if not table["rowCount"] else "Raw-derived normalized evidence view; raw JSON remains authoritative.",
            "evidenceRefs": [profile_refs[name]],
        }
    return result


def _column_classifications(ontology: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    profile_refs = _object(_object(evidence.get("toolIndex")).get("profiles"))
    lineage_ref = evidence["normalizationLineageEvidenceRef"]
    result: dict[str, Any] = {}
    special = {
        "papers.paper_id": ("entity_merge_key", "paperId"),
        "scholars.scholar_id": ("entity_merge_key", "scholarId"),
        "institutions.institution_id": ("entity_merge_key", "institutionId"),
        "papers.source_raw": ("provenance", "paperObservationSourceRaw"),
        "scholars.source_raw": ("provenance", "scholarObservationSourceRaw"),
        "institutions.source_raw": ("provenance", "institutionObservationSourceRaw"),
    }
    for table in evidence["catalog"]["tables"]:
        for column in table["columns"]:
            coordinate = f"{table['name']}.{column}"
            refs = [profile_refs[table["name"]], lineage_ref]
            if coordinate in special and special[coordinate][1] in ontology["datatypeProperties"]:
                role, property_id = special[coordinate]
                result[coordinate] = {
                    "role": role, "propertyIds": [property_id],
                    "reason": "Safe normalized identity/provenance view of an authoritative raw binding.",
                    "evidenceRefs": refs,
                }
            else:
                result[coordinate] = {
                    "role": "ignored", "propertyIds": [],
                    "reason": "Not promoted from the normalized view; retained through exact raw Source navigation.",
                    "evidenceRefs": refs,
                }
    return result


def _observation_mappings(ontology: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    profiles = _object(_object(evidence.get("toolIndex")).get("profiles"))
    result: dict[str, Any] = {}
    for entity, table, key, endpoint in (
        ("Paper", "papers", "paper_id", "/openapi/paper/search"),
        ("Scholar", "scholars", "scholar_id", "/openapi/scholar/search"),
        ("Institution", "institutions", "institution_id", "/openapi/institutions/search"),
    ):
        lower = entity[0].lower() + entity[1:]
        result[entity] = {
            "entityTable": table, "mergeKey": key, "observationClassId": entity + "Observation",
            "observedEntityPropertyId": "observes" + entity,
            "genericObservedEntityPropertyId": "observesEntity",
            "sourceRecordClassId": "SourceRecord",
            "sourceDocumentClassId": "SourceDocument",
            "endpoint": endpoint,
            "businessIdPathPattern": "/data/list/*/id",
            "sourceRawColumn": "source_raw", "sourceRawPropertyId": lower + "ObservationSourceRaw",
            "rowLocatorFields": ["documentId", "jsonPointer", "recordHash"],
            "evidenceRefs": [profiles[table], evidence["sourceIndexEvidenceRef"]],
        }
    return result


def _response_observation_mappings(evidence: dict[str, Any]) -> dict[str, Any]:
    source_ref = evidence["sourceIndexEvidenceRef"]
    return {
        observation_class: {
            "responseClassId": "SearchResponse",
            "sourceRecordClassId": "SourceRecord",
            "responsePropertyId": "observationInResponse",
            "recordPropertyId": "observationFromRecord",
            "genericObservedEntityPropertyId": "observesEntity",
            "resultRankPropertyId": "resultRank",
            "resultRankFormula": "data.list array_index + 1",
            "resultRankBase": 1,
            "endpoint": endpoint,
            "recordPathPattern": "/data/list/*",
            "evidenceRefs": [source_ref],
            "note": "Every API record remains a distinct observation even when its business entity and record hash repeat.",
        }
        for observation_class, endpoint in (
            ("PaperObservation", "/openapi/paper/search"),
            ("ScholarObservation", "/openapi/scholar/search"),
            ("InstitutionObservation", "/openapi/institutions/search"),
        )
    }


def _source_coverage(evidence: dict[str, Any]) -> dict[str, Any]:
    source_index = _object(evidence.get("sourceIndex"))
    metrics = _object(source_index.get("metrics"))
    catalog = _object(evidence.get("catalog"))
    return {
        "tableCount": catalog.get("tableCount"),
        "nonEmptyTableCount": catalog.get("nonEmptyTableCount"),
        "columnCount": sum(len(_array(item.get("columns"))) for item in _array(catalog.get("tables")) if isinstance(item, dict)),
        "totalRowCount": catalog.get("totalRowCount"),
        "documentCount": metrics.get("documentCount"),
        "nonEmptyResponseCount": metrics.get("nonEmptyResponseCount"),
        "emptyResponseCount": metrics.get("emptyResponseCount"),
        "recordObservationCount": metrics.get("recordCount"),
        "fragmentCount": metrics.get("fragmentCount"),
        "rawPathPatternCount": metrics.get("pathPatternCount"),
        "unclassifiedRawPathCount": metrics.get("unclassifiedPathCount"),
        "sourceIndexSha256": source_index.get("sourceIndexSha256"),
        "evidenceRefs": [evidence["sourceIndexEvidenceRef"]],
    }


def _entity_resolution_mappings(evidence: dict[str, Any]) -> dict[str, Any]:
    source_ref = evidence["sourceIndexEvidenceRef"]
    profiles = _object(_object(evidence.get("sourceIndex")).get("entityProfiles"))
    result: dict[str, Any] = {}
    for kind, class_id in (("paper", "Paper"), ("scholar", "Scholar"), ("institution", "Institution")):
        profile = deepcopy(_object(profiles.get(kind)))
        result[class_id] = {
            **profile,
            "conflictPolicy": "observation_only",
            "stableFieldConsensus": deepcopy(_object(profile.get("fieldConsensus"))),
            "evidenceRefs": [source_ref],
            "note": (
                "Merge by businessId only. Stable values are promoted per field only when all observations agree; "
                "conflicting values remain on observations. Mutable metrics never participate in entity consensus."
            ),
        }
    return result


def _observation_value_mappings(ontology: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    source_ref = evidence["sourceIndexEvidenceRef"]
    datatypes = _object(ontology.get("datatypeProperties"))
    result: dict[str, Any] = {}
    for observed_id, (canonical_id, observation_class, entity_class, endpoint, path_pattern) in _STABLE_OBSERVATION_VALUES.items():
        if canonical_id not in datatypes or observed_id not in datatypes:
            continue
        result[canonical_id] = {
            "entityClassId": entity_class,
            "observationClassId": observation_class,
            "canonicalPropertyId": canonical_id,
            "observationPropertyId": observed_id,
            "endpoint": endpoint,
            "pathPattern": path_pattern,
            "observationProjection": "always_preserve_per_record",
            "canonicalProjection": "require_consensus_by_business_id",
            "conflictDisposition": "observation_only",
            "evidenceRefs": [source_ref],
        }
    return result


def _relation_snapshot_mappings(ontology: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    source_ref = evidence["sourceIndexEvidenceRef"]
    objects = _object(ontology.get("objectProperties"))
    result: dict[str, Any] = {}
    explicit_paths = {
        "observationHasTopic": [
            ("/openapi/paper/search", "/data/list/*/fields/*"),
            ("/openapi/scholar/search", "/data/list/*/fields/*"),
            ("/openapi/institutions/search", "/data/list/*/fields/*"),
        ],
        "observationHasVenue": [
            ("/openapi/paper/search", "/data/list/*/venue"),
            ("/openapi/scholar/search", "/data/list/*/venues/*"),
        ],
        "observationHasAward": [
            ("/openapi/paper/search", "/data/list/*/awards/*"),
            ("/openapi/scholar/search", "/data/list/*/awards/*"),
        ],
    }
    for property_id, (observation_class, target_class, shortcut, endpoint, path_pattern) in _RELATION_SNAPSHOTS.items():
        if property_id not in objects:
            continue
        paths = [
            (item_endpoint, item_path)
            for item_endpoint, item_path in explicit_paths.get(property_id, [(endpoint, path_pattern)])
            if _raw_path_observed(evidence, item_endpoint, item_path)
        ]
        if not paths:
            continue
        result[property_id] = {
            "observationClassId": observation_class,
            "targetClassId": target_class,
            "observationPropertyId": property_id,
            "domainShortcutPropertyId": shortcut or None,
            "sourcePaths": [
                {"endpoint": item_endpoint, "pathPattern": item_path}
                for item_endpoint, item_path in paths
            ],
            "snapshotPolicy": "preserve_each_response_membership",
            "domainProjectionPolicy": (
                "non_exhaustive_highlight_union" if "Related" in property_id
                else "authority_aware_union_with_observation_provenance"
            ),
            "missingnessPolicy": "absent_array_or_field_means_unknown_not_false",
            "evidenceRefs": [source_ref],
        }
    if "observationHasAuthorship" in objects and "authorshipFromObservation" in objects:
        result["observationHasAuthorship"] = {
            "observationClassId": "PaperObservation",
            "targetClassId": "Authorship",
            "observationPropertyId": "observationHasAuthorship",
            "inversePropertyId": "authorshipFromObservation",
            "domainShortcutPropertyId": "hasAuthorship",
            "sourcePaths": [
                {"endpoint": "/openapi/paper/search", "pathPattern": "/data/list/*/author_ids/*"}
            ],
            "snapshotPolicy": "one_authorship_per_source_array_element",
            "domainProjectionPolicy": "derive_authoredBy_and_hasAuthorship_without_dropping_snapshot",
            "missingnessPolicy": "absent_array_means_unknown_not_false",
            "evidenceRefs": [source_ref],
        }
    return result


def _iri_generation_mappings(ontology: dict[str, Any], config: Stage1Config, evidence: dict[str, Any]) -> dict[str, Any]:
    namespace = config.ontology.namespace
    source_ref = evidence["sourceIndexEvidenceRef"]
    classes = _object(ontology.get("classes"))
    abstract = {"DomainEntity", "EntityObservation"}
    specifications: dict[str, dict[str, Any]] = {
        "Paper": {"template": "{baseNamespace}instance/paper/{businessIdEncoded}", "identityInputs": ["/data/list/*/id"], "encoding": "rfc3986_percent_encode_utf8"},
        "Scholar": {"template": "{baseNamespace}instance/scholar/{businessIdEncoded}", "identityInputs": ["/data/list/*/id"], "encoding": "rfc3986_percent_encode_utf8"},
        "Institution": {"template": "{baseNamespace}instance/institution/{businessIdEncoded}", "identityInputs": ["/data/list/*/id"], "encoding": "rfc3986_percent_encode_utf8"},
        "PaperObservation": {"template": "{baseNamespace}instance/observation/paper/{sourceDocumentId}/{sourceSha256}/{jsonPointerToken}", "identityInputs": ["source_index.documentId", "SourceDocument.sourceSha256", "SourceRecord.recordJsonPointer"], "encoding": "base64url_utf8_no_padding_for_pointer"},
        "ScholarObservation": {"template": "{baseNamespace}instance/observation/scholar/{sourceDocumentId}/{sourceSha256}/{jsonPointerToken}", "identityInputs": ["source_index.documentId", "SourceDocument.sourceSha256", "SourceRecord.recordJsonPointer"], "encoding": "base64url_utf8_no_padding_for_pointer"},
        "InstitutionObservation": {"template": "{baseNamespace}instance/observation/institution/{sourceDocumentId}/{sourceSha256}/{jsonPointerToken}", "identityInputs": ["source_index.documentId", "SourceDocument.sourceSha256", "SourceRecord.recordJsonPointer"], "encoding": "base64url_utf8_no_padding_for_pointer"},
        "SourceDocument": {"template": "{baseNamespace}instance/source-document/{sourceDocumentId}", "identityInputs": ["source_index.documentId = sha256({relativeFile, sourceSha256}) prefix"], "encoding": "controller_lowercase_hex_identifier"},
        "SearchResponse": {"template": "{baseNamespace}instance/search-response/{responseId}", "identityInputs": ["source_index.responseId derived from documentId"], "encoding": "controller_lowercase_hex_identifier"},
        "SourceRecord": {"template": "{baseNamespace}instance/source-record/{sourceDocumentId}/{sourceSha256}/{jsonPointerToken}", "identityInputs": ["source_index.documentId", "SourceDocument.sourceSha256", "SourceRecord.recordJsonPointer"], "encoding": "base64url_utf8_no_padding_for_pointer"},
        "SourceFragment": {"template": "{baseNamespace}instance/source-fragment/{sourceDocumentId}/{sourceSha256}/{jsonPointerToken}", "identityInputs": ["source_index.documentId", "SourceDocument.sourceSha256", "SourceFragment.jsonPointer"], "encoding": "base64url_utf8_no_padding_for_pointer"},
        "Authorship": {"template": "{baseNamespace}instance/authorship/{sourceDocumentId}/{sourceSha256}/{recordPointerToken}/{authorArrayIndex}", "identityInputs": ["source_index.documentId", "SourceDocument.sourceSha256", "SourceRecord.recordJsonPointer", "author_ids array_index"], "encoding": "base64url_utf8_no_padding_for_pointer"},
        "Topic": {"template": "{baseNamespace}instance/topic/{conceptSha256}", "identityInputs": ["raw topic string"], "encoding": "unicode_nfkc_trim_casefold_then_sha256"},
        "Venue": {"template": "{baseNamespace}instance/venue/{conceptSha256}", "identityInputs": ["raw venue string"], "encoding": "unicode_nfkc_trim_casefold_then_sha256"},
        "Award": {"template": "{baseNamespace}instance/award/{conceptSha256}", "identityInputs": ["raw award string"], "encoding": "unicode_nfkc_trim_casefold_then_sha256"},
        "NewsItem": {"template": "{baseNamespace}instance/news/{sourceDocumentId}/{sourceSha256}/{newsPointerToken}", "identityInputs": ["source_index.documentId", "SourceDocument.sourceSha256", "news object JSON Pointer"], "encoding": "base64url_utf8_no_padding_for_pointer"},
    }
    mappings: dict[str, Any] = {}
    for class_id in classes:
        if class_id in abstract:
            mappings[class_id] = {
                "instantiationPolicy": "abstract_no_direct_instances",
                "collisionPolicy": "not_applicable",
                "evidenceRefs": [source_ref],
            }
            continue
        specification = specifications.get(class_id)
        if specification is None:
            mappings[class_id] = {
                "instantiationPolicy": "content_addressed_from_source_evidence",
                "template": "{baseNamespace}instance/other/{classId}/{sourceEvidenceSha256}",
                "identityInputs": ["canonical source evidence JSON"],
                "encoding": "sha256",
                "collisionPolicy": "hash_collision_is_fatal",
                "evidenceRefs": [source_ref],
            }
        else:
            mappings[class_id] = {
                "instantiationPolicy": "deterministic",
                **specification,
                "collisionPolicy": "same_normalized_identity_merges; hash_or_encoded_identity_collision_is_fatal",
                "evidenceRefs": [source_ref],
            }
    return {
        "contractVersion": "dataelf-iri-generation.v1",
        "baseNamespace": namespace,
        "stringNormalization": {
            "businessIds": "preserve_then_rfc3986_percent_encode_utf8",
            "conceptKeys": "Unicode NFKC, trim Unicode whitespace, casefold, then SHA-256 UTF-8",
            "displayLabels": "preserve original per observation; canonical label requires consensus",
            "jsonPointers": "RFC 6901 UTF-8 encoded as unpadded base64url",
        },
        "classMappings": mappings,
        "evidenceRefs": [source_ref],
    }


def _shacl_contract(ontology: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    source_ref = evidence["sourceIndexEvidenceRef"]
    required_shapes = [
        class_id for class_id in (
            "Paper", "Scholar", "Institution", "EntityObservation", "SourceDocument",
            "SearchResponse", "SourceRecord", "SourceFragment", "Authorship", "NewsItem",
        ) if class_id in _object(ontology.get("classes"))
    ]
    return {
        "contractVersion": "dataelf-shacl-contract.v1",
        "artifact": "shacl.ttl",
        "generation": "deterministic_from_validated_ontology_and_grounding",
        "requiredShapeClassIds": required_shapes,
        "requiredConstraintKinds": [
            "required_business_keys", "exact_observation_endpoints", "exact_source_ownership",
            "positive_author_order", "datatype_lexical_space", "inverse_properties",
            "observation_subproperties", "authoredBy_authorship_consistency",
        ],
        "missingnessPolicy": {
            "NewsItem.newsTitle": "required exactly once",
            "NewsItem.newsDate": "optional at most once; absence remains unknown",
            "NewsItem.newsSource": "optional at most once; absence remains unknown",
        },
        "evidenceRefs": [source_ref],
    }


def _source_access_paths(ontology: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    source_ref = evidence["sourceIndexEvidenceRef"]
    semantic_prefixes = {
        "DomainEntity": ["^observesEntity"],
        "Paper": ["^observesEntity"],
        "Scholar": ["^observesEntity"],
        "Institution": ["^observesEntity"],
        # Mutable relationship arrays are represented first as observation
        # snapshots.  These routes remain executable even when the model
        # deliberately omits an unsafe stable domain projection.
        "Topic": ["^observationHasTopic"],
        "Venue": ["^observationHasVenue"],
        "Award": ["^observationHasAward"],
        "NewsItem": ["newsItemFromFragment"],
        "Authorship": ["authorshipFromObservation"],
        "EntityObservation": [],
        "PaperObservation": [],
        "ScholarObservation": [],
        "InstitutionObservation": [],
    }
    result: dict[str, Any] = {}
    for class_id in ontology["classes"]:
        if class_id == "SourceDocument":
            steps: list[str] = []
            locators = ["sourcePath", "sourceSha256"]
        elif class_id == "SearchResponse":
            steps = ["responseHasFragment", "fragmentFromDocument"]
            locators = ["jsonPointer", "sourcePath", "sourceSha256"]
        elif class_id == "SourceRecord":
            steps = ["recordHasFragment", "fragmentFromDocument"]
            locators = ["recordJsonPointer", "jsonPointer", "sourcePath", "sourceSha256"]
        elif class_id == "SourceFragment":
            steps = ["fragmentFromDocument"]
            locators = ["jsonPointer", "sourcePath", "sourceSha256"]
        elif class_id == "NewsItem":
            # News is nested beneath an institution response and may be absent
            # from a particular acquisition.  Keep the required ontology class
            # executable without depending on an optional observation snapshot.
            steps = ["newsItemFromFragment", "fragmentFromDocument"]
            locators = ["jsonPointer", "sourcePath", "sourceSha256"]
        else:
            steps = [*semantic_prefixes.get(class_id, ["^observesEntity"]), "observationFromRecord", "recordHasFragment", "fragmentFromDocument"]
            locators = ["resultRank", "recordHash", "recordJsonPointer", "jsonPointer", "sourcePath", "sourceSha256"]
        result[class_id] = {
            "startClassId": class_id,
            "steps": steps,
            "terminalClassId": "SourceDocument",
            "locatorPropertyIds": locators,
            "direction": "domain_to_raw",
            "evidenceRefs": [source_ref],
        }
    return result


def _apply_conflict_safe_domains(ontology: dict[str, Any], evidence: dict[str, Any]) -> None:
    profiles = _object(_object(evidence.get("sourceIndex")).get("entityProfiles"))
    mappings = {
        ("paper", "title"): ("title", "PaperObservation"),
        ("paper", "abstract"): ("abstract", "PaperObservation"),
        ("paper", "published_at"): ("publishedAt", "PaperObservation"),
        ("scholar", "name"): ("scholarName", "ScholarObservation"),
        ("scholar", "email"): ("email", "ScholarObservation"),
        ("scholar", "homepage"): ("homepage", "ScholarObservation"),
        ("institution", "name"): ("institutionName", "InstitutionObservation"),
        ("institution", "country"): ("country", "InstitutionObservation"),
        ("institution", "region"): ("region", "InstitutionObservation"),
    }
    datatypes = _object(ontology.get("datatypeProperties"))
    for (kind, field), (property_id, observation_class) in mappings.items():
        consensus = _object(_object(_object(profiles.get(kind)).get("fieldConsensus")).get(field))
        if int(consensus.get("conflictingEntityCount", 0) or 0) <= 0 or property_id not in datatypes:
            continue
        datatypes[property_id]["comment"] = (
            f"Canonical {field} projected only for business IDs whose observations agree; conflicts remain on {observation_class}."
        )


def _hint_resolutions(ontology: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    domain = evidence_result(evidence, evidence["domainHintsEvidenceRef"])
    source_ref = evidence["sourceIndexEvidenceRef"]
    domain_ref = evidence["domainHintsEvidenceRef"]
    entity_ids = {"Paper": "Paper", "Scholar": "Scholar", "Institution": "Institution"}
    relation_ids = {
        "Paper.AUTHORED_BY.Scholar": "authoredBy",
        "Paper.AFFILIATED_WITH.Institution": "affiliatedWithInstitution",
        "Scholar.AFFILIATED_WITH.Institution": "scholarAffiliatedWithInstitution",
        "Scholar.HAS_VENUE": "hasVenue",
    }
    result: dict[str, Any] = {}
    for name, hint in domain["entities"].items():
        class_id = entity_ids.get(name)
        if class_id in ontology["classes"]:
            refs = [ref for ref in (hint.get("identityEvidenceRef"), source_ref) if isinstance(ref, str)]
            result[f"entity:{name}"] = {"status": "implemented", "ontologyElementIds": [class_id], "reason": "Raw-backed entity identity is available.", "evidenceRefs": refs}
        else:
            result[f"entity:{name}"] = {"status": "omitted", "ontologyElementIds": [], "reason": "No raw endpoint instances support this hinted entity.", "evidenceRefs": [domain_ref, source_ref]}
    for name, hint in domain["relations"].items():
        property_id = relation_ids.get(name)
        if property_id in ontology["objectProperties"]:
            refs = [source_ref]
            refs.extend(ref for ref in (hint.get("sourceJoinEvidenceRef"), hint.get("targetJoinEvidenceRef")) if isinstance(ref, str))
            result[f"relation:{name}"] = {"status": "implemented", "ontologyElementIds": [property_id], "reason": "Raw relationship and normalized join evidence are available.", "evidenceRefs": list(dict.fromkeys(refs))}
        else:
            result[f"relation:{name}"] = {"status": "omitted", "ontologyElementIds": [], "reason": "No non-empty raw relationship instances support this hint.", "evidenceRefs": [domain_ref, source_ref]}
    return result


def _competency_questions(
    grounding: dict[str, Any], ontology: dict[str, Any], evidence: dict[str, Any], config: Stage1Config
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_elements = set(ontology["classes"]) | set(ontology["objectProperties"]) | set(ontology["datatypeProperties"])
    questions = [{"id": f"cq_{index:02d}", "question": question} for index, question in enumerate(config.ontology.competency_questions, 1)]
    plan_specs: dict[int, tuple[str, list[dict[str, Any]], str]] = {
        1: (
            "covered",
            [
                {"startClassId": "Paper", "steps": ["hasAuthorship", "authoredByScholar"], "resultClassId": "Scholar"},
                {"startClassId": "Paper", "steps": ["affiliatedWithInstitution"], "resultClassId": "Institution"},
            ],
            "Paper reaches Scholar through reified Authorship and reaches its authoritative Institution links directly.",
        ),
        2: (
            "covered",
            [
                {"startClassId": "Scholar", "steps": ["scholarAffiliatedWithInstitution"], "resultClassId": "Institution"},
                {"startClassId": "Scholar", "steps": ["^authoredBy"], "resultClassId": "Paper"},
                {"startClassId": "Scholar", "steps": ["hasVenue"], "resultClassId": "Venue"},
            ],
            "Scholar has direct authoritative Institution and Venue navigation and inverse authoredBy navigation to Paper.",
        ),
        3: (
            "not_covered",
            [
                {"startClassId": "Institution", "steps": ["^observesInstitution", "fundingTotalUsd"], "resultDatatype": "xsd:integer"},
            ],
            "No FundingEvent or funding-round records exist. The fallback exposes only observation-scoped institution funding totals and does not invent events.",
        ),
        4: (
            "covered",
            [
                {"startClassId": "Paper", "steps": ["hasTopic"], "resultClassId": "Topic"},
                {"startClassId": "Paper", "steps": ["hasVenue"], "resultClassId": "Venue"},
                {"startClassId": "Paper", "steps": ["hasAward"], "resultClassId": "Award"},
                {"startClassId": "Paper", "steps": ["^observesPaper", "citationCount"], "resultDatatype": "xsd:integer"},
                {"startClassId": "Paper", "steps": ["^observesPaper", "hotnessDay"], "resultDatatype": "xsd:integer"},
            ],
            "Paper concept navigation is direct; mutable citation and time-window metrics are reached through PaperObservation.",
        ),
        5: (
            "covered",
            [
                {"startClassId": "DomainEntity", "steps": ["^observesEntity", "observationFromRecord", "recordJsonPointer"], "resultDatatype": "xsd:string"},
                {"startClassId": "DomainEntity", "steps": ["^observesEntity", "observationFromRecord", "recordHasFragment", "jsonPointer"], "resultDatatype": "xsd:string"},
                {"startClassId": "DomainEntity", "steps": ["^observesEntity", "observationFromRecord", "recordHasFragment", "fragmentFromDocument", "sourcePath"], "resultDatatype": "xsd:string"},
            ],
            "Every entity reaches its observation, exact SourceRecord/SourceFragment pointers, and workspace-relative SourceDocument path; every ontology element also has a sourceBinding.",
        ),
    }
    classes = ontology["classes"]
    objects = ontology["objectProperties"]
    datatypes = ontology["datatypeProperties"]

    def class_compatible(actual: Any, expected: Any, visited: set[str] | None = None) -> bool:
        if not isinstance(actual, str) or not isinstance(expected, str):
            return False
        if actual == expected:
            return True
        visited = set() if visited is None else visited
        if actual in visited:
            return False
        visited.add(actual)
        return any(
            class_compatible(parent, expected, visited)
            for parent in _array(_object(classes.get(actual)).get("subClassOf"))
        )

    def executable_path(spec: dict[str, Any]) -> dict[str, Any] | None:
        """Return a path whose declared result is derived from the ontology."""

        start = spec.get("startClassId")
        if start not in classes:
            return None
        current_class: Any = start
        result_datatype: str | None = None
        steps = [str(step) for step in _array(spec.get("steps"))]
        for position, step in enumerate(steps):
            inverse = step.startswith("^")
            property_id = step.removeprefix("^")
            if property_id in objects:
                if result_datatype is not None:
                    return None
                prop = _object(objects[property_id])
                expected = prop.get("range") if inverse else prop.get("domain")
                if not class_compatible(current_class, expected):
                    return None
                current_class = prop.get("domain") if inverse else prop.get("range")
            elif property_id in datatypes:
                prop = _object(datatypes[property_id])
                if inverse or position != len(steps) - 1 or not class_compatible(current_class, prop.get("domain")):
                    return None
                result_datatype = str(prop.get("range"))
            else:
                return None
        result = {"startClassId": start, "steps": steps}
        if result_datatype is not None:
            result["resultDatatype"] = result_datatype
        else:
            result["resultClassId"] = current_class
        return result

    coverage: dict[str, Any] = {}
    for index, question in enumerate(questions, 1):
        status, paths, explanation = plan_specs.get(
            index,
            ("not_covered", [], "No source-grounded executable query plan is declared for this configured question."),
        )
        executable_paths = [path for raw_path in paths if (path := executable_path(raw_path)) is not None]
        if status == "covered" and not executable_paths:
            status = "not_covered"
            explanation += " No configured fallback path is executable in this candidate ontology."
        elements: list[str] = []
        for path in executable_paths:
            start = path.get("startClassId")
            if start in all_elements:
                elements.append(start)
            elements.extend(step.removeprefix("^") for step in path.get("steps", []) if step.removeprefix("^") in all_elements)
            result_class = path.get("resultClassId")
            if result_class in all_elements:
                elements.append(result_class)
        if not elements:
            # not_covered still needs an explicit, existing ontology element to
            # document the semantic boundary of the unsupported question.
            preferred_start = next(
                (raw_path.get("startClassId") for raw_path in paths if raw_path.get("startClassId") in classes),
                next(iter(classes), None),
            )
            if isinstance(preferred_start, str):
                elements.append(preferred_start)
        coverage[question["id"]] = {
            "status": status,
            "ontologyElementIds": list(dict.fromkeys(elements)),
            "evidenceRefs": [evidence["sourceIndexEvidenceRef"]],
            "explanation": explanation,
            "queryPlan": {
                "paths" if status == "covered" else "fallbackPaths": executable_paths,
                "inverseStepPrefix": "^",
            },
        }
    return questions, coverage


def _association_and_authority(
    grounding: dict[str, Any], ontology: dict[str, Any], evidence: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_ref = evidence["sourceIndexEvidenceRef"]
    objects = ontology["objectProperties"]

    def endpoint_property(range_id: str, preferred: str) -> str | None:
        preferred_value = _object(objects.get(preferred))
        if preferred_value.get("domain") == "Authorship" and preferred_value.get("range") == range_id:
            return preferred
        return next(
            (
                identifier
                for identifier, value in objects.items()
                if _object(value).get("domain") == "Authorship" and _object(value).get("range") == range_id
            ),
            None,
        )

    endpoint_ids = [
        identifier
        for identifier in (
            endpoint_property("Paper", "authorshipOfPaper"),
            endpoint_property("Scholar", "authoredByScholar"),
        )
        if isinstance(identifier, str)
    ]
    association = {
        "Authorship": {
            "endpointPropertyIds": endpoint_ids,
            "qualifiers": {
                "authorOrder": {"propertyId": "authorOrder", "formula": "array_index + 1"},
                "isFirstAuthor": {"propertyId": "isFirstAuthor", "formula": "array_index == 0"},
            },
            "authorityEndpoint": "/openapi/paper/search",
            "authorityPathPattern": "/data/list/*/author_ids/*",
            "evidenceRefs": [source_ref],
        }
    }
    authority: dict[str, Any] = {}
    for name, comparison in _object(_object(evidence.get("sourceIndex")).get("relationComparisons")).items():
        authority[name] = {
            **deepcopy(comparison),
            "corroborationSemantics": "membership_corroboration" if name == "Paper-Scholar" else "non_exhaustive_highlight",
            "evidenceRefs": [source_ref],
        }
    return association, authority


def normalize_candidate_contract(
    candidate: dict[str, Any], evidence: dict[str, Any], config: Stage1Config
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert semantically usable model output into the controller-owned v2 JSON contract."""

    if evidence.get("sourceType") != "ai_index_raw":
        return candidate, {"applied": False, "reason": "non_raw_compatibility_mode"}
    raw_grounding = _normalize_grounding_property_names(_object(candidate.get("grounding")))
    ontology = _normalize_ontology(candidate, config, evidence["sourceFingerprint"])
    _apply_conflict_safe_domains(ontology, evidence)
    _prune_absent_relation_snapshots(ontology, evidence)
    object_evidence, datatype_evidence = _property_evidence(raw_grounding, ontology, evidence)
    _deduplicate_datatype_raw_bindings(ontology, datatype_evidence)
    # A model may declare attractive fields that do not exist in this exact
    # acquisition (for example an award key or a previous-heat metric).  The
    # formal contract cannot retain a datatype property with an empty evidence
    # array, and asking the model to invent evidence during a repair round is
    # unsafe.  Omit only those ungrounded model properties; controller-owned
    # provenance properties all receive deterministic source-index evidence.
    for identifier in list(ontology["datatypeProperties"]):
        if _array(datatype_evidence.get(identifier)):
            continue
        ontology["datatypeProperties"].pop(identifier, None)
        datatype_evidence.pop(identifier, None)
    questions, cq_coverage = _competency_questions(raw_grounding, ontology, evidence, config)
    association, authority = _association_and_authority(raw_grounding, ontology, evidence)
    grounding = {
        "schemaVersion": "dataelf-grounding.v2",
        "sourceFingerprint": evidence["sourceFingerprint"],
        "tableClassifications": _table_classifications(ontology, evidence),
        "columnClassifications": _column_classifications(ontology, evidence),
        "classEvidence": _class_evidence(raw_grounding, ontology, evidence),
        "objectPropertyEvidence": object_evidence,
        "datatypePropertyEvidence": datatype_evidence,
        "entityObservationMappings": _observation_mappings(ontology, evidence),
        "accessPaths": deepcopy(_object(raw_grounding.get("accessPaths"))),
        "domainHintResolutions": _hint_resolutions(ontology, evidence),
        "competencyQuestions": questions,
        "cqCoverage": cq_coverage,
        "sourceCoverage": _source_coverage(evidence),
        "sourceBindings": {},
        "sourceAccessPaths": _source_access_paths(ontology, evidence),
        "rawPathClassifications": deepcopy(_object(raw_grounding.get("rawPathClassifications"))),
        "associationMappings": association,
        "entityResolutionMappings": _entity_resolution_mappings(evidence),
        "responseObservationMappings": _response_observation_mappings(evidence),
        "relationAuthority": authority,
        "observationValueMappings": _observation_value_mappings(ontology, evidence),
        "relationSnapshotMappings": _relation_snapshot_mappings(ontology, evidence),
        "iriGenerationMappings": _iri_generation_mappings(ontology, config, evidence),
        "shaclContract": _shacl_contract(ontology, evidence),
        "normalizationEvidenceRefs": [evidence["normalizationLineageEvidenceRef"]],
    }
    normalized = {"ontology": ontology, "grounding": grounding}
    # Reuse the same deterministic binding builder that seeds repair runtimes.
    from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.model_runtime import _controller_source_bindings

    grounding["sourceBindings"] = _controller_source_bindings(
        normalized, evidence, AI_INDEX_SOURCE_ENDPOINT_TARGETS
    )
    return normalized, {
        "applied": True,
        "classCount": len(ontology["classes"]),
        "objectPropertyCount": len(ontology["objectProperties"]),
        "datatypePropertyCount": len(ontology["datatypeProperties"]),
        "sourceBindingCount": len(grounding["sourceBindings"]),
    }


class AIIndexOntologyAdapter:
    source_endpoint_targets = AI_INDEX_SOURCE_ENDPOINT_TARGETS
    generator_system = stage1_prompts.GENERATOR_SYSTEM
    reviewer_system = stage1_prompts.REVIEWER_SYSTEM
    candidate_from_semantic_plan = staticmethod(candidate_from_semantic_plan)
    normalize_candidate_contract = staticmethod(normalize_candidate_contract)
    generator_prompt = staticmethod(stage1_prompts.generator_prompt)
    semantic_plan_prompt = staticmethod(stage1_prompts.semantic_plan_prompt)
    reviewer_prompt = staticmethod(stage1_prompts.reviewer_prompt)
    compact_reviewer_prompt = staticmethod(stage1_prompts.compact_reviewer_prompt)
    prompt_fingerprint = staticmethod(stage1_prompts.prompt_fingerprint)
    repair_feedback = staticmethod(stage1_prompts.repair_feedback)
    validate_candidate = staticmethod(stage1_validation.validate_candidate)
    validate_review = staticmethod(stage1_validation.validate_review)
    build_shacl_ttl = staticmethod(stage1_shacl.build_shacl_ttl)
    shacl_contract_errors = staticmethod(stage1_shacl.shacl_contract_errors)


AI_INDEX_ONTOLOGY_ADAPTER = AIIndexOntologyAdapter()


__all__ = [
    "AI_INDEX_ONTOLOGY_ADAPTER",
    "AIIndexOntologyAdapter",
    "candidate_from_semantic_plan",
    "normalize_candidate_contract",
]
