from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import file_sha256, sha256_json


SOURCE_INDEX_VERSION = "dataelf-source-index.v2"
LINEAGE_VERSION = "dataelf-normalization-lineage.v2"


def _pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _pointer_unescape(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def json_pointer_get(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise KeyError(f"invalid JSON Pointer: {pointer}")
    current = document
    for token in pointer[1:].split("/"):
        key = _pointer_unescape(token)
        if isinstance(current, list):
            if not key.isdigit() or int(key) >= len(current):
                raise KeyError(pointer)
            current = current[int(key)]
        elif isinstance(current, dict) and key in current:
            current = current[key]
        else:
            raise KeyError(pointer)
    return current


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _endpoint_kind(endpoint: str) -> str | None:
    if "paper/search" in endpoint:
        return "paper"
    if "scholar/search" in endpoint:
        return "scholar"
    if "institutions/search" in endpoint:
        return "institution"
    if "funding-profile" in endpoint:
        return "funding"
    return None


def _walk_fragments(value: Any, pointer: str = "", pattern: str = "") -> Iterable[tuple[str, str, Any]]:
    # Index containers as well as leaves.  This makes request/envelope objects,
    # records, nested objects, arrays and individual array elements independently
    # replayable without copying their values into the ontology bundle.
    yield pointer, pattern, value
    if isinstance(value, dict):
        for key, child in value.items():
            escaped = _pointer_escape(str(key))
            yield from _walk_fragments(child, f"{pointer}/{escaped}", f"{pattern}/{escaped}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_fragments(child, f"{pointer}/{index}", f"{pattern}/*")
        return


_SEMANTIC_SUFFIXES = {
    "/id", "/title", "/abstract", "/published_at", "/venue", "/fields/*",
    "/author_ids/*", "/institution_ids/*", "/awards/*", "/name", "/email",
    "/homepage", "/paper_ids/*", "/venues/*", "/country", "/region",
    "/related_paper_ids/*", "/related_scholar_ids/*", "/news/*/title",
    "/news/*/date", "/news/*/source",
}
_OBSERVATION_SUFFIXES = {
    "/citation_count", "/paper_count", "/scholar_count", "/funding_total_usd",
    "/hotness/day", "/hotness/week", "/hotness/month", "/hotness/half_year",
    "/hotness/previous_half_year", "/impact/paper", "/impact/talent", "/impact/news",
    "/impact/funding", "/impact/industry",
}


def classify_raw_path(pattern: str) -> str:
    if pattern.startswith("/raw/") or pattern == "/raw":
        return "redundant_but_source_linked"
    if pattern.startswith("/request/") or pattern in {
        "/source", "/mode", "/method", "/endpoint", "/trace_id", "/data/total",
    }:
        return "observation_promoted"
    if pattern.startswith("/data/list/*"):
        suffix = pattern.removeprefix("/data/list/*")
        if suffix in _SEMANTIC_SUFFIXES:
            return "semantic_promoted"
        if suffix in _OBSERVATION_SUFFIXES:
            return "observation_promoted"
    return "source_only"


def _relation_comparisons(observations: dict[str, dict[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    """Compare independently exposed relationship views without treating highlights as exhaustive."""

    papers = observations.get("paper", {})
    scholars = observations.get("scholar", {})
    institutions = observations.get("institution", {})
    paper_author = {
        (paper_id, str(scholar_id))
        for paper_id, items in papers.items()
        for item in items
        for scholar_id in item.get("author_ids", [])
    }
    scholar_paper = {
        (str(paper_id), scholar_id)
        for scholar_id, items in scholars.items()
        for item in items
        for paper_id in item.get("paper_ids", [])
    }
    paper_institution = {
        (paper_id, str(institution_id))
        for paper_id, items in papers.items()
        for item in items
        for institution_id in item.get("institution_ids", [])
    }
    scholar_institution = {
        (scholar_id, str(institution_id))
        for scholar_id, items in scholars.items()
        for item in items
        for institution_id in item.get("institution_ids", [])
    }
    highlighted_papers = {
        (str(paper_id), institution_id)
        for institution_id, items in institutions.items()
        for item in items
        for paper_id in item.get("related_paper_ids", [])
    }
    highlighted_scholars = {
        (str(scholar_id), institution_id)
        for institution_id, items in institutions.items()
        for item in items
        for scholar_id in item.get("related_scholar_ids", [])
    }

    def comparison(
        authoritative: set[tuple[str, str]],
        corroborating: set[tuple[str, str]],
        authority: str,
        support: str,
        strategy: str,
        support_observed: bool = True,
    ) -> dict[str, Any]:
        return {
            "authority": authority,
            "corroboration": support if support_observed else None,
            "corroborationStatus": "observed" if support_observed else "path_absent_in_acquisition",
            "expectedCorroborationPath": support,
            "differenceStrategy": (
                strategy
                if support_observed
                else f"{strategy} The expected corroboration path is absent in this acquisition; no corroboration projection is executable."
            ),
            "authoritativeCount": len(authoritative),
            "corroboratingCount": len(corroborating),
            "intersectionCount": len(authoritative & corroborating),
            "authorityOnlyCount": len(authoritative - corroborating),
            "corroborationOnlyCount": len(corroborating - authoritative),
        }

    return {
        "Paper-Scholar": comparison(
            paper_author,
            scholar_paper,
            "/openapi/paper/search:/data/list/*/author_ids/*",
            "/openapi/scholar/search:/data/list/*/paper_ids/*",
            "Use paper.author_ids order as authoritative authorship; scholar.paper_ids only corroborates membership and may be non-exhaustive.",
            any("paper_ids" in item for items in scholars.values() for item in items),
        ),
        "Paper-Institution": comparison(
            paper_institution,
            highlighted_papers,
            "/openapi/paper/search:/data/list/*/institution_ids/*",
            "/openapi/institutions/search:/data/list/*/related_paper_ids/*",
            "Use paper.institution_ids for direct paper-institution links; institution.related_paper_ids is a non-exhaustive highlight relation.",
            any("related_paper_ids" in item for items in institutions.values() for item in items),
        ),
        "Scholar-Institution": comparison(
            scholar_institution,
            highlighted_scholars,
            "/openapi/scholar/search:/data/list/*/institution_ids/*",
            "/openapi/institutions/search:/data/list/*/related_scholar_ids/*",
            "Use scholar.institution_ids for affiliation-like links; institution.related_scholar_ids is a non-exhaustive highlight relation.",
            any("related_scholar_ids" in item for items in institutions.values() for item in items),
        ),
    }


def build_source_index(raw_source: Path, source_fingerprint: str, raw_subdir: str) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    fragments: list[dict[str, Any]] = []
    profiles: dict[str, dict[str, Any]] = {}
    record_by_pointer: dict[tuple[str, str], str] = {}
    entity_observations: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))

    for path in sorted(raw_source.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"raw document must be an object: {path}")
        relative = path.relative_to(raw_source).as_posix()
        workspace_relative = (Path(raw_subdir) / relative).as_posix()
        digest = file_sha256(path)
        document_id = "srcdoc_" + sha256_json({"path": workspace_relative, "sha256": digest})[:20]
        endpoint = str(document.get("endpoint", ""))
        entity_kind = _endpoint_kind(endpoint)
        data = document.get("data")
        items = data.get("list", []) if isinstance(data, dict) and isinstance(data.get("list"), list) else []
        total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
        response_id = "response_" + sha256_json({"documentId": document_id})[:20]
        documents.append(
            {
                "documentId": document_id,
                "responseId": response_id,
                "relativeFile": workspace_relative,
                "endpoint": endpoint,
                "method": document.get("method"),
                "mode": document.get("mode"),
                "traceId": document.get("trace_id"),
                "request": document.get("request", {}),
                "resultCount": total,
                "recordCount": len(items),
                "empty": not items,
                "sizeBytes": path.stat().st_size,
                "sha256": digest,
            }
        )
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            pointer = f"/data/list/{index}"
            business_id = str(item.get("id") or item.get(f"{entity_kind}_id") or "")
            record_id = "srcrec_" + sha256_json({"documentId": document_id, "pointer": pointer})[:20]
            record_by_pointer[(document_id, pointer)] = record_id
            records.append(
                {
                    "recordId": record_id,
                    "documentId": document_id,
                    "responseId": response_id,
                    "entityKind": entity_kind,
                    "businessId": business_id,
                    "resultRank": index + 1,
                    "jsonPointer": pointer,
                    "recordHash": sha256_json(item),
                }
            )
            if entity_kind and business_id:
                entity_observations[entity_kind][business_id].append(item)
        for pointer, pattern, value in _walk_fragments(document):
            record_pointer = ""
            pieces = pointer.split("/")
            if len(pieces) >= 4 and pieces[1:3] == ["data", "list"] and pieces[3].isdigit():
                record_pointer = f"/data/list/{pieces[3]}"
            profile_key = f"{endpoint}|{pattern}"
            profile = profiles.setdefault(
                profile_key,
                {
                    "endpoint": endpoint,
                    "pathPattern": pattern,
                    "classification": classify_raw_path(pattern),
                    "occurrenceCount": 0,
                    "documentIds": set(),
                    "valueKinds": set(),
                    "samplePointers": [],
                },
            )
            profile["occurrenceCount"] += 1
            profile["documentIds"].add(document_id)
            profile["valueKinds"].add(_kind(value))
            if len(profile["samplePointers"]) < 3:
                profile["samplePointers"].append({"documentId": document_id, "jsonPointer": pointer})
            fragments.append(
                {
                    "fragmentId": "srcfrag_" + sha256_json({"documentId": document_id, "pointer": pointer})[:20],
                    "documentId": document_id,
                    "responseId": response_id,
                    "recordId": record_by_pointer.get((document_id, record_pointer)),
                    "jsonPointer": pointer,
                    "pathPattern": pattern,
                    "valueKind": _kind(value),
                    "valueHash": sha256_json(value),
                    "classification": classify_raw_path(pattern),
                }
            )

    path_profiles: dict[str, Any] = {}
    for key, profile in sorted(profiles.items()):
        path_profiles[key] = {
            **profile,
            "documentIds": sorted(profile["documentIds"]),
            "documentCount": len(profile["documentIds"]),
            "valueKinds": sorted(profile["valueKinds"]),
        }
    stable_fields = {
        "paper": ("title", "abstract", "published_at", "venue"),
        "scholar": ("name", "email", "homepage"),
        "institution": ("name", "country", "region"),
    }
    entity_profiles: dict[str, Any] = {}
    for entity_kind in ("paper", "scholar", "institution"):
        observations = [item for item in records if item.get("entityKind") == entity_kind]
        business_ids = {str(item.get("businessId")) for item in observations if item.get("businessId")}
        field_consensus: dict[str, Any] = {}
        conflicting_entities: set[str] = set()
        for field in stable_fields[entity_kind]:
            consensus = 0
            missing = 0
            conflicts: list[str] = []
            for business_id in sorted(business_ids):
                records_for_entity = entity_observations[entity_kind].get(business_id, [])
                values = {
                    sha256_json(record[field])
                    for record in records_for_entity
                    if field in record and record[field] is not None
                }
                if not values:
                    missing += 1
                elif len(values) == 1:
                    consensus += 1
                else:
                    conflicts.append(business_id)
                    conflicting_entities.add(business_id)
            field_consensus[field] = {
                "policy": "require_consensus",
                "consensusEntityCount": consensus,
                "conflictingEntityCount": len(conflicts),
                "missingEntityCount": missing,
                "conflictBusinessIds": conflicts,
                "conflictDisposition": "observation_only",
            }
        entity_profiles[entity_kind] = {
            "businessEntityCount": len(business_ids),
            "observationCount": len(observations),
            "mergeKey": "businessId",
            "mergePolicy": "require_consensus",
            "conflictingEntityCount": len(conflicting_entities),
            "fieldConsensus": field_consensus,
        }
    result = {
        "schemaVersion": SOURCE_INDEX_VERSION,
        "sourceFingerprint": source_fingerprint,
        "documents": documents,
        "records": records,
        "fragments": fragments,
        "pathProfiles": path_profiles,
        "relationComparisons": _relation_comparisons(entity_observations),
        "entityProfiles": entity_profiles,
        "metrics": {
            "documentCount": len(documents),
            "nonEmptyResponseCount": sum(not item["empty"] for item in documents),
            "emptyResponseCount": sum(item["empty"] for item in documents),
            "recordCount": len(records),
            "fragmentCount": len(fragments),
            "pathPatternCount": len(path_profiles),
            "unclassifiedPathCount": 0,
        },
    }
    result["sourceIndexSha256"] = sha256_json(result)
    return result


def replay_source_index(workspace: Path, index: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    documents: dict[str, Any] = {}
    metadata_by_id = {str(item.get("documentId")): item for item in index.get("documents", []) if isinstance(item, dict)}
    for document_id, metadata in metadata_by_id.items():
        path = workspace / str(metadata.get("relativeFile", ""))
        if not path.is_file():
            errors.append(f"missing raw document {metadata.get('relativeFile')}")
            continue
        if file_sha256(path) != metadata.get("sha256"):
            errors.append(f"raw document hash mismatch {metadata.get('relativeFile')}")
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            documents[document_id] = document
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"cannot parse raw document {metadata.get('relativeFile')}: {exc}")
            continue
        data = document.get("data") if isinstance(document, dict) else None
        items = data.get("list", []) if isinstance(data, dict) and isinstance(data.get("list"), list) else []
        total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
        expected_metadata = {
            "endpoint": document.get("endpoint") if isinstance(document, dict) else None,
            "method": document.get("method") if isinstance(document, dict) else None,
            "mode": document.get("mode") if isinstance(document, dict) else None,
            "traceId": document.get("trace_id") if isinstance(document, dict) else None,
            "request": document.get("request", {}) if isinstance(document, dict) else {},
            "resultCount": total,
            "recordCount": len(items),
            "empty": not items,
            "sizeBytes": path.stat().st_size,
        }
        for field, expected_value in expected_metadata.items():
            if metadata.get(field) != expected_value:
                errors.append(f"document metadata mismatch {document_id}:{field}")
        expected_response_id = "response_" + sha256_json({"documentId": document_id})[:20]
        if metadata.get("responseId") != expected_response_id:
            errors.append(f"document response ID mismatch {document_id}")
    record_ids: dict[tuple[str, str], str] = {}
    for collection, hash_field in (("records", "recordHash"), ("fragments", "valueHash")):
        for item in index.get(collection, []):
            if not isinstance(item, dict):
                errors.append(f"invalid {collection} entry")
                continue
            document = documents.get(str(item.get("documentId")))
            if document is None:
                continue
            pointer = str(item.get("jsonPointer", ""))
            try:
                value = json_pointer_get(document, pointer)
            except KeyError:
                errors.append(f"unresolvable JSON Pointer {item.get('documentId')}:{pointer}")
                continue
            if sha256_json(value) != item.get(hash_field):
                errors.append(f"JSON Pointer hash mismatch {item.get('documentId')}:{pointer}")
            metadata = metadata_by_id.get(str(item.get("documentId")), {})
            if item.get("responseId") != metadata.get("responseId"):
                errors.append(f"response link mismatch {collection}:{item.get('documentId')}:{pointer}")
            if collection == "records":
                pieces = pointer.split("/")
                if len(pieces) != 4 or pieces[1:3] != ["data", "list"] or not pieces[3].isdigit():
                    errors.append(f"record pointer is not an exact data.list element {item.get('documentId')}:{pointer}")
                    continue
                expected_rank = int(pieces[3]) + 1
                if item.get("resultRank") != expected_rank:
                    errors.append(f"record resultRank mismatch {item.get('documentId')}:{pointer}; expected {expected_rank}")
                expected_kind = _endpoint_kind(str(metadata.get("endpoint", "")))
                expected_id = str(value.get("id") or value.get(f"{expected_kind}_id") or "") if isinstance(value, dict) else ""
                if item.get("entityKind") != expected_kind or item.get("businessId") != expected_id:
                    errors.append(f"record business identity mismatch {item.get('documentId')}:{pointer}")
                expected_record_id = "srcrec_" + sha256_json({"documentId": item.get("documentId"), "pointer": pointer})[:20]
                if item.get("recordId") != expected_record_id:
                    errors.append(f"record ID mismatch {item.get('documentId')}:{pointer}")
                record_ids[(str(item.get("documentId")), pointer)] = expected_record_id
            else:
                expected_class = classify_raw_path(str(item.get("pathPattern", "")))
                if item.get("classification") != expected_class:
                    errors.append(f"fragment classification mismatch {item.get('documentId')}:{pointer}")
                pieces = pointer.split("/")
                record_pointer = f"/data/list/{pieces[3]}" if len(pieces) >= 4 and pieces[1:3] == ["data", "list"] and pieces[3].isdigit() else ""
                if item.get("recordId") != record_ids.get((str(item.get("documentId")), record_pointer)):
                    errors.append(f"fragment record link mismatch {item.get('documentId')}:{pointer}")
    indexed_fragments: dict[str, set[str]] = defaultdict(set)
    for item in index.get("fragments", []):
        if isinstance(item, dict):
            indexed_fragments[str(item.get("documentId"))].add(str(item.get("jsonPointer", "")))
    for document_id, document in documents.items():
        discovered = {pointer for pointer, _pattern, _value in _walk_fragments(document)}
        if indexed_fragments.get(document_id, set()) != discovered:
            errors.append(f"fragment pointer coverage mismatch {document_id}")
    metrics = index.get("metrics", {}) if isinstance(index.get("metrics"), dict) else {}
    expected_metrics = {
        "documentCount": len(metadata_by_id),
        "nonEmptyResponseCount": sum(not bool(item.get("empty")) for item in metadata_by_id.values()),
        "emptyResponseCount": sum(bool(item.get("empty")) for item in metadata_by_id.values()),
        "recordCount": len(index.get("records", [])),
        "fragmentCount": len(index.get("fragments", [])),
        "pathPatternCount": len(index.get("pathProfiles", {})),
        "unclassifiedPathCount": 0,
    }
    for field, expected_value in expected_metrics.items():
        if metrics.get(field) != expected_value:
            errors.append(f"source index metric mismatch {field}; expected {expected_value}")
    expected = sha256_json({key: value for key, value in index.items() if key != "sourceIndexSha256"})
    if index.get("sourceIndexSha256") != expected:
        errors.append("source index canonical hash mismatch")
    return errors


def _rule(
    endpoint: str,
    path: str,
    transform: str,
    *,
    formula: str = "",
    eligible: bool = True,
    deprecation_note: str = "",
) -> dict[str, Any]:
    result = {
        "endpoint": endpoint,
        "rawPaths": [path],
        "transform": transform,
        "formula": formula,
        "nullPolicy": "preserve_unknown",
        "ontologyEligible": eligible,
    }
    if deprecation_note:
        result["deprecationNote"] = deprecation_note
    return result


def _constant_default(endpoint: str, formula: str) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "rawPaths": [],
        "transform": "constant_default",
        "formula": formula,
        "nullPolicy": "preserve_unknown",
        "ontologyEligible": False,
    }


PAPER = "/openapi/paper/search"
SCHOLAR = "/openapi/scholar/search"
INSTITUTION = "/openapi/institutions/search"


_COLUMN_RULES: dict[tuple[str, str], dict[str, Any]] = {
    ("papers", "paper_id"): _rule(PAPER, "/data/list/*/id", "rename"),
    ("papers", "title"): _rule(PAPER, "/data/list/*/title", "direct"),
    ("papers", "abstract"): _rule(PAPER, "/data/list/*/abstract", "direct"),
    ("papers", "pub_date"): _rule(PAPER, "/data/list/*/published_at", "rename"),
    ("papers", "venue"): _rule(PAPER, "/data/list/*/venue", "direct"),
    ("papers", "heat"): _rule(PAPER, "/data/list/*/hotness/half_year", "rename"),
    ("papers", "previous_heat"): _rule(PAPER, "/data/list/*/hotness/previous_half_year", "rename"),
    ("papers", "citation_count"): _rule(PAPER, "/data/list/*/citation_count", "direct"),
    ("papers", "cited_by_count"): _rule(
        PAPER,
        "/data/list/*/citation_count",
        "fallback_alias",
        eligible=False,
        deprecation_note=(
            "Deprecated normalized-view alias of papers.citation_count; retained only for legacy "
            "table compatibility and MUST NOT be promoted or interpreted as an independent fact."
        ),
    ),
    ("papers", "domains"): _rule(PAPER, "/data/list/*/fields/*", "rename", eligible=False),
    ("papers", "first_authors"): _constant_default(PAPER, "[]; source has author_ids but no first_authors payload"),
    ("papers", "corresponding_authors"): _constant_default(PAPER, "[]; source has no corresponding-author qualifier"),
    ("papers", "institution_ids"): _rule(PAPER, "/data/list/*/institution_ids", "direct", eligible=False),
    ("papers", "institutions"): _constant_default(PAPER, "[]; source exposes institution_ids, not embedded institution objects"),
    ("papers", "conf_award_info"): _rule(
        PAPER,
        "/data/list/*/awards",
        "derived",
        formula="JSON object {'awards': raw awards array}; compatibility view only",
        eligible=False,
    ),
    ("papers", "sub_domains"): _constant_default(PAPER, "[]; source record has fields but no sub_domains record field"),
    ("papers", "count_by_year"): _constant_default(PAPER, "[]; no yearly metric payload is present"),
    ("paper_author", "paper_id"): _rule(PAPER, "/data/list/*/id", "explode"),
    ("paper_author", "scholar_id"): _rule(PAPER, "/data/list/*/author_ids/*", "explode"),
    ("paper_author", "author_order"): _rule(PAPER, "/data/list/*/author_ids/*", "derived", formula="array_index + 1"),
    ("paper_author", "is_first_author"): _rule(PAPER, "/data/list/*/author_ids/*", "derived", formula="array_index == 0"),
    ("paper_author", "is_corresponding_author"): _rule(PAPER, "/data/list/*/author_ids/*", "constant_default", formula="false when raw field is absent", eligible=False),
    ("paper_institution", "paper_id"): _rule(PAPER, "/data/list/*/id", "explode"),
    ("paper_institution", "institution_id"): _rule(PAPER, "/data/list/*/institution_ids/*", "explode"),
    ("paper_institution", "is_primary"): _rule(PAPER, "/data/list/*/institution_ids/*", "constant_default", formula="false because singular institution_id is absent", eligible=False),
    ("paper_awards", "paper_id"): _rule(PAPER, "/data/list/*/id", "explode"),
    ("paper_awards", "award_title"): _rule(PAPER, "/data/list/*/awards/*", "explode"),
    ("scholars", "scholar_id"): _rule(SCHOLAR, "/data/list/*/id", "rename"),
    ("scholars", "display_name"): _rule(SCHOLAR, "/data/list/*/name", "rename"),
    ("scholars", "email"): _rule(SCHOLAR, "/data/list/*/email", "direct"),
    ("scholars", "homepage"): _rule(SCHOLAR, "/data/list/*/homepage", "direct"),
    ("scholars", "heat"): _rule(SCHOLAR, "/data/list/*/hotness/half_year", "rename"),
    ("scholars", "previous_heat"): _rule(SCHOLAR, "/data/list/*/hotness/previous_half_year", "rename"),
    ("scholars", "paper_count"): _rule(SCHOLAR, "/data/list/*/paper_ids/*", "derived", formula="length(paper_ids)"),
    ("scholars", "domains"): _rule(SCHOLAR, "/data/list/*/fields/*", "rename", eligible=False),
    ("scholars", "conference_names"): _rule(SCHOLAR, "/data/list/*/venues/*", "rename", eligible=False),
    ("scholars", "institution_ids"): _rule(SCHOLAR, "/data/list/*/institution_ids", "direct", eligible=False),
    ("scholars", "institutions"): _constant_default(SCHOLAR, "[]; source exposes institution_ids, not embedded institution objects"),
    ("scholars", "count_by_year"): _constant_default(SCHOLAR, "[]; no yearly metric payload is present"),
    ("scholars", "sub_domains"): _constant_default(SCHOLAR, "[]; source record has fields but no sub_domains record field"),
    ("scholars", "journal_names"): _constant_default(SCHOLAR, "[]; source record has venues but no journal classification"),
    ("scholars", "journal_abbreviations"): _constant_default(SCHOLAR, "[]; no journal abbreviation payload is present"),
    ("scholars", "conference_abbreviations"): _constant_default(SCHOLAR, "[]; no conference abbreviation payload is present"),
    ("scholars", "award_list"): _rule(SCHOLAR, "/data/list/*/awards", "rename", eligible=False),
    ("scholar_institution", "scholar_id"): _rule(SCHOLAR, "/data/list/*/id", "explode"),
    ("scholar_institution", "institution_id"): _rule(SCHOLAR, "/data/list/*/institution_ids/*", "explode"),
    ("scholar_institution", "is_primary"): _rule(SCHOLAR, "/data/list/*/institution_ids/*", "constant_default", formula="false because singular institution_id is absent", eligible=False),
    ("scholar_awards", "scholar_id"): _rule(SCHOLAR, "/data/list/*/id", "explode"),
    ("scholar_awards", "award_title"): _rule(SCHOLAR, "/data/list/*/awards/*", "explode"),
    ("scholar_venues", "scholar_id"): _rule(SCHOLAR, "/data/list/*/id", "explode"),
    ("scholar_venues", "venue_name"): _rule(SCHOLAR, "/data/list/*/venues/*", "explode"),
    ("scholar_venues", "venue_type"): _constant_default(SCHOLAR, "'conference'; compatibility assumption with no raw venue-type qualifier"),
    ("institutions", "institution_id"): _rule(INSTITUTION, "/data/list/*/id", "rename"),
    ("institutions", "name"): _rule(INSTITUTION, "/data/list/*/name", "direct"),
    ("institutions", "country_code"): _rule(INSTITUTION, "/data/list/*/country", "rename"),
    ("institutions", "paper_count"): _rule(INSTITUTION, "/data/list/*/paper_count", "direct"),
    ("institutions", "scholar_count"): _rule(INSTITUTION, "/data/list/*/scholar_count", "direct"),
    ("institutions", "author_count"): _rule(
        INSTITUTION,
        "/data/list/*/scholar_count",
        "fallback_alias",
        eligible=False,
        deprecation_note=(
            "Deprecated normalized-view alias of institutions.scholar_count; retained only for legacy "
            "table compatibility and MUST NOT be promoted or interpreted as an independent fact."
        ),
    ),
    ("institutions", "heat"): _rule(INSTITUTION, "/data/list/*/hotness/half_year", "rename"),
    ("institutions", "previous_heat"): _rule(INSTITUTION, "/data/list/*/hotness/previous_half_year", "rename"),
    ("institutions", "funding_total_usd"): _rule(INSTITUTION, "/data/list/*/funding_total_usd", "direct"),
    ("institutions", "domains"): _rule(INSTITUTION, "/data/list/*/fields/*", "rename", eligible=False),
    ("institutions", "sub_tags"): _constant_default(INSTITUTION, "[]; no sub_tags payload is present"),
    ("institutions", "conference_names"): _constant_default(INSTITUTION, "[]; no institution venue payload is present"),
    ("institutions", "journal_names"): _constant_default(INSTITUTION, "[]; no institution venue payload is present"),
    ("institutions", "award_list"): _constant_default(INSTITUTION, "[]; no institution award payload is present"),
}


def build_normalization_lineage(tables: Iterable[Any], source_index: dict[str, Any]) -> dict[str, Any]:
    profiles = source_index.get("pathProfiles", {})
    mappings: dict[str, Any] = {}
    for table in tables:
        for column in table.columns:
            coordinate = f"{table.name}.{column}"
            if column == "source_raw":
                rule = {
                    "endpoint": "*",
                    "rawPaths": ["$document.relativeFile"],
                    "transform": "synthetic_id",
                    "formula": "workspace-relative raw document path",
                    "nullPolicy": "forbidden",
                    "ontologyEligible": True,
                }
            else:
                rule = dict(_COLUMN_RULES.get((table.name, column), {}))
            if coordinate == "scholars.paper_count":
                # The public normalized scholar record varies by API version:
                # fixtures may expose paper_ids, while the production envelope
                # currently exposes the authoritative count only inside its
                # replayable /raw mirror.  Describe the source that actually
                # exists in this acquisition instead of claiming an
                # unexecutable length(paper_ids) transform.
                direct_paths = (
                    "/data/list/*/paper_count",
                    "/raw/data/list/*/paper_count",
                )
                direct_path = next(
                    (
                        path
                        for path in direct_paths
                        if int(profiles.get(f"{SCHOLAR}|{path}", {}).get("occurrenceCount", 0)) > 0
                    ),
                    None,
                )
                if direct_path is not None:
                    rule = _rule(
                        SCHOLAR,
                        direct_path,
                        "direct",
                        formula="raw paper_count integer value",
                    )
                elif int(
                    profiles.get(f"{SCHOLAR}|/data/list/*/paper_ids/*", {}).get("occurrenceCount", 0)
                ) > 0:
                    rule = _rule(
                        SCHOLAR,
                        "/data/list/*/paper_ids/*",
                        "derived",
                        formula="length(paper_ids)",
                    )
            if not rule:
                rule = {
                    "endpoint": "",
                    "rawPaths": [],
                    "transform": "omitted",
                    "formula": "",
                    "nullPolicy": "preserve_unknown",
                    "ontologyEligible": False,
                }
            occurrence_count = 0
            for raw_path in rule.get("rawPaths", []):
                occurrence_count += int(profiles.get(f"{rule.get('endpoint')}|{raw_path}", {}).get("occurrenceCount", 0))
            nonblank = sum(bool(str(row.get(column, "")).strip()) for row in table.rows)
            transform = str(rule.get("transform"))
            defaulted = nonblank if transform in {"fallback_alias", "constant_default"} else 0
            mappings[coordinate] = {
                "table": table.name,
                "column": column,
                **rule,
                "rawPresentCount": occurrence_count,
                "outputNonBlankCount": nonblank,
                "defaultedCount": defaulted,
            }
    result = {
        "schemaVersion": LINEAGE_VERSION,
        "sourceFingerprint": source_index.get("sourceFingerprint"),
        "sourceIndexSha256": source_index.get("sourceIndexSha256"),
        "mappings": mappings,
        "metrics": {
            "mappingCount": len(mappings),
            "ontologyEligibleCount": sum(bool(item["ontologyEligible"]) for item in mappings.values()),
            "unsafeMappingCount": sum(item["transform"] in {"fallback_alias", "constant_default"} for item in mappings.values()),
        },
    }
    result["lineageSha256"] = sha256_json(result)
    return result


def source_profile(index: dict[str, Any], *, endpoint: str = "", path_pattern: str = "") -> dict[str, Any]:
    profiles = index.get("pathProfiles", {})
    matches = [
        {"profileKey": key, **value}
        for key, value in profiles.items()
        if (not endpoint or value.get("endpoint") == endpoint)
        and (not path_pattern or value.get("pathPattern") == path_pattern)
    ]
    return {"matchCount": len(matches), "matches": matches}


__all__ = [
    "LINEAGE_VERSION",
    "SOURCE_INDEX_VERSION",
    "build_normalization_lineage",
    "build_source_index",
    "classify_raw_path",
    "json_pointer_get",
    "replay_source_index",
    "source_profile",
]
