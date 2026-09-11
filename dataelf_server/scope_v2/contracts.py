from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from zoneinfo import ZoneInfo


SCOPE_VERSION = "ai_index_scope_v2"
TIMEZONE_NAME = "Asia/Shanghai"
TIMEZONE = ZoneInfo(TIMEZONE_NAME)


class ScopeV2Error(ValueError):
    def __init__(self, code: str, message: str, *, examples: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.examples = examples or []

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "examples": self.examples}


@dataclass(frozen=True)
class TimeWindow:
    start: str
    end: str


@dataclass(frozen=True)
class ScopeCall:
    source: str
    method: str
    endpoint: str
    payload: dict[str, Any]
    page_size: int
    max_pages: int
    client_time_filter: bool
    stop_when_older: bool


@dataclass(frozen=True)
class ScopePlan:
    version: str
    query: str
    mode: str
    modules: list[str]
    timezone: str
    window: TimeWindow
    calls: list[ScopeCall]
    time_mode: str = "date_range"

    retrieval: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SOURCE_CONFIG: dict[str, dict[str, Any]] = {
    "news": {
        "endpoint": "/openapi/news/search",
        "page_size": 50,
        "sort_payload": {},
        "client_time_filter": False,
    },
    "twitter": {
        "endpoint": "/openapi/ecosystem/xx/search",
        "page_size": 50,
        "sort_payload": {"sort_type": "publish_time"},
        "client_time_filter": True,
    },
    "github": {
        "endpoint": "/openapi/ecosystem/gh/search",
        "page_size": 50,
        "sort_payload": {"sort_type": "time"},
        "client_time_filter": True,
    },
    "huggingface": {
        "endpoint": "/openapi/ecosystem/hf/search",
        "page_size": 20,
        "sort_payload": {"asset_type": "all", "sort_type": "time"},
        "client_time_filter": True,
    },
    "youtube": {
        "endpoint": "/openapi/ecosystem/y2b/search",
        "page_size": 20,
        "sort_payload": {"sort_type": "publish_time"},
        "client_time_filter": True,
    },
}
