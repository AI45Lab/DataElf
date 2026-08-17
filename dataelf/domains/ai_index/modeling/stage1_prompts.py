from __future__ import annotations

import json
from typing import Any

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import sha256_json
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config


PROMPT_VERSION = "dataelf-ontology-stage1-prompts/8"


def prompt_fingerprint() -> str:
    return sha256_json({"version": PROMPT_VERSION, "generator": GENERATOR_SYSTEM, "reviewer": REVIEWER_SYSTEM})


GENERATOR_SYSTEM = """You are DataElf's Stage 1 ontology induction agent. Build a comprehensive three-layer ontology directly grounded in raw AI Index JSON. The normalized CSV tables are evidence views only; raw JSON is authoritative.

Layers and required classes:
1. Domain: DomainEntity with Paper, Scholar, Institution, plus Topic, Venue, Award, NewsItem and reified Authorship.
2. Observation: EntityObservation with PaperObservation, ScholarObservation and InstitutionObservation. Each API occurrence remains an observation even when records are byte-identical.
3. Source: SourceDocument, SearchResponse, SourceRecord and SourceFragment.

Required navigation shapes are SearchResponse→SourceDocument, SearchResponse→SourceRecord, SearchResponse→SourceFragment, SourceDocument→SourceFragment, SourceRecord→SourceDocument, SourceFragment→SourceRecord, SourceFragment→SourceDocument, EntityObservation→SourceRecord and EntityObservation→DomainEntity. Empty responses must reach request/envelope fragments without traversing SourceRecord. Record-nested fragments also use SourceRecord. Define sourceSystem, sourcePath, jsonPointer, sourceSha256, recordHash, fragmentValueKind, fragmentValueHash, resultRank and endpoint access properties on appropriate classes. Every class and property needs both semantic evidence and a complete navigation chain to SourceRecord/SourceDocument/raw Pointer.

Semantic rules:
- Merge Paper/Scholar/Institution only by business ID with require_consensus. Preserve stable values on typed observations in all cases; publish a canonical entity value only on consensus. Conflicts stay observation-only; never choose a canonical value without consensus.
- Request topic, sort type, page, size and result count belong to SearchResponse. Rank and mutable citation/count/funding/impact/day/week/month/half-year/previous-half-year values belong to observations, never merged entities. Property names must retain the real time window.
- Promote fields, venues and awards as shared Topic/Venue/Award resources, not opaque JSON strings. Preserve every array relationship as an observation-scoped snapshot before projecting authority-aware domain shortcuts. Institution region, impact, news, funding total and every hotness window must be domain/observation accessible.
- Reify Authorship with exactly two endpoints and authorOrder=`array_index + 1`, isFirstAuthor=`array_index == 0`; authoredBy may be a shortcut. Never assert isCorrespondingAuthor.
- Paper–Institution and Scholar–Institution use direct relationships without fake qualifier classes. Never infer missing isPrimary=false.
- Keep citationCount and institutionScholarCount only. Never add citedByCount or institutionAuthorCount aliases.
- Treat institution related_* fields as non-exhaustive highlight relations. Use controller relationAuthority comparisons exactly.
- FundingEvent/yearly metric hints with no raw instance support must be explicitly omitted, not inferred from empty normalized schemas.

Source completeness rules:
- The controller pre-seeds exhaustive rawPathClassifications, sourceCoverage and normalizationEvidenceRefs. Preserve them exactly; never stage replacements for those sections.
- Every promoted business raw path supplied in the packet must appear in at least one sourceBindings entry using exact `endpoint|pathPattern` text.
- sourceBindings keys are exactly every ontology class/property ID. Each value contains semanticEvidenceRefs, sourceEvidenceRefs (including source-index ref), rawPathPatterns, and navigation (ordered class/property steps).
- sourceAccessPaths has every ontology class ID and a non-empty steps array that reaches EntityObservation/SourceRecord/SourceDocument/raw pointer as appropriate.
- raw paths have only semantic_promoted, observation_promoted, derived_with_formula, redundant_but_source_linked, or source_only classifications. Never use ignored for raw JSON.
- Unknown must remain unknown. Never promote fallback_alias or constant_default normalized columns. Derived values require the exact formula. A non-empty business array may not become xsd:string JSON.

Grounding v2:
- Keep exact tableClassifications and `table.column` columnClassifications. Ignored normalized evidence-view columns need a reason and empty propertyIds.
- classEvidence/objectPropertyEvidence/datatypePropertyEvidence values are always arrays. Raw entries use sourceKind raw_json, exact endpoint/pathPattern and evidenceRefs. Normalized entries use exact table/column or join profile coordinates.
- entityObservationMappings retains normalized row/source_raw evidence for Paper, Scholar and Institution.
- entityResolutionMappings keys Paper/Scholar/Institution and copies exact businessEntityCount, observationCount, mergePolicy, conflictingEntityCount and per-field fieldConsensus; conflictPolicy is observation_only. Mutable metrics never participate in stable-field consensus.
- responseObservationMappings keys PaperObservation/ScholarObservation/InstitutionObservation with responseClassId SearchResponse, sourceRecordClassId SourceRecord, and a declared resultRankPropertyId.
- resultRank is one-based (`data.list array_index + 1`) and must use xsd:positiveInteger; SourceRecord and SourceFragment each need an exact JSON Pointer access property.
- associationMappings.Authorship contains endpointPropertyIds and qualifier objects with propertyId/formula.
- relationAuthority copies authority, corroboration, differenceStrategy from controller evidence and marks institution-side corroborationSemantics non_exhaustive_highlight.
- domainHintResolutions keys exactly all entity:/relation: hints, each implemented or omitted with evidence.
- competencyQuestions copy configured text exactly; cqCoverage must be executable and source-grounded.
- iriGenerationMappings defines deterministic identities for every class, including source SHA + JSON Pointer observations/fragments, business-ID entities, source-specific Authorship, normalized concept keys and source-pointer NewsItem identity.
- shaclContract requires executable cardinality/key/datatype/inverse/subproperty and authoredBy/Authorship consistency constraints; publication deterministically materializes shacl.ttl.

Ontology output schema is dataelf-ontology.v2; grounding is dataelf-grounding.v2. IDs are UpperCamelCase classes and lowerCamelCase properties; URIs use only the configured namespace. Allowed kinds: entity, observation, event, association, metric, provenance, concept. Allowed datatypes: xsd:string, xsd:boolean, xsd:integer, xsd:nonNegativeInteger, xsd:decimal, xsd:date, xsd:dateTime, xsd:anyURI. Include no SEC/10-K/XBRL concepts.

Work method:
1. Inspect ontology_source_overview, raw endpoints/path profiles, lineage and relation comparisons; inspect normalized tables/identity/join evidence as needed.
2. Stage every required non-controller section. Chunk maps over 40 keys using merge and mark only the final chunk complete. Stage arrays through ontology_stage_array.
3. Submit with ontology_submit_candidate only. On repair, patch only cited sections; durable correct sections are already present."""


REVIEWER_SYSTEM = """You are DataElf's independent fresh-context Stage 1 reviewer. You did not participate in generation. Raw AI Index JSON is authoritative; normalized tables are only evidence views.

Use the raw endpoint, raw path, column lineage, relation comparison and replay tools. Mandatory checks are informationCompleteness, sourceNavigability, missingnessSemantics, associationEndpoints, observationMetrics, multivalueConcepts, relationAuthority, competencyQuestionExecutability, instanceIdentity and constraintExecutability. Return every check as pass/fail with a substantive summary and evidence refs.

Specifically reject: missing promoted raw fields (including envelope fields such as /source); empty SearchResponse fragment paths that require a nonexistent record; any unclassified/unreplayable Pointer; unknown converted to false/0; fallback/default aliases promoted; mutable metrics on merged entities; stable values or relationship arrays lacking observation snapshots; duplicate semantic aliases for one raw value; opaque JSON business arrays; Authorship without both endpoints/order formula; isCorrespondingAuthor/isPrimary defaults; related_* treated as exhaustive affiliation; missing observation rank/request context; nondeterministic or collision-prone instance IRIs; missing executable SHACL constraints; unsupported FundingEvent facts; or any ontology element without a Domain→Observation→SourceRecord→SourceDocument/raw navigation.

Promotion boundary: require promotion only for controller paths classified semantic_promoted or observation_promoted and normalized columns with ontologyEligible=true. A /raw mirror classified redundant_but_source_linked or source_only remains complete when it is replayable through SourceFragment; do not demand a new ontology property for it. In particular, isCorrespondingAuthor must remain unasserted because the executable Authorship contract is indexed by canonical author_ids and cannot safely join the separate /raw authors array. Empty normalized helper columns with ontologyEligible=false (including paper_awards conf/year) must not be promoted; verify their raw context is source-navigable instead. Reject isCorrespondingAuthor/isPrimary only when a candidate actually asserts or defaults them, not when they are intentionally absent.

approve requires deterministic validation valid, all eight checks pass, and no critical/high/medium issue. revise is for targeted repair; unusable is only for fundamentally corrupt candidates. Submit exactly one dataelf-ontology-review.v2 result through ontology_submit_review. Every issue includes severity, category, JSON path, evidenceRefs, requiredChange and testable acceptanceCriteria."""


def generator_prompt(
    config: Stage1Config,
    evidence: dict[str, Any],
    feedback: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
) -> str:
    catalog = evidence["catalog"]
    domain = evidence["evidence"][evidence["domainHintsEvidenceRef"]]["result"]
    source_index = evidence.get("sourceIndex", {})
    promoted_raw_paths = [
        key
        for key, item in source_index.get("pathProfiles", {}).items()
        if isinstance(item, dict)
        and item.get("classification") in {"semantic_promoted", "observation_promoted"}
    ]
    packet: dict[str, Any] = {
        "task": "Generate or repair the DataElf Ontology Stage 1 candidate.",
        "ontologyId": config.ontology.ontology_id,
        "namespace": config.ontology.namespace,
        "title": config.ontology.title,
        "sourceFingerprint": evidence["sourceFingerprint"],
        "catalogEvidenceRef": evidence["catalogEvidenceRef"],
        "domainHintsEvidenceRef": evidence["domainHintsEvidenceRef"],
        "sourceIndexEvidenceRef": evidence.get("sourceIndexEvidenceRef"),
        "normalizationLineageEvidenceRef": evidence.get("normalizationLineageEvidenceRef"),
        "catalog": catalog,
        "sourceCounts": {
            "tableCount": catalog["tableCount"],
            "nonEmptyTableCount": catalog["nonEmptyTableCount"],
            "columnCount": sum(len(item["columns"]) for item in catalog["tables"]),
            "totalRowCount": catalog["totalRowCount"],
        },
        "domainHints": domain,
        "rawSourceMetrics": source_index.get("metrics", {}),
        "entityProfiles": source_index.get("entityProfiles", {}),
        "relationComparisons": source_index.get("relationComparisons", {}),
        "promotedRawPathsThatMustBeBound": promoted_raw_paths,
        "controllerSeededSections": ["rawPathClassifications", "sourceCoverage", "normalizationEvidenceRefs"],
        "competencyQuestions": list(config.ontology.competency_questions),
        "requiredSections": [
            "metadata", "classes", "objectProperties", "datatypeProperties",
            "tableClassifications", "columnClassifications", "classEvidence",
            "objectPropertyEvidence", "datatypePropertyEvidence", "entityObservationMappings",
            "accessPaths", "domainHintResolutions", "competencyQuestions", "cqCoverage", "sourceCoverage",
            "sourceBindings", "sourceAccessPaths", "rawPathClassifications", "associationMappings",
            "entityResolutionMappings", "responseObservationMappings", "relationAuthority",
            "observationValueMappings", "relationSnapshotMappings", "iriGenerationMappings",
            "shaclContract", "normalizationEvidenceRefs"
        ],
    }
    if feedback:
        packet["repairFeedback"] = feedback
    if baseline:
        packet["repairBaseline"] = {
            "alreadySeededInDurableState": True,
            "instruction": "Patch only cited defects with merge mode, preserve all other staged content, then submit.",
        }
    return "Controller packet:\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))


def semantic_plan_prompt(
    config: Stage1Config,
    evidence: dict[str, Any],
    feedback: dict[str, Any] | None,
) -> str:
    """Build the bounded prompt used by compact non-streaming model transports."""

    source_index = evidence.get("sourceIndex", {})
    documents = source_index.get("documents", [])
    requests = [
        {
            "endpoint": item.get("endpoint"),
            "request": item.get("request"),
            "resultCount": item.get("resultCount"),
        }
        for item in documents
        if isinstance(item, dict)
    ]
    packet: dict[str, Any] = {
        "task": "Describe the semantic core of the raw-grounded three-layer AI Index ontology.",
        "ontologyId": config.ontology.ontology_id,
        "title": config.ontology.title,
        "sourceFingerprint": evidence["sourceFingerprint"],
        "sourceMetrics": source_index.get("metrics", {}),
        "entityProfiles": source_index.get("entityProfiles", {}),
        "relationComparisons": source_index.get("relationComparisons", {}),
        "requests": requests,
        "domainHints": evidence["evidence"][evidence["domainHintsEvidenceRef"]]["result"],
        "competencyQuestions": list(config.ontology.competency_questions),
        "controllerContract": {
            "fundingEventPolicy": "omit_without_raw_instances",
            "stableValuePolicy": "require_consensus",
            "mutableMetricLayer": "observation",
            "relationshipArrayPolicy": "observation_snapshot_before_projection",
            "note": "The controller materializes exhaustive grounding, provenance, IRI and SHACL sections after this semantic plan.",
        },
    }
    if feedback:
        packet["repairFeedback"] = feedback
    return "Compact semantic planning packet:\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))


def reviewer_prompt(
    config: Stage1Config,
    evidence: dict[str, Any],
    candidate: dict[str, Any],
    validation: dict[str, Any],
) -> str:
    from dataelf.domains.ai_index.modeling.stage1_shacl import build_shacl_ttl

    grounding = candidate["grounding"]
    omitted = {
        name: {
            "sha256": sha256_json(grounding.get(name)),
            "entryCount": len(grounding.get(name, {})) if isinstance(grounding.get(name), dict) else None,
            "controllerValidation": "exhaustively checked; use raw/lineage tools for spot checks",
        }
        for name in ("columnClassifications", "rawPathClassifications")
    }
    semantic_grounding = {
        key: value
        for key, value in grounding.items()
        if key not in {"columnClassifications", "rawPathClassifications"}
    }
    packet = {
        "task": "Independently audit this Stage 1 ontology candidate.",
        "configuredCompetencyQuestions": list(config.ontology.competency_questions),
        "catalog": evidence["catalog"],
        "sourceIndexEvidenceRef": evidence.get("sourceIndexEvidenceRef"),
        "normalizationLineageEvidenceRef": evidence.get("normalizationLineageEvidenceRef"),
        "rawSourceMetrics": evidence.get("sourceIndex", {}).get("metrics", {}),
        "entityProfiles": evidence.get("sourceIndex", {}).get("entityProfiles", {}),
        "relationComparisons": evidence.get("sourceIndex", {}).get("relationComparisons", {}),
        "domainHints": evidence["evidence"][evidence["domainHintsEvidenceRef"]]["result"],
        "ontology": candidate["ontology"],
        "grounding": semantic_grounding,
        "deterministicShaclTtl": build_shacl_ttl(candidate["ontology"]),
        "omittedControllerExhaustiveSections": omitted,
        "deterministicValidation": validation,
    }
    return "Fresh review packet:\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))


def compact_reviewer_prompt(
    config: Stage1Config,
    evidence: dict[str, Any],
    candidate: dict[str, Any],
    validation: dict[str, Any],
) -> str:
    """Bounded independent-review packet for non-streaming model transports."""

    grounding = candidate["grounding"]
    grounding_manifest = {
        key: {
            "sha256": sha256_json(value),
            "entryCount": len(value) if isinstance(value, (dict, list)) else None,
        }
        for key, value in grounding.items()
    }
    packet = {
        "task": "Independently review the semantic ontology and the deterministic Stage 1 validation result.",
        "configuredCompetencyQuestions": list(config.ontology.competency_questions),
        "sourceIndexEvidenceRef": evidence.get("sourceIndexEvidenceRef"),
        "normalizationLineageEvidenceRef": evidence.get("normalizationLineageEvidenceRef"),
        "rawSourceMetrics": evidence.get("sourceIndex", {}).get("metrics", {}),
        "entityProfiles": evidence.get("sourceIndex", {}).get("entityProfiles", {}),
        "relationComparisons": evidence.get("sourceIndex", {}).get("relationComparisons", {}),
        "ontology": candidate["ontology"],
        "groundingManifest": grounding_manifest,
        "deterministicValidation": validation,
        "reviewInstruction": (
            "Approve only when deterministicValidation is valid and all ten mandatory checks pass. "
            "Use sourceIndexEvidenceRef in every check. Submit exactly one ontology_submit_review call."
        ),
    }
    return "Compact fresh review packet:\n" + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))


def repair_feedback(validation: dict[str, Any] | None, review: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {"instruction": "Repair only the cited defects; preserve correct staged content."}
    if validation and validation.get("status") != "valid":
        grouped: dict[str, dict[str, Any]] = {}
        for issue in validation.get("errors", []):
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("code", "unknown"))
            bucket = grouped.setdefault(code, {"code": code, "count": 0, "samples": []})
            bucket["count"] += 1
            if len(bucket["samples"]) < 5:
                bucket["samples"].append(issue)
        result["deterministicIssueGroups"] = list(grouped.values())
        result["deterministicErrorCount"] = len(validation.get("errors", []))
    # An approved review may still carry non-blocking precision findings.  A
    # later --repair-from run must be able to improve those findings instead of
    # silently discarding them merely because publication was technically
    # allowed.
    if review and review.get("issues"):
        result["reviewVerdict"] = review.get("verdict")
        result["reviewIssues"] = [
            {
                "path": item.get("path"),
                "requiredChange": item.get("requiredChange"),
                "acceptanceCriteria": item.get("acceptanceCriteria"),
                "evidenceRefs": item.get("evidenceRefs", []),
                "severity": item.get("severity"),
            }
            for item in review.get("issues", [])
            if isinstance(item, dict)
        ]
    return result
