"""Compile extracted fields into source calls; never parse the user's query again."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from dataelf_server.scope_v2.contracts import (
    SOURCE_CONFIG, SCOPE_VERSION, TIMEZONE, TIMEZONE_NAME, ScopeCall, ScopePlan, ScopeV2Error, TimeWindow,
)
from .schema import Intent


SOURCE_MODULES = {
    "news": "brief", "twitter": "opinion", "github": "open_source",
    "huggingface": "open_source", "youtube": "dissemination",
}


def build_scope_plan(intent: Intent, query: str, *, now: datetime | None = None, max_pages: int = 50) -> ScopePlan:
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
        raise ValueError("max_pages must be an integer between 1 and 1000")
    current = (now or datetime.now(TIMEZONE)).astimezone(TIMEZONE)
    if len(intent.domains) != 1 or intent.domains[0].id != "ai_index":
        raise ScopeV2Error("unsupported_scope", "当前服务只支持 AI Index 数据域。")
    selected = set(intent.domains[0].sources or SOURCE_CONFIG)
    if not selected.issubset(SOURCE_CONFIG):
        raise ScopeV2Error("unsupported_scope", "请求包含当前服务不支持的数据来源。")
    sources = [source for source in SOURCE_CONFIG if source in selected]
    time_range = intent.time_range
    # Resolve omitted bounds in this service adapter, without changing extracted intent.
    end_date = date.fromisoformat(time_range.end_date) if time_range.end_date else current.date()
    start_date = date.fromisoformat(time_range.start_date) if time_range.start_date else (
        end_date if not time_range.end_date else end_date - timedelta(days=29)
    )
    if start_date > end_date:
        raise ScopeV2Error("invalid_time_expression", "开始日期不能晚于结束日期。")
    start = datetime.combine(start_date, time.min, tzinfo=TIMEZONE)
    end = datetime.combine(end_date, time.max, tzinfo=TIMEZONE)
    modules = list(dict.fromkeys(SOURCE_MODULES[source] for source in sources))
    comprehensive = selected == set(SOURCE_CONFIG)
    calls = []
    for source in sources:
        config = SOURCE_CONFIG[source]
        payload = dict(config["sort_payload"])
        if source == "news":
            payload.update(date_start=start_date.isoformat(), date_end=end_date.isoformat())
        calls.append(ScopeCall(
            source=source, method="POST", endpoint=config["endpoint"], payload=payload,
            page_size=config["page_size"], max_pages=max_pages,
            client_time_filter=True, stop_when_older=source != "news",
        ))
    return ScopePlan(
        version=SCOPE_VERSION, query=query, mode="comprehensive_daily" if comprehensive else "module_recent",
        modules=["comprehensive"] if comprehensive else modules,
        timezone=TIMEZONE_NAME, window=TimeWindow(start=start.isoformat(), end=end.isoformat()),
        calls=calls, time_mode="date_range", retrieval=intent.retrieval.model_dump(),
    )
