from __future__ import annotations

import io
import json
from datetime import datetime

import pytest

from dataelf_server.intent import IntentModelConfig, IntentRecognizer, SERVE_PROFILE
from dataelf_server.intent.planner import build_scope_plan
from dataelf_server.scope_v2.runner import ScopeV2Executor
from tests.server.test_scope_v2 import SequentialFakeClient, api_response


NOW = datetime.fromisoformat("2026-09-08T12:00:00+08:00")


def intent(*, sources=None, start="2026-08-01", end="2026-08-02", retrieval=None):
    value = SERVE_PROFILE.defaults().model_dump()
    value["domains"][0]["sources"] = sources or []
    value["time_range"] = {"start_date": start, "end_date": end}
    if retrieval:
        value["retrieval"] = retrieval
    value["output"]["focus_points"] = ["DO NOT INJECT INTO EXISTING SOP"]
    return SERVE_PROFILE.validate(value)


def test_config_requires_model_and_resolves_environment_defaults(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    config = IntentModelConfig(model_name="model").resolve()
    assert config.endpoint == "http://example.test/v1/chat/completions"
    assert config.api_key == "env-key"
    assert IntentRecognizer(config=IntentModelConfig(model_name="model")).config.endpoint == config.endpoint
    explicit = IntentModelConfig(model_name="model", base_url="http://other.test/v1/chat/completions/", api_key="explicit").resolve()
    assert explicit.endpoint == "http://other.test/v1/chat/completions"
    assert explicit.api_key == "explicit"
    assert "explicit" not in repr(explicit)
    with pytest.raises(ValueError, match="model_name"):
        IntentModelConfig().resolve().validate_for_run()


def test_config_loads_current_yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("server:\n  intent:\n    model_name: local-model\n    base_url: http://configured.test/v1\n    api_key: abc\n")
    monkeypatch.setenv("DATAELF_CONFIG_FILE", str(path))
    from dataelf_server.settings import Settings
    assert IntentModelConfig.from_env() == Settings.from_env().intent_config
    assert IntentModelConfig.from_env().model_name == "local-model"


@pytest.mark.parametrize("sources,modules", [
    (["github", "huggingface"], ["open_source"]),
    (["twitter", "youtube"], ["opinion", "dissemination"]),
    ([], ["comprehensive"]),
])
def test_plan_compilation_uses_fields_without_reparsing_original_query(sources, modules):
    plan = build_scope_plan(intent(sources=sources), "这句话不包含任何模块或日期信息", now=NOW)
    assert plan.modules == modules
    assert plan.time_mode == "date_range"
    assert plan.window.start.startswith("2026-08-01T00:00:00")
    assert plan.window.end.startswith("2026-08-02T23:59:59")
    assert "DO NOT INJECT" not in json.dumps(plan.to_dict())
    assert {call.source for call in plan.calls} == set(sources or ["news", "twitter", "github", "huggingface", "youtube"])


@pytest.mark.parametrize("start,end,expected", [
    (None, None, ("2026-09-08", "2026-09-08")),
    (None, "2026-09-01", ("2026-08-03", "2026-09-01")),
    ("2026-09-01", None, ("2026-09-01", "2026-09-08")),
])
def test_service_date_defaults_do_not_mutate_extracted_intent(start, end, expected):
    extracted = intent(start=start, end=end)
    plan = build_scope_plan(extracted, "input", now=NOW)
    assert (plan.window.start[:10], plan.window.end[:10]) == expected
    assert extracted.time_range.start_date == start and extracted.time_range.end_date == end


def test_exact_range_keeps_multiple_days_and_paginates_news(tmp_path):
    plan = build_scope_plan(intent(sources=["news"]), "input", now=NOW)
    first = [{"news_id": str(i), "date": "2026-08-01", "title": "day one"} for i in range(50)]
    second = [{"news_id": "50", "date": "2026-08-02", "title": "day two"}]
    fake = SequentialFakeClient([api_response(first, total=51, trace="one"), api_response(second, total=51, trace="two")])
    result = ScopeV2Executor(client=fake, output_root=tmp_path).execute(plan)["sources"]["news"]
    assert result["kept_count"] == 51 and result["pages_requested"] == 2
    assert result["scan_complete"] and not result["fallback_applied"]
    assert {item["published_at"][:10] for item in result["items"]} == {"2026-08-01", "2026-08-02"}


def test_exact_date_never_falls_back_to_older_data(tmp_path):
    plan = build_scope_plan(intent(sources=["github"], start="2026-08-07", end="2026-08-07"), "input", now=NOW)
    fake = SequentialFakeClient([api_response([{"url": "https://example.test/repo", "published_at": "2026-08-06", "title": "old"}], total=1, trace="trace")])
    result = ScopeV2Executor(client=fake, output_root=tmp_path).execute(plan)["sources"]["github"]
    assert result["kept_count"] == 0
    assert result["effective_date"] is None and not result["fallback_applied"]


def test_configurable_scan_reaches_records_beyond_original_limit(tmp_path):
    extracted = intent(sources=["twitter"], start="2026-08-03", end="2026-08-03")
    responses = [api_response([{"tweet_post_id": str(i), "published_at": "2026-09-02", "text": "recent"}], total=2551, trace=str(i)) for i in range(50)]
    responses += [api_response([{"tweet_post_id": "historical", "published_at": "2026-08-03", "text": "target"},
                               {"tweet_post_id": "old", "published_at": "2026-08-02", "text": "old"}], total=2551, trace="last")]
    plan = build_scope_plan(extracted, "input", now=NOW, max_pages=100)
    result = ScopeV2Executor(client=SequentialFakeClient(responses), output_root=tmp_path).execute(plan)["sources"]["twitter"]
    assert result["pages_requested"] == 51 and result["scan_complete"]
    assert result["kept_count"] == 1 and result["items"][0]["published_at"].startswith("2026-08-03")
    assert not result["fallback_applied"]


def test_retrieval_filters_apply_before_modeling(tmp_path):
    value = intent(sources=["news"], retrieval={"keywords": ["agent"], "entities": ["OpenAI"], "exclude_keywords": ["招聘"]})
    plan = build_scope_plan(value, "input", now=NOW)
    rows = [{"news_id": str(i), "date": "2026-08-01", "title": title} for i, title in enumerate([
        "OpenAI Agent release", "OpenAI agent 招聘", "Other agent release", "OpenAI unrelated release",
    ])]
    fake = SequentialFakeClient([api_response(rows, total=4, trace="trace")])
    result = ScopeV2Executor(client=fake, output_root=tmp_path).execute(plan)["sources"]["news"]
    assert [item["title"] for item in result["items"]] == ["OpenAI Agent release"]


def test_server_pipeline_calls_llm_and_consumes_writing_section_output(tmp_path, monkeypatch):
    from tests.server.test_workflow import components
    from dataelf_server.workflows.pipeline import ServerPipeline
    cfg, profile, explorer, _ = components(tmp_path)
    extracted = intent(sources=["news"], start="2026-08-27", end="2026-08-27")
    extracted.output.focus_points = ["商业价值"]
    query = "自定义查询\n# 写作要求\n重点分析商业价值"
    requests = []
    def model(request, *, timeout):
        requests.append(json.loads(request.data))
        sections = json.loads(requests[-1]["messages"][1]["content"])["input_sections"]
        response = {"_writing_sections": [sections[-1]["id"]], **extracted.model_dump()}
        return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response)}}]}).encode())
    monkeypatch.setattr("urllib.request.urlopen", model)
    profile.recognizer = IntentRecognizer(config=IntentModelConfig(model_name="test", base_url="http://model.test/v1"))
    pipeline = ServerPipeline(cfg, profile_factory=lambda settings: profile, explorer_factory=lambda settings: explorer)
    workspace = tmp_path / "attempt"
    result = pipeline.run(job_id="job_llm", request_payload={"query": query}, workspace_path=workspace,
                          progress=lambda *args: None, source_trace=lambda *args: None)
    assert result and len(requests) == 1
    assert json.loads(requests[0]["messages"][1]["content"])["input_sections"][0]["body"].strip() == "自定义查询"
    spec = json.loads((workspace / "job_spec.json").read_text())
    assert spec["parameters"]["intent_input"]["domains"][0]["sources"] == ["news"]
    prompt = (workspace / "prompts/discovery_prompt.md").read_text()
    assert "商业价值" in prompt and "自定义查询" not in prompt
    assert spec["parameters"]["output"]["focus_points"] == ["商业价值"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_intent_failure_or_cancellation_never_starts_acquisition(tmp_path, cancelled):
    from dataelf.discovery.contracts import JobSpec
    from dataelf.discovery.run_control import RunControl
    from dataelf.discovery.workflow import run_job
    from dataelf_server.intent import IntentError
    from dataelf_server.presentation.errors import classify_job_error
    from dataelf_server.workflows.profile import ServerProfile
    from tests.server.helpers import settings
    control = RunControl(workspace_path=tmp_path / "attempt")
    class Recognizer:
        def extract(self, query, **kwargs):
            if cancelled:
                control.cancel()
                return intent()
            raise IntentError("Model response violates the intent contract")
    def unexpected(*args, **kwargs):
        pytest.fail("Acquisition must not run after failed or cancelled intent recognition")
    cfg = settings(project_root=tmp_path, state_dir=tmp_path / "state")
    profile = ServerProfile(cfg, recognizer=Recognizer(), prefetcher=unexpected)
    job = run_job(JobSpec(domain="ai_index", objective="生成综合总结", workflow_profile="server"),
                  cfg.execution_config(), plugin=profile, explorer=object(), control=control)
    assert job.status == "failed"
    assert job.error_code == ("RUN_CANCELLED" if cancelled else "INTENT_MODEL_FAILED")
    if not cancelled:
        public = classify_job_error(job.error_code, job.error_message, "recognizing_intent")
        assert public.category == "model_error" and public.reason == "intent_recognition_failed"
    assert not list((tmp_path / "attempt").glob("scope_v2/*/plan.json"))
