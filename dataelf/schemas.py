from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class RawArtifact(BaseModel):
    raw_id: str
    job_id: str
    connector: str
    endpoint: str
    request: dict[str, Any]
    content_hash: str
    content_uri: str
    created_at: datetime = Field(default_factory=now_utc)


class RecordEnvelope(BaseModel):
    record_id: str
    job_id: str
    source: str
    source_type: str
    source_id: str
    observed_at: datetime = Field(default_factory=now_utc)
    payload: dict[str, Any]
    raw_id: str | None = None


class DomainObject(BaseModel):
    object_id: str
    job_id: str
    domain: str
    object_type: str
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)
    source_record_ids: list[str] = Field(default_factory=list)


class DomainRelation(BaseModel):
    relation_id: str
    job_id: str
    domain: str
    relation_type: str
    source_object_id: str
    target_object_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    source_record_ids: list[str] = Field(default_factory=list)
