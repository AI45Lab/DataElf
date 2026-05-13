import json

from llm.provider import LLMProvider
from llm.tracing import LLMTraceRecorder, TracingLLMProvider, llm_trace_context


class FakeMessageProvider(LLMProvider):
    def generate_from_messages(self, model: str, messages: list[dict], **kwargs):
        if kwargs.get("response_format"):
            return '{"action_type": "mutate_pipeline"}'
        return "plain response"

    def load_json_content(self, content: str):
        return json.loads(content)

    def generate(self, model: str, prompt: str, system_prompt: str | None = None, **kwargs):
        return "plain response"

    def generate_json(self, model: str, prompt: str, **kwargs):
        return {"action_type": "mutate_pipeline"}


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_tracing_provider_flushes_core_call_with_full_messages(tmp_path):
    recorder = LLMTraceRecorder(env_id="test-security", output_dir=tmp_path)
    provider = TracingLLMProvider(FakeMessageProvider(), recorder)

    with llm_trace_context(
        job_id="job_trace",
        mode="pilot",
        attempt_id="attempt_02",
        scope="core",
        caller="planner",
    ):
        parsed = provider.generate_json("demo-model", "full planner prompt")

    assert parsed == {"action_type": "mutate_pipeline"}
    path = recorder.finalize_job("job_trace")
    rows = _read_jsonl(path)

    assert len(rows) == 1
    row = rows[0]
    meta = json.loads(row["meta_json"])
    assert row["dataset_type"] == "dataelf_llm_call_core"
    assert row["env_id"] == "test-security"
    assert row["is_terminal"] is True
    assert row["is_session_completed"] is True
    assert row["messages"][1]["content"][0]["text"] == "full planner prompt"
    assert row["response"]["content"][0]["text"] == '{"action_type": "mutate_pipeline"}'
    assert meta["caller"] == "planner"
    assert meta["parsed_response"] == {"action_type": "mutate_pipeline"}


def test_tracing_provider_records_tool_metadata(tmp_path):
    recorder = LLMTraceRecorder(env_id="tool-profile", output_dir=tmp_path)
    provider = TracingLLMProvider(FakeMessageProvider(), recorder)

    with llm_trace_context(
        job_id="job_tool",
        mode="run",
        scope="tool",
        caller="security_audit.HarmfulContentLLMJudge",
        tool_name="security_audit",
        tool_component="HarmfulContentLLMJudge",
        tool_call_context={"risk_type": "harmful", "sample_id": "sample_001"},
    ):
        response = provider.generate("demo-model", "full tool prompt")

    assert response == "plain response"
    path = recorder.finalize_job("job_tool")
    row = _read_jsonl(path)[0]
    meta = json.loads(row["meta_json"])

    assert row["dataset_type"] == "dataelf_llm_call_tool"
    assert row["env_name"] == "dataelf/tool"
    assert row["messages"][0]["content"][0]["text"] == "full tool prompt"
    assert meta["scope"] == "tool"
    assert meta["tool_name"] == "security_audit"
    assert meta["tool_component"] == "HarmfulContentLLMJudge"
    assert meta["tool_call_context"] == {"risk_type": "harmful", "sample_id": "sample_001"}
