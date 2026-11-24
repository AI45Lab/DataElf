from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from dataelf.discovery.contracts import DiscoveryJob, ReviewResult
from dataelf.schemas import new_id


REQUIRED_INSIGHT_FIELDS = [
    "insight_id", "title", "thesis", "why_now", "confidence", "counterarguments",
    "analysis_artifacts", "next_questions",
]


def insight_output_schema(*, new_submission=False):
    properties = {field:{'type':'string','minLength':1} for field in ('insight_id','title','thesis','why_now')}
    properties['confidence'] = {'type':'number','minimum':0,'maximum':1}
    for field in ('counterarguments','analysis_artifacts','next_questions'):
        properties[field] = {'type':'array','minItems':1,'items':{'type':'string','minLength':1}}
    properties['analysis_artifacts']['items'] = {'oneOf':[
        {'type':'string','minLength':1},
        {'type':'object','required':['path'],'properties':{
            'path':{'type':'string','minLength':1},'role':{'type':'string','minLength':1}},
         'additionalProperties':False}]}
    required=list(REQUIRED_INSIGHT_FIELDS)
    if new_submission:
        required += ['supporting_signals','related_entities','external_support']
        properties['supporting_signals']={'type':'array','items':{'type':'string','minLength':1}}
        properties['related_entities']={'type':'array','items':{'oneOf':[{'type':'string','minLength':1},{'type':'object','minProperties':1}]}}
        properties['external_support']={'type':'array','items':{'oneOf':[{'type':'string','minLength':1},{'type':'object','minProperties':1}]}}
        # Empty alternatives/questions are honest; actual supporting artifacts
        # remain mandatory. Meaning and sufficiency are independently reviewed.
        for field in ('counterarguments','next_questions'):
            properties[field].pop('minItems',None)
    items={'type':'array','items':{'type':'object','required':required,'properties':properties}}
    if not new_submission:items['maxItems']=5
    return {'type':'object','required':['insight_candidates'],'properties':{'insight_candidates':items}}


def review_ai_index(job: DiscoveryJob, workspace: Path) -> ReviewResult:
    warnings: list[str] = []
    path = workspace / "insights" / "insight_candidates.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result(job, "failed", [f"Cannot read insight candidates: {exc}"], {})
    insights = payload.get("insight_candidates", []) if isinstance(payload, dict) else []
    if not isinstance(insights, list):
        return _result(job, 'failed', ['AI_INDEX_INSIGHTS_SCHEMA_INVALID: insight_candidates must be an array.'], {})
    invalid = False
    # A diagnostic report is not a successful acquisition. Do not infer this
    # from prose/title keywords, and allow later successful retries to recover.
    acquisition_path = workspace / 'raw/ai_index/acquisition_report.json'
    if acquisition_path.is_file():
        try:
            acquisition = json.loads(acquisition_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return _result(job, 'failed', ['AI_INDEX_ACQUISITION_REPORT_INVALID'], {})
        if not isinstance(acquisition, dict):
            return _result(job, 'failed', ['AI_INDEX_ACQUISITION_REPORT_INVALID'], {})
        if any(key in acquisition and type(acquisition[key]) is not bool
               for key in ('acquisition_succeeded', 'any_data_returned')):
            return _result(job, 'failed', ['AI_INDEX_ACQUISITION_REPORT_INVALID'], {})
        if (acquisition.get('acquisition_succeeded') is False
                or acquisition.get('any_data_returned') is False) and not _has_source_records(workspace):
            invalid = True
            warnings.append('AI_INDEX_ACQUISITION_FAILED: no source records; acquisition diagnostics are not insights.')
    scripts = list((workspace / "scripts").glob("*.py"))
    deep_dives = list((workspace / "deep_dives").glob("*.md"))
    expected_outputs = _expected_outputs(job)
    metrics: dict[str, int] = {
        "insight_count": len(insights),
        "expected_output_count": expected_outputs,
        "script_count": len(scripts),
        "deep_dive_count": len(deep_dives),
    }
    exceeds_requested_count = len(insights) > expected_outputs
    if not 1 <= len(insights) <= 5:
        warnings.append("Expected 1-5 insight candidates.")
    if exceeds_requested_count:
        warnings.append(
            f"Produced {len(insights)} insights, exceeding the requested maximum of {expected_outputs}."
        )
    if not scripts:
        warnings.append("No Python analysis scripts found under scripts/*.py.")
    if not deep_dives:
        warnings.append("No deep-dive reports found under deep_dives/*.md.")
    if not _csv_has_rows(workspace / "tables" / "external_findings.csv"):
        warnings.append("No external findings table found; web search may be unavailable.")
    for index, item in enumerate(insights, start=1):
        if not isinstance(item, dict):
            invalid = True
            warnings.append(f"Insight {index} is not an object.")
            continue
        missing = [field for field in REQUIRED_INSIGHT_FIELDS if item.get(field) in (None, "", [])]
        if missing:
            invalid = True
            warnings.append(f"Insight {index} missing required fields: {', '.join(missing)}.")
        for field in ('insight_id', 'title', 'thesis', 'why_now'):
            if not isinstance(item.get(field), str) or not item[field].strip():
                invalid = True
                warnings.append(f'Insight {index} {field} must be a nonempty string.')
        for field in ('counterarguments', 'analysis_artifacts', 'next_questions'):
            value = item.get(field)
            def valid_entry(x):
                if isinstance(x,str):return bool(x.strip())
                return field == 'analysis_artifacts' and isinstance(x,dict) and set(x) <= {'path','role'} and isinstance(x.get('path'),str) and bool(x['path'].strip()) and ('role' not in x or isinstance(x['role'],str) and bool(x['role'].strip()))
            if not isinstance(value, list) or not all(valid_entry(x) for x in value):
                invalid = True
                allowed = 'nonempty paths or path/role objects' if field == 'analysis_artifacts' else 'nonempty strings'
                warnings.append(f'Insight {index} {field} must be an array of {allowed}.')
        if not item.get("external_support"):
            warnings.append(f"Insight {index} external support is weak or absent.")
        text = " ".join(str(item.get(key, "")) for key in ("title", "thesis", "why_now")).lower()
        if "top-n" in text or "排名" in text:
            warnings.append(f"Insight {index} may be mostly a ranking.")
        confidence = item.get('confidence')
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1 or not math.isfinite(confidence):
            invalid = True
            warnings.append(f"Insight {index} confidence must be a number between 0 and 1.")
    status = "failed" if invalid or not insights or exceeds_requested_count else ("pass_with_warnings" if warnings else "pass")
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


def _has_source_records(workspace: Path) -> bool:
    from .table_builder import _items
    for path in (workspace / 'raw/ai_index').glob('*.json'):
        if path.name == 'acquisition_report.json':
            continue
        try:
            response = json.loads(path.read_text(encoding='utf-8'))
            if (isinstance(response, dict) and response.get('code', 0) in (0, '0')
                    and any(isinstance(row, dict) and row for row in _items(response))):
                return True
        except (OSError, ValueError, TypeError):
            continue
    return False


def _expected_outputs(job: DiscoveryJob) -> int:
    try:
        return max(1, min(int(job.spec.parameters.get("expected_outputs", 3)), 5))
    except (TypeError, ValueError):
        return 3


__all__ = ["REQUIRED_INSIGHT_FIELDS", "review_ai_index"]
