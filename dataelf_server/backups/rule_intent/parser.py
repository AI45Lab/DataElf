from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
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
    time_mode: str = "latest_on_or_before_date"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


COMPREHENSIVE_ALIASES = (
    "综合总结",
    "综合简报",
    "每日总结",
    "今日总结",
    "日报",
)

MODULE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("brief", ("快讯", "新闻")),
    ("opinion", ("观点", "twitter", "推特", "x平台")),
    ("open_source", ("开源社区", "开源", "github", "hugging face", "huggingface", "hf")),
    ("dissemination", ("传播", "youtube", "视频")),
)

MODULE_SOURCES: dict[str, tuple[str, ...]] = {
    "brief": ("news",),
    "opinion": ("twitter",),
    "open_source": ("github", "huggingface"),
    "dissemination": ("youtube",),
}

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

EXAMPLES = [
    "生成综合总结",
    "生成快讯模块总结",
    "生成开源社区和传播模块总结",
]

DATE_PATTERN = re.compile(
    r"(?:(?P<year>\d{4})\s*(?:年|[-/.])\s*)?"
    r"(?P<month>\d{1,2})\s*(?:月|[-/.])\s*"
    r"(?P<day>\d{1,2})\s*日?"
)
RELATIVE_DAYS = (
    ("大前天", 3),
    ("前天", 2),
    ("昨天", 1),
    ("昨日", 1),
    ("今天", 0),
    ("今日", 0),
    ("当天", 0),
)


def parse_scope(query: str, *, now: datetime | None = None) -> ScopePlan:
    """Map a natural-language digest request to concrete AI Index calls."""

    clean_query = query.strip()
    if not clean_query:
        raise ScopeV2Error("unsupported_scope", "自然语言指令不能为空。", examples=EXAMPLES)

    normalized = unicodedata.normalize("NFKC", clean_query).casefold()
    current = _local_now(now)

    if any(alias in normalized for alias in COMPREHENSIVE_ALIASES):
        mode = "comprehensive_daily"
        modules = ["comprehensive"]
        # Comprehensive means all four product modules.  The open-source
        # module expands to two physical AI Index sources, so this produces
        # five calls in stable module order.
        sources = _deduplicate_sources(list(MODULE_SOURCES))
    else:
        modules = [
            module
            for module, aliases in MODULE_ALIASES
            if any(_contains_alias(normalized, alias) for alias in aliases)
        ]
        if not modules:
            if "模块总结" in normalized or "模块汇总" in normalized:
                raise ScopeV2Error(
                    "module_required",
                    "模块总结必须至少指定快讯、观点、开源社区或传播中的一个模块。",
                    examples=EXAMPLES[1:],
                )
            raise ScopeV2Error(
                "unsupported_scope",
                "无法识别摘要类型；当前只支持综合总结和指定模块总结。",
                examples=EXAMPLES,
            )
        mode = "module_recent"
        sources = _deduplicate_sources(modules)

    start, end, time_mode = _parse_time_window(normalized, current)
    window = TimeWindow(start=start.isoformat(), end=end.isoformat())
    calls = [
        _build_call(
            source,
            start=start,
            end=end,
            # News supports a server-side date range and needs one request.
            # Ecosystem endpoints expose only descending time order, so they
            # need bounded pagination before client-side date filtering.
            max_pages=1 if source == "news" else 5,
            auto_latest=False,
        )
        for source in sources
    ]
    return ScopePlan(
        version=SCOPE_VERSION,
        query=clean_query,
        mode=mode,
        modules=modules,
        timezone=TIMEZONE_NAME,
        window=window,
        calls=calls,
        time_mode=time_mode,
    )


def _local_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(TIMEZONE)
    if value.tzinfo is None:
        return value.replace(tzinfo=TIMEZONE)
    return value.astimezone(TIMEZONE)


def _contains_alias(text: str, alias: str) -> bool:
    normalized_alias = alias.casefold()
    if normalized_alias == "hf":
        return re.search(r"(?<![a-z0-9])hf(?![a-z0-9])", text) is not None
    return normalized_alias in text


def _parse_time_window(
    text: str, current: datetime
) -> tuple[datetime, datetime, str]:
    explicit_dates = _explicit_dates(text, current.date())
    relative_matches = _relative_matches(text)
    recent_days = _recent_day_count(text)
    time_reference_count = len(explicit_dates) + len(relative_matches)
    if time_reference_count > 1 or (recent_days is not None and recent_days != 1):
        raise ScopeV2Error(
            "time_range_not_supported",
            "当前只支持单个目标日期，并在该日期及其前一个月内选择最近可用日；不支持时间范围查询。",
            examples=EXAMPLES,
        )

    if explicit_dates:
        target_date = explicit_dates[0]
    elif relative_matches:
        target_date = current.date() - timedelta(days=relative_matches[0][1])
    else:
        # No time expression, and the accepted "近1天" compatibility form,
        # both mean the current natural day.
        target_date = current.date()

    start_date = _one_calendar_month_before(target_date)
    end = (
        current
        if target_date == current.date()
        else datetime.combine(target_date, time.max, tzinfo=TIMEZONE)
    )
    return _day_start(start_date), end, "latest_on_or_before_date"


def _explicit_dates(text: str, current_date: date) -> list[date]:
    values: list[date] = []
    for match in DATE_PATTERN.finditer(text):
        year = int(match.group("year") or current_date.year)
        try:
            value = date(year, int(match.group("month")), int(match.group("day")))
        except ValueError as exc:
            raise ScopeV2Error(
                "invalid_time_expression",
                f"无法识别日期：{match.group(0)}。",
                examples=EXAMPLES,
            ) from exc
        values.append(value)
    return values


def _recent_day_count(text: str) -> int | None:
    match = re.search(r"(?:近|最近|过去)\s*(\d{1,2})\s*天", text)
    if match:
        value = int(match.group(1))
        return value
    if any(value in text for value in ("近一周", "最近一周", "过去一周", "近期")):
        return 7
    return None


def _relative_matches(text: str) -> list[tuple[str, int]]:
    """Find relative-day aliases while preferring longer overlapping phrases."""

    found: list[tuple[str, int]] = []
    occupied: list[tuple[int, int]] = []
    for alias, days_ago in sorted(RELATIVE_DAYS, key=lambda item: len(item[0]), reverse=True):
        for match in re.finditer(re.escape(alias), text):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in occupied):
                continue
            occupied.append(span)
            found.append((alias, days_ago))
    return found


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=TIMEZONE)


def _one_calendar_month_before(value: date) -> date:
    """Return the same calendar day in the preceding month, clamped if needed."""

    if value.month == 1:
        year, month = value.year - 1, 12
    else:
        year, month = value.year, value.month - 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _deduplicate_sources(modules: list[str]) -> list[str]:
    sources: list[str] = []
    for module in modules:
        for source in MODULE_SOURCES[module]:
            if source not in sources:
                sources.append(source)
    return sources


def _build_call(
    source: str,
    *,
    start: datetime,
    end: datetime,
    max_pages: int,
    auto_latest: bool,
) -> ScopeCall:
    config = SOURCE_CONFIG[source]
    payload = dict(config["sort_payload"])
    if source == "news" and not auto_latest:
        payload.update(
            {
                "date_start": start.date().isoformat(),
                "date_end": end.date().isoformat(),
            }
        )
    client_filter = bool(config["client_time_filter"])
    return ScopeCall(
        source=source,
        method="POST",
        endpoint=str(config["endpoint"]),
        payload=payload,
        page_size=int(config["page_size"]),
        max_pages=max_pages,
        client_time_filter=client_filter,
        stop_when_older=client_filter,
    )
