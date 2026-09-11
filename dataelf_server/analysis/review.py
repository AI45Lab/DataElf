from __future__ import annotations

import csv
import json
from pathlib import Path

from dataelf.discovery.contracts import ReviewResult
from dataelf.schemas import new_id
from dataelf_server.analysis.rdf_analysis import validate_analysis_manifest
from dataelf_server.analysis.insight_contract import load_contract
from dataelf_server.analysis.writing import write_writing_review


REQUIRED_INSIGHT_FIELDS = [
    "insight_id",
    "title",
    "thesis",
    "why_now",
    "confidence",
    "counterarguments",
    "analysis_artifacts",
    "next_questions",
]


def review_workspace(job_id: str, workspace_path: Path, *, scope: str) -> ReviewResult:
    path = workspace_path / "insights" / "insight_candidates.json"
    candidate_path = workspace_path / "insights" / "candidate_signals.json"
    warnings: list[str] = []
    analysis_expected = scope == "scope_v2"
    analysis_issues = (
        validate_analysis_manifest(workspace_path) if analysis_expected else []
    )
    warnings.extend(analysis_issues)
    payload: dict = {"insight_count": 0, "script_count": 0, "deep_dive_count": 0}
    if not candidate_path.exists():
        warnings.append("candidate_signals.json is missing.")
    if not path.exists():
        warnings.append("insight_candidates.json is missing.")
        return _result(job_id, "failed", warnings, payload)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warnings.append(f"insight_candidates.json is not valid JSON: {exc}")
        return _result(job_id, "failed", warnings, payload)

    insights = data.get("insight_candidates", [])
    writing = write_writing_review(workspace_path, insights, load_contract(workspace_path) or {})
    if writing:
        warnings.extend(writing["warnings"])
        analysis_issues.extend(writing["errors"])
        warnings.extend(writing["errors"])
    payload["insight_count"] = len(insights)
    scripts = list((workspace_path / "scripts").glob("*.py"))
    deep_dives = list((workspace_path / "deep_dives").glob("*.md"))
    payload["script_count"] = len(scripts)
    payload["deep_dive_count"] = len(deep_dives)
    if not scripts:
        warnings.append("No Python analysis scripts found under scripts/*.py.")
    if not deep_dives:
        warnings.append("No deep-dive reports found under deep_dives/*.md.")
    if not _csv_has_rows(workspace_path / "tables" / "external_findings.csv"):
        warnings.append("No external findings table found; web search may be unavailable.")
    if not insights:
        warnings.append("Expected at least one insight candidate.")
    if insights and not any(item.get("analysis_artifacts") for item in insights if isinstance(item, dict)):
        warnings.append("No insight includes analysis_artifacts.")
    for idx, item in enumerate(insights, start=1):
        missing = [field for field in REQUIRED_INSIGHT_FIELDS if field not in item or item.get(field) in (None, "", [])]
        if missing:
            warnings.append(f"Insight {idx} missing required fields: {', '.join(missing)}.")
        if not item.get("external_support"):
            warnings.append(f"Insight {idx} external support is weak or absent.")
        combined_text = " ".join([str(item.get("title", "")), str(item.get("thesis", "")), str(item.get("why_now", ""))]).lower()
        if "top" in combined_text or "排名" in combined_text or "top-n" in combined_text:
            warnings.append(f"Insight {idx} may be mostly a top-N ranking; deepen the mechanism or anomaly.")
        confidence = item.get("confidence", 0)
        try:
            if not 0 <= float(confidence) <= 1:
                warnings.append(f"Insight {idx} confidence should be between 0 and 1.")
        except (TypeError, ValueError):
            warnings.append(f"Insight {idx} confidence should be numeric.")

    status = "pass" if not warnings else "pass_with_warnings"
    if not insights or analysis_issues:
        status = "failed"
    return _result(job_id, status, warnings, payload)


def _result(job_id: str, status: str, warnings: list[str], payload: dict) -> ReviewResult:
    return ReviewResult(
        review_id=new_id("review"),
        job_id=job_id,
        status=status,
        warnings=warnings,
        recommended_revision=bool(warnings),
        metrics=payload,
    )


def _csv_has_rows(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8", newline="") as handle:
        return any(True for _ in csv.DictReader(handle))
