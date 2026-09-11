from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InsightQueryRequest(BaseModel):
    """A natural-language DataElf discovery request."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    scope: Literal["scope_v2"] = "scope_v2"

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be blank")
        return normalized


class JobError(BaseModel):
    category: Literal[
        "intent_error",
        "source_error",
        "model_error",
        "analysis_error",
        "artifact_error",
        "service_error",
    ]
    reason: str
    message: str
    retryable: bool
    action: str
    stage: str
    details: dict[str, Any] | None = None


class Insight(BaseModel):
    model_config = ConfigDict(extra="allow")

    insight_id: str
    title: str
    thesis: str
    why_now: str
    supporting_signals: list[str] = Field(default_factory=list)
    analysis_artifacts: list[str]
    related_entities: list[str] = Field(default_factory=list)
    external_support: list[dict[str, Any]] = Field(default_factory=list)
    counterarguments: list[str]
    confidence: float = Field(ge=0, le=1)
    next_questions: list[str]


class JobRecord(BaseModel):
    attempt: int = 1
    job_id: str
    trace_id: str
    status: Literal["queued", "running", "completed", "failed"]
    stage: str
    progress: int = Field(ge=0, le=100)
    request: dict[str, Any]
    workspace_path: str
    insights: list[dict[str, Any]] | None = None
    source_trace_id: str | None = None
    error: JobError | None = None
    created_at: str
    started_at: str | None = None
    updated_at: str


def envelope(
    *,
    code: int | str,
    msg: str,
    trace_id: str,
    data: Any,
) -> dict[str, Any]:
    return {"code": code, "msg": msg, "trace_id": trace_id, "data": data}
