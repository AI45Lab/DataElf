from llm.openai_provider import OpenAIProvider


def _make_provider(base_url: str = "https://gateway.example/v1") -> OpenAIProvider:
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.base_url = base_url
    provider.max_retries = 3
    provider.retry_delay = 0.0
    return provider


def test_generate_json_caches_response_format_incompatibility(monkeypatch):
    OpenAIProvider._json_response_format_support_cache.clear()
    provider = _make_provider()
    calls: list[dict] = []

    def fake_call_with_retry(model: str, messages: list[dict], **kwargs):
        calls.append(kwargs.copy())
        if len(calls) == 1:
            raise RuntimeError("400 unsupported parameter: response_format json_object")
        return '{"status": "ok"}'

    monkeypatch.setattr(provider, "_call_with_retry", fake_call_with_retry)

    first = provider.generate_json("demo-model", "first prompt")
    second = provider.generate_json("demo-model", "second prompt")

    assert first == {"status": "ok"}
    assert second == {"status": "ok"}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    assert "response_format" not in calls[2]


def test_generate_json_keeps_using_response_format_when_supported(monkeypatch):
    OpenAIProvider._json_response_format_support_cache.clear()
    provider = _make_provider()
    calls: list[dict] = []

    def fake_call_with_retry(model: str, messages: list[dict], **kwargs):
        calls.append(kwargs.copy())
        return '{"status": "ok"}'

    monkeypatch.setattr(provider, "_call_with_retry", fake_call_with_retry)

    first = provider.generate_json("demo-model", "first prompt")
    second = provider.generate_json("demo-model", "second prompt")

    assert first == {"status": "ok"}
    assert second == {"status": "ok"}
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[1]["response_format"] == {"type": "json_object"}
