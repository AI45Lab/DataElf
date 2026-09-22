from __future__ import annotations

import json
from pathlib import Path

from dataelf.discovery.contracts import DiscoveryJob, ReviewResult
from dataelf.domains.finance.artifacts import output_schema_for_spec, validate_payload
from dataelf.schemas import new_id


def review_finance(job: DiscoveryJob, workspace: Path) -> ReviewResult:
    """Review the declared deliverable against its schema (single source: artifacts.py)."""
    schema = output_schema_for_spec(job.spec.parameters)
    try:
        payload = json.loads((workspace / schema.path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result(job, "failed", [f"Cannot read finance {schema.label}: {exc}"], {schema.metrics_key: False})
    review = validate_payload(schema, payload)
    if review.hard_error:
        return _result(job, "failed", [review.hard_error], review.metrics)
    status = "pass_with_warnings" if review.warnings else "pass"
    return _result(job, status, review.warnings, review.metrics)


def _result(job: DiscoveryJob, status: str, warnings: list[str], metrics: dict) -> ReviewResult:
    return ReviewResult(review_id=new_id("review"), job_id=job.job_id, status=status, warnings=warnings, recommended_revision=bool(warnings), metrics=metrics)


__all__ = ["review_finance"]
