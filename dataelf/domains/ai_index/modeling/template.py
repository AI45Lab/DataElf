from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import (
    atomic_write_json,
    atomic_write_text,
    file_sha256,
    read_json_object,
    sha256_json,
)
from dataelf.domains.ai_index.modeling.ontology_adapter import (
    candidate_from_semantic_plan,
    normalize_candidate_contract,
)
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.checkpoints import configure_artifact_subdir, stage1_root, utc_now
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.raw_source import discover_raw_paths
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.source import prepare_source_cache
from dataelf.domains.ai_index.modeling.stage1_shacl import build_shacl_ttl
from dataelf.domains.ai_index.modeling.stage1_validation import validate_candidate, validate_review


TEMPLATE_SCHEMA_VERSION = "dataelf-ontology-template.v1"
TEMPLATE_BINDING_SCHEMA_VERSION = "dataelf-ontology-template-binding.v1"


class TemplateCompatibilityError(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        messages = [str(item.get("message", "incompatible raw source")) for item in report.get("errors", [])]
        report_path = report.get("reportPath")
        suffix = f"; report={report_path}" if report_path else ""
        super().__init__(
            "fixed ontology template is incompatible with this raw source: "
            + "; ".join(messages[:10])
            + suffix
        )


def templates_root() -> Path:
    return Path(__file__).resolve().parent / "templates"


def available_templates() -> tuple[str, ...]:
    root = templates_root()
    return tuple(sorted(path.parent.name for path in root.glob("*/template.json") if path.is_file()))


def load_template(template_id: str) -> tuple[dict[str, Any], Path]:
    identifier = template_id.strip()
    if not identifier or Path(identifier).name != identifier:
        raise ValueError(f"invalid ontology template id: {template_id!r}")
    path = templates_root() / identifier / "template.json"
    if not path.is_file():
        choices = ", ".join(available_templates()) or "<none>"
        raise ValueError(f"unknown ontology template {identifier!r}; available templates: {choices}")
    template = read_json_object(path)
    if template.get("schemaVersion") != TEMPLATE_SCHEMA_VERSION or template.get("templateId") != identifier:
        raise ValueError(f"invalid ontology template contract: {path}")
    if template.get("approval", {}).get("decision") != "approve":
        raise ValueError(f"ontology template is not approved: {identifier}")
    return template, path


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _type_error(value: Any, expected: str, path: str) -> str | None:
    if expected == "string":
        valid = isinstance(value, str)
    elif expected == "integer":
        valid = _is_integer(value)
    elif expected == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "string_array":
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    elif expected == "hotness":
        valid = isinstance(value, dict) and set(value) <= {
            "day", "week", "month", "half_year", "previous_half_year"
        } and "half_year" in value and all(_is_integer(item) for item in value.values())
    elif expected == "impact":
        valid = isinstance(value, dict) and set(value) == {
            "paper", "talent", "news", "funding", "industry"
        } and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value.values())
    elif expected == "news_array":
        valid = isinstance(value, list) and all(
            isinstance(item, dict)
            and set(item) == {"title", "source", "date"}
            and all(isinstance(item[key], str) for key in ("title", "source", "date"))
            for item in value
        )
    else:
        return f"template declares unsupported field type {expected!r} at {path}"
    return None if valid else f"expected {expected} at {path}, got {type(value).__name__}"


def validate_template_compatibility(
    workspace: Path,
    config: Stage1Config,
    template: dict[str, Any],
    template_path: Path,
) -> dict[str, Any]:
    raw_root = workspace / config.source.raw_subdir
    supported = set(template.get("supportedEndpoints", []))
    compatibility = template.get("compatibility", {})
    record_contracts = compatibility.get("records", {})
    errors: list[dict[str, Any]] = []
    endpoint_counts = {endpoint: {"documentCount": 0, "recordCount": 0} for endpoint in sorted(supported)}

    def error(relative: str, path: str, message: str) -> None:
        errors.append({"relativeFile": relative, "path": path, "message": message})

    raw_paths = discover_raw_paths(raw_root, config)
    for raw_path in raw_paths:
        relative = (Path(config.source.raw_subdir) / raw_path.relative_to(raw_root)).as_posix()
        try:
            document = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            error(relative, "", f"cannot parse JSON: {exc}")
            continue
        if not isinstance(document, dict):
            error(relative, "", "top-level value must be an object")
            continue
        required_top = set(compatibility.get("topLevelRequired", []))
        allowed_top = required_top | set(compatibility.get("topLevelOptional", []))
        missing = sorted(required_top - set(document))
        unknown = sorted(set(document) - allowed_top)
        if missing or unknown:
            error(relative, "", f"top-level keys differ; missing={missing}, unknown={unknown}")
        endpoint = document.get("endpoint")
        if endpoint not in supported:
            error(relative, "/endpoint", f"unsupported endpoint {endpoint!r}")
            continue
        endpoint_counts[str(endpoint)]["documentCount"] += 1
        if document.get("source") != "ai_index":
            error(relative, "/source", "source must be 'ai_index'")
        if document.get("method") != "POST":
            error(relative, "/method", "method must be 'POST'")
        if document.get("mode") not in {"api", "fixture"}:
            error(relative, "/mode", "mode must be 'api' or 'fixture'")
        if not isinstance(document.get("trace_id"), str):
            error(relative, "/trace_id", "trace_id must be a string")
        if not isinstance(document.get("raw"), dict):
            error(relative, "/raw", "raw mirror must be an object")

        request = document.get("request")
        if not isinstance(request, dict):
            error(relative, "/request", "request must be an object")
        else:
            required_request = set(compatibility.get("requestRequired", []))
            allowed_request = required_request | set(compatibility.get("requestOptional", []))
            missing = sorted(required_request - set(request))
            unknown = sorted(set(request) - allowed_request)
            if missing or unknown:
                error(relative, "/request", f"request keys differ; missing={missing}, unknown={unknown}")
            for key in ("page", "size"):
                if key in request and not _is_integer(request[key]):
                    error(relative, f"/request/{key}", f"{key} must be an integer")
            if "sort_type" in request and not isinstance(request["sort_type"], str):
                error(relative, "/request/sort_type", "sort_type must be a string")
            for key in ("domains", "sub_domains"):
                if key in request and not (
                    isinstance(request[key], list) and all(isinstance(item, str) for item in request[key])
                ):
                    error(relative, f"/request/{key}", f"{key} must be a string array")

        data = document.get("data")
        if not isinstance(data, dict) or set(data) != {"list", "total"}:
            error(relative, "/data", "data must contain exactly list and total")
            continue
        items = data.get("list")
        if not isinstance(items, list):
            error(relative, "/data/list", "data.list must be an array")
            continue
        if not _is_integer(data.get("total")):
            error(relative, "/data/total", "data.total must be an integer")
        endpoint_counts[str(endpoint)]["recordCount"] += len(items)
        contract = record_contracts.get(endpoint, {})
        required_fields = set(contract.get("required", []))
        allowed_fields = required_fields | set(contract.get("optional", []))
        field_types = contract.get("fields", {})
        for index, item in enumerate(items):
            pointer = f"/data/list/{index}"
            if not isinstance(item, dict):
                error(relative, pointer, "record must be an object")
                continue
            missing = sorted(required_fields - set(item))
            unknown = sorted(set(item) - allowed_fields)
            if missing or unknown:
                error(relative, pointer, f"record keys differ; missing={missing}, unknown={unknown}")
            for field, value in item.items():
                expected = field_types.get(field)
                if not isinstance(expected, str):
                    continue
                message = _type_error(value, expected, f"{pointer}/{field}")
                if message:
                    error(relative, f"{pointer}/{field}", message)

    for endpoint, counts in endpoint_counts.items():
        if counts["documentCount"] == 0:
            errors.append({"relativeFile": "", "path": "/endpoint", "message": f"missing endpoint {endpoint}"})
    if sum(item["recordCount"] for item in endpoint_counts.values()) == 0:
        errors.append({"relativeFile": "", "path": "/data/list", "message": "all endpoint responses are empty"})
    report = {
        "schemaVersion": "dataelf-template-compatibility.v1",
        "status": "valid" if not errors else "invalid",
        "templateId": template["templateId"],
        "templateSha256": file_sha256(template_path),
        "documentCount": len(raw_paths),
        "endpointCounts": endpoint_counts,
        "errors": errors,
    }
    return report


def _raw_classifications(evidence: dict[str, Any]) -> dict[str, Any]:
    source_ref = str(evidence["sourceIndexEvidenceRef"])
    return {
        key: {
            "endpoint": profile["endpoint"],
            "pathPattern": profile["pathPattern"],
            "classification": profile["classification"],
            "occurrenceCount": profile["occurrenceCount"],
            "evidenceRefs": [source_ref],
        }
        for key, profile in evidence["sourceIndex"]["pathProfiles"].items()
    }


def _template_review(evidence: dict[str, Any]) -> dict[str, Any]:
    source_ref = str(evidence["sourceIndexEvidenceRef"])
    lineage_ref = str(evidence["normalizationLineageEvidenceRef"])
    refs = [source_ref, lineage_ref]
    summaries = {
        "informationCompleteness": "Every normalized field and raw path is classified; unpromoted values remain replayable through SourceFragment.",
        "sourceNavigability": "Domain entities navigate through observations, records, documents, fragments, and exact JSON Pointers.",
        "missingnessSemantics": "Absent and null values remain unknown; the template does not introduce default false or zero values.",
        "associationEndpoints": "Authorship is reified with paper and scholar endpoints and deterministic array-order qualifiers.",
        "observationMetrics": "Mutable counts, hotness, impact, and funding values remain observation-scoped.",
        "multivalueConcepts": "Topics, venues, awards, news, and identifier arrays use structured deterministic mappings.",
        "relationAuthority": "Paper and scholar identifier arrays remain authoritative; institution related lists are non-exhaustive highlights.",
        "competencyQuestionExecutability": "The fixed template preserves the reviewed query paths and explicit omissions for unsupported funding events.",
        "instanceIdentity": "Paper, Scholar, and Institution merge only by their endpoint-scoped business identifiers.",
        "constraintExecutability": "The deterministic SHACL contract is regenerated from the bound ontology and validated offline.",
    }
    return {
        "schemaVersion": "dataelf-ontology-review.v2",
        "verdict": "approve",
        "summary": "Approved fixed-template binding: the reviewed semantic contract was deterministically rebound to this job and passed strict source compatibility and Stage 1 validation.",
        "checkedEvidenceRefs": refs,
        "checks": {
            name: {"status": "pass", "summary": summary, "evidenceRefs": refs}
            for name, summary in summaries.items()
        },
        "issues": [],
    }


def bind_template(config: Stage1Config, workspace: Path, template_id: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    configure_artifact_subdir(config)
    if not workspace.is_dir():
        raise ValueError(f"workspace does not exist: {workspace}")
    if config.source.format != "ai_index_raw":
        raise ValueError("fixed AI Index templates require source.format=ai_index_raw")
    template, template_path = load_template(template_id)
    compatibility = validate_template_compatibility(workspace, config, template, template_path)
    if compatibility["status"] != "valid":
        report_path = stage1_root(workspace, config) / "template_checks" / template_id / "compatibility.json"
        compatibility["reportPath"] = str(report_path)
        atomic_write_json(report_path, compatibility)
        raise TemplateCompatibilityError(compatibility)

    _cache_root, evidence, cache_reused = prepare_source_cache(workspace, config)
    candidate = candidate_from_semantic_plan(
        dict(template["semanticPlan"]),
        config,
        str(evidence["sourceFingerprint"]),
    )
    candidate["grounding"]["rawPathClassifications"] = _raw_classifications(evidence)
    candidate, normalization = normalize_candidate_contract(candidate, evidence, config)
    validation = validate_candidate(candidate["ontology"], candidate["grounding"], evidence, config)
    if validation.get("status") != "valid":
        raise ValueError(
            "fixed ontology template failed deterministic Stage 1 validation: "
            + "; ".join(str(item.get("message", item)) for item in validation.get("errors", [])[:10])
        )
    review = _template_review(evidence)
    review_errors = validate_review(review, evidence, config)
    if review_errors:
        raise ValueError("fixed ontology template review contract is invalid: " + "; ".join(review_errors[:10]))

    template_sha = file_sha256(template_path)
    identity = {
        "template": template_sha,
        "source": evidence["sourceFingerprint"],
        "config": sha256_json({
            "ontologyId": config.ontology.ontology_id,
            "namespace": config.ontology.namespace,
            "competencyQuestions": list(config.ontology.competency_questions),
        }),
        "implementation": file_sha256(Path(__file__).resolve()),
    }
    run_id = f"template_{template_id}_{sha256_json(identity)[:16]}"
    target = stage1_root(workspace, config) / "published" / run_id
    if not target.exists():
        temporary = target.parent / f".{run_id}.{os.getpid()}.tmp"
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            atomic_write_json(temporary / "ontology.json", candidate["ontology"])
            atomic_write_json(temporary / "grounding.json", candidate["grounding"])
            atomic_write_json(temporary / "evidence.json", evidence)
            atomic_write_json(temporary / "source_index.json", evidence["sourceIndex"])
            atomic_write_json(temporary / "normalization_lineage.json", evidence["normalizationLineage"])
            atomic_write_json(temporary / "validation.json", validation)
            atomic_write_json(temporary / "review.json", review)
            atomic_write_json(temporary / "template_compatibility.json", compatibility)
            atomic_write_text(temporary / "shacl.ttl", build_shacl_ttl(candidate["ontology"]))
            artifact_names = (
                "ontology.json",
                "grounding.json",
                "evidence.json",
                "source_index.json",
                "normalization_lineage.json",
                "validation.json",
                "review.json",
                "template_compatibility.json",
                "shacl.ttl",
            )
            manifest = {
                "schemaVersion": "dataelf-ontology-manifest.v2",
                "runId": run_id,
                "createdAt": utc_now(),
                "sourceFingerprint": evidence["sourceFingerprint"],
                "ontologyId": config.ontology.ontology_id,
                "generationMode": "template",
                "templateId": template_id,
                "templateSha256": template_sha,
                "templateBindingSchemaVersion": TEMPLATE_BINDING_SCHEMA_VERSION,
                "artifacts": {
                    name: {
                        "sha256": file_sha256(temporary / name),
                        "sizeBytes": (temporary / name).stat().st_size,
                    }
                    for name in artifact_names
                },
                "compatibility": identity,
                "modelRuntime": {
                    "templateBinding": {
                        "modelCalls": 0,
                        "cacheReused": cache_reused,
                        "contractNormalization": normalization,
                    }
                },
                "manualAuditRequired": False,
                "rawSources": [
                    {
                        key: document.get(key)
                        for key in ("relativeFile", "sha256", "sizeBytes", "endpoint")
                    }
                    for document in evidence["sourceIndex"]["documents"]
                ],
            }
            atomic_write_json(temporary / "manifest.json", manifest)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    latest = {
        "schemaVersion": "dataelf-ontology-latest.v2",
        "runId": run_id,
        "publishedAt": utc_now(),
        "bundle": str(target),
        "ontologySha256": file_sha256(target / "ontology.json"),
        "manifestSha256": file_sha256(target / "manifest.json"),
        "generationMode": "template",
        "templateId": template_id,
    }
    atomic_write_json(stage1_root(workspace, config) / "latest.json", latest)
    return {
        "status": "completed",
        "runId": run_id,
        "bundle": str(target),
        "generationMode": "template",
        "templateId": template_id,
        "templateSha256": template_sha,
        "modelCallCount": 0,
        "cacheReused": cache_reused,
        "compatibility": compatibility,
    }


__all__ = [
    "TemplateCompatibilityError",
    "available_templates",
    "bind_template",
    "load_template",
    "validate_template_compatibility",
]
