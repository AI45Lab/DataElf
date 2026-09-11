from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any
from dataelf_server.presentation.schemas import Insight
from dataelf_server.analysis.insight_contract import validate_workspace_insights
from dataelf_server.analysis.rdf_analysis import validate_analysis_manifest, load_candidate_signals
from dataelf.discovery.artifacts import resolve_workspace_path

class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

def _load_and_validate_insights(workspace_path: Path) -> list[dict[str, Any]]:
    path = workspace_path / "insights" / "insight_candidates.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError("invalid_insights", "insight_candidates.json is missing") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError("invalid_insights", "insight_candidates.json is invalid JSON") from exc

    values = document.get("insight_candidates") if isinstance(document, dict) else None
    if not isinstance(values, list) or not values:
        raise PipelineError(
            "invalid_insight_count", "Pi must return at least one insight candidate."
        )

    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, value in enumerate(values, start=1):
        try:
            insight = Insight.model_validate(value)
        except Exception as exc:
            raise PipelineError(
                "invalid_insights",
                f"Insight {index} does not match the required schema: {exc}",
            ) from exc
        if not all((insight.title.strip(), insight.thesis.strip(), insight.why_now.strip())):
            raise PipelineError("invalid_insights", f"Insight {index} contains empty text fields.")
        if not re.fullmatch(r"ins_\d{3,}", insight.insight_id) or insight.insight_id in seen_ids:
            raise PipelineError(
                "invalid_insights",
                f"Insight {index} must have a unique ID in ins_<digits> format.",
            )
        seen_ids.add(insight.insight_id)
        if not insight.analysis_artifacts or not insight.counterarguments or not insight.next_questions:
            raise PipelineError(
                "invalid_insights",
                f"Insight {index} must include artifacts, counterarguments, and next questions.",
            )
        validated.append(insight.model_dump(mode="json"))
    return validated



def workspace_insights(workspace: Path, *, scope: str) -> list[dict]:
    insights = _load_and_validate_insights(workspace)
    issues = validate_workspace_insights(workspace)
    if scope == "scope_v2":
        issues.extend(validate_analysis_manifest(workspace))
        signals = {item["signal_id"]: item for item in load_candidate_signals(workspace)}
        for insight in insights:
            selected = insight.get("supporting_signals") or []
            if not selected or any(value not in signals for value in selected):
                issues.append(f"Insight {insight['insight_id']} references unknown or missing candidate signals.")
                continue
            allowed = {source for signal in selected for source in signals[signal].get("source_ids", [])}
            cited = {str(ref.get("source_id") or "") for ref in insight.get("external_support", [])}
            if not cited or not cited.issubset(allowed):
                issues.append(f"Insight {insight['insight_id']} cites sources unsupported by its signals.")
            for artifact in insight["analysis_artifacts"]:
                if not resolve_workspace_path(workspace, artifact).is_file():
                    issues.append(f"Insight {insight['insight_id']} references a missing analysis artifact.")
    if issues:
        raise PipelineError("insight_format_invalid", "; ".join(issues))
    return insights
