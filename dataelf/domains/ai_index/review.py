from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from dataelf.discovery.contracts import DiscoveryJob, ReviewResult
from dataelf.schemas import new_id


REQUIRED_INSIGHT_FIELDS = [
    "insight_id", "title", "thesis", "why_now", "confidence", "counterarguments",
    "analysis_artifacts", "next_questions",
]


def review_ai_index(job: DiscoveryJob, workspace: Path) -> ReviewResult:
    warnings: list[str] = []
    path = workspace / "insights" / "insight_candidates.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result(job, "failed", [f"Cannot read insight candidates: {exc}"], {})
    insights = payload.get("insight_candidates", []) if isinstance(payload, dict) else []
    scripts = list((workspace / "scripts").glob("*.py"))
    deep_dives = list((workspace / "deep_dives").glob("*.md"))
    metrics: dict[str, int] = {"insight_count": len(insights), "script_count": len(scripts), "deep_dive_count": len(deep_dives)}
    if not 1 <= len(insights) <= 5:
        warnings.append("Expected 1-5 insight candidates.")
    if not scripts:
        warnings.append("No Python analysis scripts found under scripts/*.py.")
    if not deep_dives:
        warnings.append("No deep-dive reports found under deep_dives/*.md.")
    if not _csv_has_rows(workspace / "tables" / "external_findings.csv"):
        warnings.append("No external findings table found; web search may be unavailable.")
    for index, item in enumerate(insights, start=1):
        if not isinstance(item, dict):
            warnings.append(f"Insight {index} is not an object.")
            continue
        missing = [field for field in REQUIRED_INSIGHT_FIELDS if item.get(field) in (None, "", [])]
        if missing:
            warnings.append(f"Insight {index} missing required fields: {', '.join(missing)}.")
        if not item.get("external_support"):
            warnings.append(f"Insight {index} external support is weak or absent.")
        text = " ".join(str(item.get(key, "")) for key in ("title", "thesis", "why_now")).lower()
        if "top-n" in text or "排名" in text:
            warnings.append(f"Insight {index} may be mostly a ranking.")
        try:
            if not 0 <= float(item.get("confidence")) <= 1:
                warnings.append(f"Insight {index} confidence should be between 0 and 1.")
        except (TypeError, ValueError):
            warnings.append(f"Insight {index} confidence should be numeric.")
    status = "failed" if not insights else ("pass_with_warnings" if warnings else "pass")
    return _result(job, status, warnings, metrics)


def _result(job: DiscoveryJob, status: str, warnings: list[str], metrics: dict[str, Any]) -> ReviewResult:
    return ReviewResult(
        review_id=new_id("review"), job_id=job.job_id, status=status,
        warnings=warnings, recommended_revision=bool(warnings), metrics=metrics,
    )


def _csv_has_rows(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("r", encoding="utf-8", newline="") as handle:
        return any(True for _ in csv.DictReader(handle))


__all__ = ["REQUIRED_INSIGHT_FIELDS", "review_ai_index"]
