from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dataelf_server.scope_v2.client import ScopeV2AIIndexClient, ScopeV2AIIndexError
from dataelf_server.scope_v2.integration import (
    ScopeV2IntegrationError,
    ScopeV2PrefetchResult,
    materialize_filtered_ai_index_envelopes,
    prefetch_scope_v2,
)
from dataelf_server.backups.rule_intent.parser import ScopeV2Error, parse_scope
from dataelf_server.scope_v2.runner import ScopeV2Executor, load_env_file


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 18, 14, 30, tzinfo=SHANGHAI)


@pytest.mark.parametrize(
    "query",
    ["生成综合总结", "综合简报", "每日总结", "日报", "今日总结"],
)
def test_comprehensive_aliases_create_daily_plan(query: str) -> None:
    plan = parse_scope(query, now=NOW)
    assert plan.mode == "comprehensive_daily"
    assert plan.modules == ["comprehensive"]
    assert [call.source for call in plan.calls] == [
        "news",
        "twitter",
        "github",
        "huggingface",
        "youtube",
    ]
    assert [call.max_pages for call in plan.calls] == [1, 5, 5, 5, 5]
    assert plan.window.start == "2026-07-18T00:00:00+08:00"
    assert plan.window.end == "2026-08-18T14:30:00+08:00"
    assert plan.time_mode == "latest_on_or_before_date"
    assert plan.calls[0].payload == {
        "date_start": "2026-07-18",
        "date_end": "2026-08-18",
    }


def test_comprehensive_takes_precedence_over_modules() -> None:
    plan = parse_scope("综合总结，同时看看开源社区", now=NOW)
    assert plan.modules == ["comprehensive"]
    assert [call.source for call in plan.calls] == [
        "news",
        "twitter",
        "github",
        "huggingface",
        "youtube",
    ]


@pytest.mark.parametrize(
    ("query", "module", "sources"),
    [
        ("快讯模块总结", "brief", ["news"]),
        ("新闻总结", "brief", ["news"]),
        ("Twitter 观点", "opinion", ["twitter"]),
        ("推特观点", "opinion", ["twitter"]),
        ("X平台观点", "opinion", ["twitter"]),
        ("开源社区", "open_source", ["github", "huggingface"]),
        ("GitHub 和 HF", "open_source", ["github", "huggingface"]),
        ("Hugging Face 开源总结", "open_source", ["github", "huggingface"]),
        ("YouTube 传播", "dissemination", ["youtube"]),
        ("视频传播", "dissemination", ["youtube"]),
    ],
)
def test_module_aliases(query: str, module: str, sources: list[str]) -> None:
    plan = parse_scope(query, now=NOW)
    assert plan.mode == "module_recent"
    assert plan.modules == [module]
    assert [call.source for call in plan.calls] == sources
    assert all(call.max_pages == (1 if call.source == "news" else 5) for call in plan.calls)
    assert plan.time_mode == "latest_on_or_before_date"
    assert plan.window.start == "2026-07-18T00:00:00+08:00"


def test_multi_module_plan_deduplicates_sources_and_sets_exact_payloads() -> None:
    plan = parse_scope("生成开源社区、观点和传播模块总结", now=NOW)
    assert plan.modules == ["opinion", "open_source", "dissemination"]
    assert [call.source for call in plan.calls] == [
        "twitter",
        "github",
        "huggingface",
        "youtube",
    ]
    calls = {call.source: call for call in plan.calls}
    assert calls["twitter"].payload == {"sort_type": "publish_time"}
    assert calls["github"].payload == {"sort_type": "time"}
    assert calls["huggingface"].payload == {
        "asset_type": "all",
        "sort_type": "time",
    }
    assert calls["youtube"].page_size == 20


def test_plain_x_is_not_a_twitter_alias() -> None:
    with pytest.raises(ScopeV2Error) as caught:
        parse_scope("分析 x 模块", now=NOW)
    assert caught.value.code == "unsupported_scope"


def test_module_name_is_required() -> None:
    with pytest.raises(ScopeV2Error) as caught:
        parse_scope("生成模块总结", now=NOW)
    assert caught.value.code == "module_required"


@pytest.mark.parametrize(
    ("query", "start", "end"),
    [
        ("今天的快讯", "2026-07-18", "2026-08-18"),
        ("昨天的快讯", "2026-07-17", "2026-08-17"),
        ("前天的观点", "2026-07-16", "2026-08-16"),
        ("大前天的快讯", "2026-07-15", "2026-08-15"),
        ("2026年8月15日的快讯", "2026-07-15", "2026-08-15"),
        ("生成 2026 年 8 月 15 日的快讯", "2026-07-15", "2026-08-15"),
        ("2026年8月19日的快讯", "2026-07-19", "2026-08-19"),
        ("近1天的传播总结", "2026-07-18", "2026-08-18"),
    ],
)
def test_single_time_expression_bounds_search_at_requested_day(
    query: str, start: str, end: str
) -> None:
    plan = parse_scope(query, now=NOW)
    assert plan.window.start.startswith(start)
    assert plan.window.end.startswith(end)
    assert plan.time_mode == "latest_on_or_before_date"
    if plan.calls[0].source == "news":
        assert plan.calls[0].payload["date_start"] == start
        assert plan.calls[0].payload["date_end"] == end


def test_previous_month_clamps_end_of_month() -> None:
    plan = parse_scope(
        "2026年3月31日的快讯",
        now=datetime(2026, 4, 2, 9, 0, tzinfo=SHANGHAI),
    )
    assert plan.window.start == "2026-02-28T00:00:00+08:00"
    assert plan.window.end.startswith("2026-03-31T23:59:59")


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("2026年2月30日的快讯", "invalid_time_expression"),
        ("昨天到今天的综合总结", "time_range_not_supported"),
        ("8月15日到8月17日的观点", "time_range_not_supported"),
        ("昨天的2026年8月17日快讯", "time_range_not_supported"),
        ("近7天的传播总结", "time_range_not_supported"),
        ("近32天的快讯", "time_range_not_supported"),
        ("近期的开源社区总结", "time_range_not_supported"),
    ],
)
def test_invalid_time_expressions(query: str, code: str) -> None:
    with pytest.raises(ScopeV2Error) as caught:
        parse_scope(query, now=NOW)
    assert caught.value.code == code


class Response:
    def __init__(self, payload: dict):
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.raw


def test_client_authenticates_and_retries_transient_server_error() -> None:
    calls = []
    sleeps = []

    def opener(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            body = io.BytesIO(b'{"code":"busy","message":"retry"}')
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, body)
        return Response({"code": 0, "data": {"total": 0, "list": []}})

    client = ScopeV2AIIndexClient(
        base_url="https://index.example/api/v2",
        api_key="dummy-key",
        opener=opener,
        sleeper=sleeps.append,
        min_request_interval_seconds=0,
    )
    client.post("/openapi/news/search", {"page": 1, "size": 50})
    assert len(calls) == 2
    assert calls[0].get_header("X-ai-index-key") == "dummy-key"
    assert sleeps == [1.0]


def test_client_honors_retry_after_for_429() -> None:
    attempts = 0
    sleeps = []

    def opener(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            body = io.BytesIO(b'{"code":"qps_limit_exceeded","message":"slow down"}')
            headers = {"Retry-After": "0.25"}
            raise urllib.error.HTTPError(request.full_url, 429, "rate", headers, body)
        return Response({"code": 0, "data": {"total": 0, "list": []}})

    client = ScopeV2AIIndexClient(
        base_url="https://index.example/api/v2",
        api_key="dummy-key",
        opener=opener,
        sleeper=sleeps.append,
        min_request_interval_seconds=0,
    )
    client.post("/openapi/news/search", {"page": 1, "size": 50})
    assert attempts == 2
    assert sleeps == [0.25]


def test_client_rejects_business_error() -> None:
    client = ScopeV2AIIndexClient(
        base_url="https://index.example/api/v2",
        api_key="dummy-key",
        opener=lambda request, timeout: Response(
            {
                "code": -10002,
                "msg": "bad filter",
                "trace_id": "bad-trace",
            }
        ),
        min_request_interval_seconds=0,
    )
    with pytest.raises(ScopeV2AIIndexError) as caught:
        client.post("/openapi/news/search", {"page": 1, "size": 50})
    assert caught.value.code == "-10002"
    assert caught.value.trace_id == "bad-trace"


class FakeClient:
    def __init__(self, pages: dict[tuple[str, int], dict]):
        self.pages = pages
        self.calls: list[tuple[str, dict]] = []

    def post(self, endpoint: str, payload: dict) -> dict:
        self.calls.append((endpoint, payload))
        return self.pages[(endpoint, payload["page"])]


class SequentialFakeClient:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, endpoint: str, payload: dict) -> dict:
        self.calls.append((endpoint, payload))
        return self.responses.pop(0)


def api_response(items: list[dict], *, total: int, trace: str) -> dict:
    return {
        "code": 0,
        "msg": "success",
        "trace_id": trace,
        "data": {"total": total, "list": items},
    }


def test_ecosystem_pages_backward_and_keeps_latest_day_not_after_target(
    tmp_path: Path,
) -> None:
    endpoint = "/openapi/ecosystem/xx/search"
    first_page = api_response(
        [
            {
                "tweet_post_id": "too-new-1",
                "published_at": "2026-08-17 10:00:00",
                "text": "after target",
            },
            {
                "tweet_post_id": "too-new-2",
                "published_at": "2026-08-16 10:00:00",
                "text": "after target",
            },
        ],
        total=100,
        trace="page-1-trace",
    )
    second_page = api_response(
        [
            {
                "tweet_post_id": "target-day",
                "published_at": "2026-08-15 09:00:00",
                "text": "target day",
            },
            {
                "tweet_post_id": "older-day",
                "published_at": "2026-08-14 09:00:00",
                "text": "older day",
            },
        ],
        total=100,
        trace="page-2-trace",
    )
    fake = SequentialFakeClient([first_page, second_page])
    plan = parse_scope("2026年8月15日的观点模块总结", now=NOW)

    result = ScopeV2Executor(
        client=fake, output_root=tmp_path  # type: ignore[arg-type]
    ).execute(plan, run_id="run_historical")

    source = result["sources"]["twitter"]
    assert source["pages_requested"] == 2
    assert source["probe_pages_requested"] == 0
    assert source["formal_pages_requested"] == 2
    assert source["requested_date"] == "2026-08-15"
    assert source["window_start"] == "2026-07-15"
    assert source["effective_date"] == "2026-08-15"
    assert source["fallback_applied"] is False
    assert source["scan_complete"] is True
    assert source["trace_ids"] == ["page-1-trace", "page-2-trace"]
    assert [item["source_id"] for item in source["items"]] == [
        "twitter:target-day"
    ]
    assert len(fake.calls) == 2
    assert fake.calls[0][1] == {
        "sort_type": "publish_time",
        "page": 1,
        "size": 50,
    }
    assert fake.calls[1][1] == {
        "sort_type": "publish_time",
        "page": 2,
        "size": 50,
    }
    assert (tmp_path / "run_historical" / "raw" / "twitter_range_page_1.json").exists()
    assert (tmp_path / "run_historical" / "raw" / "twitter_range_page_2.json").exists()


def test_news_uses_one_server_side_month_range_request(
    tmp_path: Path,
) -> None:
    fake = SequentialFakeClient([
        api_response(
            [
                {"news_id": "latest", "date": "2026-08-15", "title": "Latest"},
                {"news_id": "older", "date": "2026-08-14", "title": "Older"},
            ],
            total=2,
            trace="range-trace",
        )
    ])
    plan = parse_scope("2026年8月15日的快讯模块总结", now=NOW)

    result = ScopeV2Executor(
        client=fake, output_root=tmp_path  # type: ignore[arg-type]
    ).execute(plan, run_id="run_range_news")

    assert fake.calls[0][1] == {
        "date_start": "2026-07-15",
        "date_end": "2026-08-15",
        "page": 1,
        "size": 50,
    }
    assert len(fake.calls) == 1
    assert result["sources"]["news"]["effective_date"] == "2026-08-15"
    assert result["sources"]["news"]["kept_count"] == 1


def test_empty_date_bounded_search_stops_with_no_items(tmp_path: Path) -> None:
    endpoint = "/openapi/news/search"
    fake = FakeClient(
        {(endpoint, 1): api_response([], total=0, trace="empty-probe")}
    )
    plan = parse_scope("快讯模块总结", now=NOW)

    result = ScopeV2Executor(
        client=fake, output_root=tmp_path  # type: ignore[arg-type]
    ).execute(plan, run_id="run_auto_empty")

    source = result["sources"]["news"]
    assert source["kept_count"] == 0
    assert source["effective_date"] is None
    assert source["pages_requested"] == 1
    assert result["warnings"] == [
        "news: no records found from 2026-07-18 through 2026-08-18"
    ]


def test_prefetch_returns_no_source_data_when_every_probe_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def empty_execute(self, plan):
        return {
            "artifact_dir": str(tmp_path),
            "sources": {
                "news": {"kept_count": 0, "trace_ids": []},
            },
        }

    monkeypatch.setattr(ScopeV2Executor, "execute", empty_execute)
    plan = parse_scope("快讯模块总结", now=NOW)

    with pytest.raises(ScopeV2IntegrationError) as caught:
        prefetch_scope_v2(
            plan,
            tmp_path,
            base_url="https://index.example/api/v2",
            api_key="dummy-key",
        )

    assert caught.value.code == "no_source_data"


def test_executor_persists_sanitized_failure(tmp_path: Path) -> None:
    class BrokenClient:
        def post(self, endpoint: str, payload: dict) -> dict:
            raise ScopeV2AIIndexError("rate_limited", "slow down", http_status=429)

    plan = parse_scope("快讯", now=NOW)
    executor = ScopeV2Executor(
        client=BrokenClient(), output_root=tmp_path  # type: ignore[arg-type]
    )
    with pytest.raises(ScopeV2AIIndexError):
        executor.execute(plan, run_id="run_failed")
    failure = json.loads((tmp_path / "run_failed" / "error.json").read_text())
    assert failure["error"] == {
        "code": "rate_limited",
        "message": "slow down",
        "trace_id": None,
        "http_status": 429,
    }


def test_load_env_file_does_not_override_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nexport AI_INDEX_BASE_URL='https://index.example/api/v2'\n"
        'AI_INDEX_API_KEY="file-key"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_INDEX_BASE_URL", "temporary-test-value")
    monkeypatch.delenv("AI_INDEX_BASE_URL")
    monkeypatch.setenv("AI_INDEX_API_KEY", "process-key")
    load_env_file(env_file)
    assert __import__("os").environ["AI_INDEX_API_KEY"] == "process-key"
    assert __import__("os").environ["AI_INDEX_BASE_URL"] == "https://index.example/api/v2"


def test_materialize_filtered_envelope_for_plain_pi(tmp_path: Path) -> None:
    plan = parse_scope("快讯模块总结", now=NOW)
    result_path = tmp_path / "scope_v2" / "run_test" / "result.json"
    result_path.parent.mkdir(parents=True)
    execution = {
        "artifact_dir": str(result_path.parent),
        "sources": {
            "news": {
                "trace_ids": ["trace-news"],
                "raw_files": ["raw/news_page_1.json"],
                "kept_count": 1,
                "items": [
                    {
                        "source": "news",
                        "source_id": "news:n1",
                        "data": {
                            "news_id": "n1",
                            "title": "News",
                            "date": "2026-08-18",
                        },
                    }
                ],
            }
        },
    }
    result_path.write_text(json.dumps(execution), encoding="utf-8")
    prefetch = ScopeV2PrefetchResult(
        execution=execution,
        result_path=result_path,
        trace_ids=["trace-news"],
        item_count=1,
    )
    paths = materialize_filtered_ai_index_envelopes(plan, prefetch, tmp_path)
    assert len(paths) == 1
    envelope = json.loads(paths[0].read_text(encoding="utf-8"))
    assert envelope["source"] == "ai_index"
    assert envelope["mode"] == "api"
    assert envelope["endpoint"] == "/openapi/news/search"
    assert envelope["trace_id"] == "trace-news"
    assert envelope["data"]["list"][0]["news_id"] == "n1"
    assert "AI_INDEX_API_KEY" not in json.dumps(envelope)


def test_materialize_rejects_empty_prefetch(tmp_path: Path) -> None:
    plan = parse_scope("快讯模块总结", now=NOW)
    prefetch = ScopeV2PrefetchResult(
        execution={"sources": {"news": {"items": [], "trace_ids": []}}},
        result_path=tmp_path / "result.json",
        trace_ids=[],
        item_count=0,
    )
    with pytest.raises(ScopeV2IntegrationError) as caught:
        materialize_filtered_ai_index_envelopes(plan, prefetch, tmp_path)
    assert caught.value.code == "no_source_data"
