from __future__ import annotations

import io
import json
import urllib.error
from copy import deepcopy
from datetime import datetime

import pytest

from dataelf_server.intent import Domain, IntentError, IntentRecognizer, Profile, SERVE_PROFILE, Source
from dataelf_server.intent import IntentModelConfig

BASE_URL = "http://127.0.0.1:49153/v1/chat/completions"
MODEL_NAME = "test-intent"
CONFIG = IntentModelConfig(model_name=MODEL_NAME, base_url=BASE_URL, api_key="test-key")


REFERENCE = datetime.fromisoformat("2026-09-08T12:00:00+08:00")


def response(payload=None, *, finish_reason="stop", content=None):
    return {"choices": [{"finish_reason": finish_reason, "message": {
        "content": content if content is not None else json.dumps(payload or SERVE_PROFILE.defaults().model_dump()),
    }}]}


def mock_model(monkeypatch, envelope):
    requests = []
    def open_request(request, *, timeout):
        requests.append((request, timeout))
        return io.BytesIO(json.dumps(envelope).encode())
    monkeypatch.setattr("urllib.request.urlopen", open_request)
    return requests


def test_one_call_with_schema_reference_time_and_fixed_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://must-not-replace-endpoint.invalid")
    calls = mock_model(monkeypatch, response())
    result = IntentRecognizer(config=CONFIG).extract("看看", reference_time=REFERENCE)
    assert result == SERVE_PROFILE.defaults()
    assert len(calls) == 1
    request, timeout = calls[0]
    payload = json.loads(request.data)
    assert request.full_url == BASE_URL
    assert payload["model"] == MODEL_NAME
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "2026-09-08T12:00:00+08:00" in payload["messages"][0]["content"]
    assert payload["messages"][1] == {"role": "user", "content": "看看"}
    assert timeout == 90


@pytest.mark.parametrize("path,value", [
    (("time_range", "start_date"), "2026-02-30"),
    (("time_range", "start_date"), "2026-9-8"),
    (("retrieval", "keywords"), [1]),
    (("retrieval", "keywords"), [" "]),
    (("retrieval", "keywords"), ["智能体", "智能体"]),
    (("retrieval", "entities"), "OpenAI"),
    (("output", "task_types"), None),
])
def test_malformed_fields_fail_without_fabricated_defaults(monkeypatch, path, value):
    payload = SERVE_PROFILE.defaults().model_dump()
    payload[path[0]][path[1]] = value
    mock_model(monkeypatch, response(payload))
    with pytest.raises(IntentError, match="contract"):
        IntentRecognizer(config=CONFIG).extract("测试", reference_time=REFERENCE)


@pytest.mark.parametrize("change", ["missing", "extra", "unknown_domain", "unknown_source", "duplicate_domain", "reverse_dates"])
def test_contract_and_capability_errors(monkeypatch, change):
    payload = SERVE_PROFILE.defaults().model_dump()
    if change == "missing":
        del payload["output"]
    elif change == "extra":
        payload["explanation"] = "should not be accepted"
    elif change == "unknown_domain":
        payload["domains"][0]["id"] = "other"
    elif change == "unknown_source":
        payload["domains"][0]["sources"] = ["arxiv"]
    elif change == "duplicate_domain":
        payload["domains"] *= 2
    else:
        payload["time_range"] = {"start_date": "2026-09-08", "end_date": "2026-09-01"}
    mock_model(monkeypatch, response(payload))
    with pytest.raises(IntentError):
        IntentRecognizer(config=CONFIG).extract("测试", reference_time=REFERENCE)


@pytest.mark.parametrize("envelope", [
    {}, {"choices": []}, [],
    response(finish_reason="length"),
    response(content="```json\n{}\n```"),
    response(content="null"),
    {"choices": [{"finish_reason": "stop", "message": {"content": None, "refusal": "refused"}}]},
])
def test_invalid_envelopes_and_incomplete_output_fail(monkeypatch, envelope):
    mock_model(monkeypatch, envelope)
    with pytest.raises(IntentError):
        IntentRecognizer(config=CONFIG).extract("测试", reference_time=REFERENCE)


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError(BASE_URL, 503, "private server details", {}, None),
    urllib.error.URLError("private proxy details"),
    TimeoutError("private timeout details"),
])
def test_transport_errors_are_bounded_and_do_not_expose_response_details(monkeypatch, error):
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise error
    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(IntentError) as caught:
        IntentRecognizer(config=CONFIG).extract("测试")
    assert "private" not in str(caught.value)
    assert len(calls) == 1


def test_custom_domain_and_source_relationships(monkeypatch):
    profile = Profile(domains=(
        Domain("papers", "论文", (Source("arxiv", "预印本"),)),
        Domain("code", "代码", (Source("gitlab", "代码仓库"),)),
    ), default_domains=("papers",))
    payload = profile.defaults().model_dump()
    payload["domains"] = [{"id": "code", "sources": ["gitlab"]}]
    calls = mock_model(monkeypatch, response(payload))
    result = IntentRecognizer(profile, config=CONFIG).extract("查看 GitLab", reference_time=REFERENCE)
    assert result.domains[0].id == "code"
    schema = json.loads(calls[0][0].data)["response_format"]["json_schema"]["schema"]
    assert len(schema["properties"]["domains"]["items"]["anyOf"]) == 2
    invalid = deepcopy(payload)
    invalid["domains"][0]["sources"] = ["arxiv"]
    with pytest.raises(ValueError):
        profile.validate(invalid)


def test_timezone_conversion_and_open_ended_range(monkeypatch):
    payload = SERVE_PROFILE.defaults().model_dump()
    payload["time_range"]["end_date"] = "2026-09-08"
    calls = mock_model(monkeypatch, response(payload))
    result = IntentRecognizer(config=CONFIG).extract("截至今天", reference_time=datetime.fromisoformat("2026-09-07T18:00:00+00:00"))
    assert result.time_range.start_date is None
    assert "2026-09-08T02:00:00+08:00" in json.loads(calls[0][0].data)["messages"][0]["content"]


def test_bad_inputs_do_not_call_model(monkeypatch):
    calls = mock_model(monkeypatch, response())
    with pytest.raises(ValueError):
        IntentRecognizer(config=CONFIG).extract(" ")
    with pytest.raises(ValueError):
        IntentRecognizer(config=CONFIG).extract("今天", reference_time=datetime(2026, 9, 8))
    assert not calls
