"""Domain review uses declared workspace evidence, never Pi message formats."""
import json
from pathlib import Path
from typing import Literal

from dataelf.discovery.contracts import ReviewResult
from dataelf.schemas import new_id

from .connector import (
    METADATA,
    RAW,
    SUMMARY,
    EvidenceError,
    project,
    read_json,
    require,
    validate_sequence,
)


def summary_from_calls(calls):
    validate_sequence(calls, complete=True)
    search = project(calls[0])
    gets = [project(call) for call in calls[1:]]
    get = gets[0] if gets else None  # Never replace the initial chosen_trace projection.
    status = 'complete'
    if any(not item['success'] for item in [search, *gets]):
        status = 'error'
    elif any(item['truncated'] for item in [search, *gets]) or any(item['count'] == 0 for item in gets):
        status = 'incomplete'
    elif search['count'] == 0:
        status = 'empty'
    result = dict(result_id='query_summary', status=status, search=search, get=get)
    if len(gets) > 1:
        result['supplemental_gets'] = gets[1:]
    return result


def same_json(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def review_query(job, workspace: Path):
    status: Literal["pass", "pass_with_warnings", "failed", "skipped"]
    metrics: dict[str, str | int | float | bool] = {'evidence_verified': False, 'metadata_matches': False, 'summary_matches': False}
    try:
        raw = read_json(workspace, RAW)
        require(isinstance(raw, dict) and set(raw) == {'calls'}, 'QUERY_RAW_INVALID')
        expected = summary_from_calls(raw['calls'])
        metrics.update(evidence_verified=True, tool_call_count=len(raw['calls']))
        require(same_json(read_json(workspace, METADATA), {'calls': [project(c) for c in raw['calls']]}), 'QUERY_METADATA_MISMATCH')
        metrics['metadata_matches'] = True
        require(same_json(read_json(workspace, SUMMARY), expected), 'QUERY_SUMMARY_MISMATCH')
        metrics['summary_matches'] = True
        require(expected['status'] != 'error', 'QUERY_TOOL_ERROR')
        warnings = {'empty': ['QUERY_EMPTY'], 'incomplete': ['QUERY_INCOMPLETE']}.get(expected['status'], [])
        status = 'pass_with_warnings' if warnings else 'pass'
    except EvidenceError as exc:
        warnings, status = [str(exc)], 'failed'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        warnings, status = ['QUERY_EVIDENCE_INVALID'], 'failed'
    return ReviewResult(review_id=new_id('review'), job_id=job.job_id, status=status, warnings=warnings,
                        metrics=metrics, recommended_revision=status != 'pass')
