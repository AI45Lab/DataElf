"""Failure-report structure and evidence checks, not a causal inference engine."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataelf.discovery.contracts import ReviewResult
from dataelf.schemas import new_id

from .connector import FIELDS, METADATA, RAW, EvidenceError, project, read_json, require
from .review import same_json, summary_from_calls

ANALYSIS = "reports/failure_analysis.json"
TRACE_POINTER = "/calls/1/envelope/result/records/0/chosen_trace"


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceLocation(ReportModel):
    # This identifies a location in this workspace, never a WT step identifier.
    path: Literal["raw/trajectory_analysis/tool_calls.json"] = "raw/trajectory_analysis/tool_calls.json"
    pointer: str = Field(min_length=1, max_length=2048)
    why: str = Field(min_length=1, max_length=800)


class Claim(ReportModel):
    statement: str = Field(min_length=1, max_length=1200)
    basis: Literal["observed", "inferred"]
    evidence: list[EvidenceLocation] = Field(min_length=1, max_length=12)


class OmittedField(ReportModel):
    call_id: str
    record_index: int = Field(ge=0)
    field: str


class AnalysisScope(ReportModel):
    basis: Literal["bounded_tool_responses"] = "bounded_tool_responses"
    call_count: int = Field(ge=1, le=6)
    search_count: int = Field(ge=1, le=1)
    get_count: int = Field(ge=0, le=5)
    truncated: bool
    omitted_fields: list[OmittedField]
    omitted_records: int = Field(ge=0)
    trace_state: Literal["unavailable", "missing", "null", "omitted", "empty", "present"]
    trace_type: str | None
    trace_length: int | None = Field(default=None, ge=0)


class FailureAnalysis(ReportModel):
    schema_version: Literal["2"] = "2"
    result_id: Literal["failure_analysis"] = "failure_analysis"
    status: Literal["located", "insufficient_evidence", "no_records", "read_failed"]
    objective: str = Field(min_length=1)
    scope: AnalysisScope
    # These are evidence-supported interpretations, not a normalized trajectory.
    task_goal: Claim | None
    success_condition: Claim | None
    key_failure: Claim | None
    direct_cause: Claim | None
    outcome: Claim | None
    possible_root_causes: list[Claim] = Field(max_length=5)
    other_explanations: list[Claim] = Field(max_length=5)
    uncertainty: str = Field(min_length=1, max_length=1600)
    limitations: list[str] = Field(min_length=1, max_length=20)


def evidence_scope(calls: list[dict]) -> AnalysisScope:
    """Only summarize acquisition facts; never locate a failure in Python."""
    summary = summary_from_calls(calls)
    projections = [project(call) for call in calls]
    shape = summary["get"]["chosen_trace"] if summary["get"] else None
    return AnalysisScope(
        call_count=len(calls), search_count=sum(c["tool"] == "wt_search_records" for c in calls),
        get_count=sum(c["tool"] == "wt_get_record" for c in calls),
        truncated=any(item["truncated"] for item in projections),
        omitted_fields=[OmittedField(call_id=item["call_id"], **omission)
                        for item in projections for omission in item["omitted_fields"] or []],
        omitted_records=sum(item["omitted_records"] or 0 for item in projections),
        trace_state=shape["state"] if shape else "unavailable",
        trace_type=shape["type"] if shape else None,
        trace_length=shape["length"] if shape else None,
    )


def _resolve_evidence(raw: dict, ref: EvidenceLocation):
    # Only fields actually requested in a successful get are admissible.
    match = re.match(r"^/calls/(0|[1-9][0-9]*)/envelope/result/records/0/([^/]+)(/|$)", ref.pointer)
    require(ref.path == RAW and match is not None, "ANALYSIS_REFERENCE_SCOPE")
    if match is None:
        raise EvidenceError("ANALYSIS_REFERENCE_SCOPE")
    index, field = int(match[1]), match[2]
    require(index < len(raw['calls']) and field in FIELDS, "ANALYSIS_REFERENCE_SCOPE")
    call = raw['calls'][index]
    require(call['tool'] == 'wt_get_record' and field in call['arguments']['fields'],
            "ANALYSIS_REFERENCE_SCOPE")
    shape = project(call).get('fields', {}).get(field)
    require(shape is not None and shape['state'] == 'present', "ANALYSIS_REFERENCE_EMPTY")
    value = raw
    for token in ref.pointer.split("/")[1:]:
        require(not re.search(r"~(?![01])", token), "ANALYSIS_POINTER_INVALID")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            require(token in value, "ANALYSIS_REFERENCE_MISSING")
            value = value[token]
        elif isinstance(value, list):
            require(re.fullmatch(r"0|[1-9][0-9]*", token) is not None,
                    "ANALYSIS_POINTER_INVALID")
            index = int(token)
            require(index < len(value), "ANALYSIS_REFERENCE_MISSING")
            value = value[index]
        else:
            raise EvidenceError("ANALYSIS_REFERENCE_MISSING")
    require(value is not None and value != "" and value != [] and value != {},
            "ANALYSIS_REFERENCE_EMPTY")
    require(bool(ref.why.strip()), "ANALYSIS_SUPPORT_REQUIRED")
    return value


def _check_report(raw: dict, report: FailureAnalysis, objective: str) -> None:
    calls = raw["calls"]
    summary = summary_from_calls(calls)
    scope = evidence_scope(calls)
    require(report.objective == objective, "ANALYSIS_OBJECTIVE_MISMATCH")
    require(same_json(report.scope.model_dump(), scope.model_dump()), "ANALYSIS_SCOPE_MISMATCH")
    require(bool(report.uncertainty.strip()) and all(x.strip() for x in report.limitations),
            "ANALYSIS_LIMITATIONS_REQUIRED")
    limits = {"bounded_record_not_full_session", "granularity_unverified"}
    if scope.truncated:
        limits.add("truncated_output")
    if scope.trace_state != "present":
        limits.add("chosen_trace_" + scope.trace_state)
    for name in ("task_goal", "success_condition", "outcome"):
        if getattr(report, name) is None:
            limits.add(name + "_unknown")
    require(limits <= set(report.limitations), "ANALYSIS_LIMITATIONS_MISMATCH")

    if summary["status"] == "error":
        require(report.status == "read_failed", "ANALYSIS_STATUS_MISMATCH")
    elif summary["status"] == "empty":
        require(report.status == "no_records", "ANALYSIS_STATUS_MISMATCH")
    elif any(project(call)["count"] == 0 for call in calls[1:]):
        # An empty get ends acquisition even when the initial trace was usable.
        # A present record with a missing/null/empty/omitted field is distinct.
        require(report.status == "insufficient_evidence", "ANALYSIS_STATUS_MISMATCH")
    else:
        require(report.status in {"located", "insufficient_evidence"}, "ANALYSIS_STATUS_MISMATCH")

    primary = [report.task_goal, report.success_condition, report.key_failure,
               report.direct_cause, report.outcome]
    for claim in [c for c in primary if c is not None] + report.possible_root_causes + report.other_explanations:
        require(bool(claim.statement.strip()), "ANALYSIS_SUPPORT_REQUIRED")
        for ref in claim.evidence:
            _resolve_evidence(raw, ref)
    for claim in report.possible_root_causes + report.other_explanations:
        require(claim.basis == "inferred", "ANALYSIS_INFERENCE_REQUIRED")
    for observation in (report.task_goal, report.success_condition, report.outcome):
        if observation is not None:
            require(observation.basis == "observed", "ANALYSIS_OBSERVATION_REQUIRED")

    if report.status == "located":
        require(not scope.truncated and scope.trace_state == "present"
                and scope.trace_type in {"array", "object", "string"}
                and scope.trace_length is not None and scope.trace_length > 0,
                "ANALYSIS_INSUFFICIENT_TRACE")
        require(all(claim is not None for claim in primary[:4]), "ANALYSIS_SUPPORT_REQUIRED")
        if report.key_failure is None:
            raise EvidenceError("ANALYSIS_SUPPORT_REQUIRED")
        require(any(len(ref.pointer.split("/")) > 8
                    for ref in report.key_failure.evidence), "ANALYSIS_LOCATION_REQUIRED")
    else:
        # Partial observations are welcome; unsupported causal localization is not.
        require(report.key_failure is None and report.direct_cause is None
                and not report.possible_root_causes, "ANALYSIS_UNSUPPORTED_LOCALIZATION")


def review_analysis(job, workspace: Path) -> ReviewResult:
    status: Literal["pass", "pass_with_warnings", "failed", "skipped"]
    metrics: dict[str, str | int | float | bool] = {"evidence_verified": False, "references_verified": False,
               "localization_reported": False, "causal_correctness_verified": False}
    try:
        raw = read_json(workspace, RAW)
        require(isinstance(raw, dict) and set(raw) == {"calls"}, "QUERY_RAW_INVALID")
        summary_from_calls(raw["calls"])
        require(same_json(read_json(workspace, METADATA),
                          {"calls": [project(call) for call in raw["calls"]]}),
                "QUERY_METADATA_MISMATCH")
        metrics["evidence_verified"] = True
        report = FailureAnalysis.model_validate(read_json(workspace, ANALYSIS))
        _check_report(raw, report, job.spec.objective)
        metrics["references_verified"] = True
        metrics["localization_reported"] = report.status == "located"
        if report.status == "read_failed":
            status, warnings = "failed", ["ANALYSIS_READ_FAILED"]
        elif report.status == "located":
            status, warnings = "pass", []
        else:
            status, warnings = "pass_with_warnings", ["ANALYSIS_" + report.status.upper()]
    except EvidenceError as exc:
        status, warnings = "failed", [str(exc)]
    except ValidationError:
        # Never emit Pydantic's input values or arbitrary report text to logs.
        status, warnings = "failed", ["ANALYSIS_SCHEMA_INVALID"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        status, warnings = "failed", ["ANALYSIS_EVIDENCE_INVALID"]
    return ReviewResult(review_id=new_id("review"), job_id=job.job_id, status=status,
                        warnings=warnings, metrics=metrics, recommended_revision=status != "pass")
