from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import Stage2Config
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.contract import Stage1Contract
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.prompts import reviewer_prompt
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.rdf import MaterializedGraph


ReviewRunner = Callable[[Stage2Config, str, Path, Path], tuple[dict[str, Any], dict[str, Any]]]


def build_review_context(
    *,
    graph: MaterializedGraph,
    validation: dict[str, Any],
    plans: dict[str, dict[str, Any]],
    contract: Stage1Contract,
) -> dict[str, Any]:
    samples: dict[str, list[dict[str, Any]]] = {}
    graph_names = sorted({item.graph for item in graph.quads})
    for graph_name in graph_names:
        samples[graph_name] = [
            {
                "subject": item.subject,
                "predicate": item.predicate,
                "value": item.value,
                "objectKind": item.object_kind,
                "datatype": item.datatype,
            }
            for item in graph.quads
            if item.graph == graph_name
        ][:20]
    summary = {
        "contractFingerprint": contract.contract_fingerprint,
        "stage1RunId": contract.run_id,
        "stage1Draft": contract.is_draft,
        "sourceFingerprint": contract.source_fingerprint,
        "sourceMetrics": contract.source_index["metrics"],
        "candidateStatus": validation["status"],
    }
    reference_operations = [
        {
            "endpoint": endpoint,
            "operationId": operation.get("id"),
            "operation": operation.get("op"),
            "sourcePath": operation.get("sourcePath"),
            "targetClassId": operation.get("targetClassId") or operation.get("memberClassId"),
            "relationKey": operation.get("relationKey"),
        }
        for endpoint, plan in sorted(plans.items())
        for operation in plan["operations"]
        if operation.get("op") in {"reference_by_business_id", "reify_array_membership"}
    ]
    return {
        "summary": summary,
        "identityPolicy": {
            "status": "controller_validated",
            "directBusinessIdPathScope": (
                "An endpoint plan's entity.businessIdPath identifies only records directly observed by that endpoint. "
                "It does not constrain cross-endpoint reference values."
            ),
            "referenceOnlyPolicy": (
                "A nonempty business ID selected by a contract-bound reference operation is a grounded "
                "reference-only entity when its own independently paginated endpoint did not return that ID. "
                "It is not unresolved and does not require a direct observation."
            ),
            "lexicalIdentityPolicy": (
                "Business IDs are opaque verbatim strings. Their spelling must not be used to infer that they are "
                "names, placeholders, or invalid IDs. In particular, canonical paper.author_ids values come verbatim "
                "from the AI Index API authors[*].author_id field; values ending in '#' are valid API author IDs."
            ),
            "sourceEqualityGuarantee": (
                "For every reference-only entity, businessId is exactly the value replayed at its recorded raw JSON "
                "pointer; the controller rejects non-string or empty reference values before materialization."
            ),
            "referenceOperations": reference_operations,
        },
        "plans": {
            endpoint: {
                "planSha256": plan["planSha256"],
                "entity": plan["entity"],
                "expectedSourceCounts": plan["expectedSourceCounts"],
                "operations": plan["operations"],
            }
            for endpoint, plan in sorted(plans.items())
        },
        "metrics": {
            "actual": graph.metrics,
            "expected": validation["expectedMetrics"],
            "sourceContract": validation["sourceContract"],
            "expectedSourceContract": validation["expectedSourceContract"],
            "serialization": validation["serialization"],
            "diagnostics": graph.diagnostics,
            "unresolvedReferences": graph.unresolved_references,
            "referenceOnlyEntities": graph.reference_only_entities,
        },
        "samples": samples,
        "competencyQueries": validation["competencyQueries"],
    }


def validate_review(review: dict[str, Any], config: Stage2Config) -> list[str]:
    errors: list[str] = []
    if review.get("schemaVersion") != "dataelf-stage2-review.v2":
        errors.append("review schemaVersion is invalid")
    if review.get("verdict") not in {"approve", "revise", "unusable"}:
        errors.append("review verdict is invalid")
    issues = review.get("issues")
    if not isinstance(issues, list):
        return errors + ["review issues must be an array"]
    valid_endpoints = {"/openapi/paper/search", "/openapi/scholar/search", "/openapi/institutions/search"}
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            errors.append(f"issues[{index}] must be an object")
            continue
        required = {"severity", "category", "path", "message", "evidenceRefs", "requiredChange", "acceptanceCriteria", "affectedEndpoints"}
        if set(issue) != required:
            errors.append(f"issues[{index}] fields are invalid")
        if issue.get("severity") not in {"critical", "high", "medium", "low"}:
            errors.append(f"issues[{index}] severity is invalid")
        affected = issue.get("affectedEndpoints")
        if not isinstance(affected, list) or any(item not in valid_endpoints for item in affected):
            errors.append(f"issues[{index}] affectedEndpoints are invalid")
    checks = review.get("checks")
    required_checks = {"sourceReplay", "informationCompleteness", "observationSemantics", "identity", "relationAuthority", "serialization", "competencyQueries"}
    if not isinstance(checks, dict) or set(checks) != required_checks:
        errors.append("review checks are incomplete")
    else:
        for check_id, check in checks.items():
            if not isinstance(check, dict) or check.get("status") not in {"pass", "fail"}:
                errors.append(f"review check {check_id} is invalid")
    blocking = [issue for issue in issues if isinstance(issue, dict) and issue.get("severity") in config.quality.blocking_severities]
    failing_checks = [key for key, value in checks.items() if isinstance(value, dict) and value.get("status") == "fail"] if isinstance(checks, dict) else []
    if review.get("verdict") == "approve" and (blocking or failing_checks):
        errors.append("approve verdict contains blocking issues or failing checks")
    if review.get("verdict") == "revise" and not issues:
        errors.append("revise verdict must contain at least one issue")
    return errors


def _default_runner(config: Stage2Config, prompt: str, context_path: Path, runtime_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.model_runtime import run_reviewer

    return run_reviewer(config=config, prompt=prompt, context_path=context_path, runtime_root=runtime_root)


def run_review(
    *,
    config: Stage2Config,
    context: dict[str, Any],
    candidate_root: Path,
    runner: ReviewRunner | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    context_path = candidate_root / "review_context.json"
    atomic_write_json(context_path, context)
    prompt = reviewer_prompt(context["summary"])
    review, runtime = (runner or _default_runner)(config, prompt, context_path, candidate_root / "review_runtime")
    errors = validate_review(review, config)
    if errors:
        raise ValueError("reviewer returned an invalid decision: " + "; ".join(errors))
    return review, runtime


__all__ = ["ReviewRunner", "build_review_context", "run_review", "validate_review"]
