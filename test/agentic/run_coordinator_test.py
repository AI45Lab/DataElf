from config import Config
from runtime import JobManager
from tools import BaseTool, ToolContext
from tools.tool_registry import ToolRegistry

from agentic.controller import (
    RunCoordinator,
    _build_security_audit_hints,
    _extract_external_write_targets,
    _normalize_missing_items,
    _should_force_programmatic_missing_slot_followup,
    _should_trust_semantic_user_reply_for_missing_items,
    _visible_tool_schemas,
)

DEFAULT_SECURITY_BASELINE = [
    "PIIRule",
    "SecretRule",
    "ToxicityKeywordRule",
    "HarmfulKeywordRule",
]


class DummySecurityAuditTool(BaseTool):
    @property
    def name(self) -> str:
        return "security_audit"

    @property
    def description(self) -> str:
        return "Run a security audit with configurable checker_names."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "data": {"type": "array", "items": {"type": "object"}},
                "checker_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["PIIRule", "SecretRule"],
                },
            },
            "required": ["data"],
        }

    def usage_example(self) -> str:
        return 'run_tool("security_audit", data=data, checker_names=["PIIRule"])'

    def run(self, context: ToolContext, **kwargs):
        return {"result": {"ok": True}}


class DummyProfileTool(BaseTool):
    @property
    def name(self) -> str:
        return "profile_tool"

    @property
    def description(self) -> str:
        return "Run profile_tool on a dataset with a required profile_name."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "data": {"type": "array", "items": {"type": "object"}},
                "profile_name": {
                    "type": "string",
                    "enum": ["balanced", "strict"],
                    "default": "balanced",
                    "description": "The profile name to apply.",
                },
            },
            "required": ["data", "profile_name"],
        }

    def usage_example(self) -> str:
        return 'run_tool("profile_tool", data=data, profile_name="balanced")'

    def run(self, context: ToolContext, **kwargs):
        return {"result": {"ok": True}}


class DummyFilterExportTool(BaseTool):
    @property
    def name(self) -> str:
        return "filter_export"

    @property
    def description(self) -> str:
        return "Filter a dataset by field and value, then export it."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "data": {"type": "array", "items": {"type": "object"}},
                "filter_field": {
                    "type": "string",
                    "description": "Dataset field name used for filtering.",
                },
                "filter_value": {
                    "type": "string",
                    "description": "Value to match in the selected field.",
                },
                "output_format": {
                    "type": "string",
                    "enum": ["json", "csv"],
                    "default": "json",
                },
            },
            "required": ["data", "filter_field", "filter_value"],
        }

    def usage_example(self) -> str:
        return 'run_tool("filter_export", data=data, filter_field="dataset_type", filter_value="rl")'

    def run(self, context: ToolContext, **kwargs):
        return {"result": {"ok": True}}


class FakeExecutor:
    def __init__(self, success: bool = True):
        self.success = success
        self.calls = 0

    def execute(self, job_id: str, pipeline: str):
        self.calls += 1
        return {
            "success": self.success,
            "result": {"ok": self.success},
            "artifacts": {},
            "metadata": {},
            "error": None if self.success else "execution failed",
        }


class FakeAgent:
    def generate_pipeline(self, task: str):
        return 'save_result({"task": "ok"})', {
            "model": "fake-model",
            "elapsed_seconds": 0.01,
            "raw_response": 'save_result({"task": "ok"})',
        }


class FakeWriteAgent:
    def generate_pipeline(self, task: str):
        pipeline = '''data = load_dataset("companies")
write_file(data, "test_data/companies_export.jsonl")
save_result({"output_file": "test_data/companies_export.jsonl"})'''
        return pipeline, {
            "model": "fake-model",
            "elapsed_seconds": 0.01,
            "raw_response": pipeline,
        }


class FakeSecurityAuditAgent:
    def generate_pipeline(self, task: str):
        pipeline = '''log_step("Loading security audit samples")

data = load_dataset("security_audit_samples")

result = run_tool(
    "security_audit",
    data=data,
    checker_names=["PIIRule", "ToxicityKeywordRule", "BiasKeywordRule"]
)

log_step(f"Audit completed with {len(result['result'])} records")

save_result(result)'''
        return pipeline, {
            "model": "fake-model",
            "elapsed_seconds": 0.01,
            "raw_response": pipeline,
        }


class FakeLLMProvider:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def generate_json(self, model: str, prompt: str, **kwargs):
        if not self.decisions:
            raise AssertionError("No more fake clarification decisions available")
        return self.decisions.pop(0)


class RaisingLLMProvider:
    def generate_json(self, model: str, prompt: str, **kwargs):
        raise RuntimeError("gateway timeout")


def test_run_coordinator_uses_defaults_and_records_clarification(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Available checkers include PIIRule, SecretRule, and HarmfulContentLLMJudge. Use defaults or provide a custom checker list?",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": ["PIIRule", "SecretRule"]},
            "response_mode": "answer_then_ask",
        }
    ])

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: "use defaults")

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="execute security audit",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    job = job_manager.get_job(response["job_id"])
    assert response["status"] == "completed"
    assert response["clarification"]["status"] == "ready"
    assert response["clarification"]["clarification_turns"] == 1
    assert response["clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE
    assert executor.calls == 1
    assert job is not None
    assert job.clarification_status == "ready"
    assert job.clarification_turns == 1


def test_extract_external_write_targets_ignores_internal_paths():
    pipeline = '''
write_file(data, ".elf/tmp/output.json")
write_file(data, ".logs/job.json")
write_file(data, "test_data/export.jsonl")
'''
    assert _extract_external_write_targets(pipeline) == ["test_data/export.jsonl"]


def test_run_coordinator_requires_write_approval_for_external_file(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeWriteAgent())

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=None,
    )

    response = coordinator.execute(
        task="export companies to a new file",
        dataset_schemas={"companies": ["id", "name"]},
        ask_user=False,
        verbose=False,
    )

    assert response["status"] == "failed"
    assert response["capability_gap"]["type"] == "write_approval_required"
    assert response["capability_gap"]["requested_paths"] == ["test_data/companies_export.jsonl"]
    assert executor.calls == 0


def test_run_coordinator_schema_required_slot_uses_default_policy(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummyProfileTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "run profile_tool on companies",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        }
    ])

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: "use defaults")

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run profile_tool on companies",
        dataset_schemas={"companies": ["id", "name"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 1
    assert response["clarification"]["resolved_slots"]["dataset_name"] == "companies"
    assert response["clarification"]["resolved_slots"]["profile_name"] == "balanced"
    assert response["clarification"]["resolved_slots"]["selection_mode"] == "defaults"


def test_run_coordinator_extracts_dataset_field_and_value_from_natural_language(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummyFilterExportTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Please specify filter_field and filter_value.",
            "ready_to_execute": False,
            "resolved_task": "run filter_export on events_dataset",
            "resolved_slots": {},
            "missing_items": ["filter_field", "filter_value"],
            "suggested_defaults": {"output_format": "json"},
            "response_mode": "ask_user",
        },
    ])

    replies = iter([
        "过滤条件是dataset type. 输出格式json",
        "对dataset type为rl的过滤",
    ])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run filter_export on events_dataset",
        dataset_schemas={"events_dataset": ["id", "dataset_type", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 2
    assert response["clarification"]["resolved_slots"]["dataset_name"] == "events_dataset"
    assert response["clarification"]["resolved_slots"]["filter_field"] == "dataset_type"
    assert response["clarification"]["resolved_slots"]["filter_value"] == "rl"


def test_should_trust_semantic_user_reply_after_repeated_unresolved_slot():
    assert _should_trust_semantic_user_reply_for_missing_items(
        user_reply="对dataset type为rl的过滤",
        unresolved_missing_items=["filter_field"],
        retry_counts={"filter_field": 2},
    ) is True
    assert _should_trust_semantic_user_reply_for_missing_items(
        user_reply="随便",
        unresolved_missing_items=["filter_field"],
        retry_counts={"filter_field": 2},
    ) is False


def test_normalize_missing_items_maps_internal_filterish_slot_names():
    assert _normalize_missing_items(
        "把数据过滤后写入新文件",
        ["flag_field_name", "flag_value", "file format", "output file"],
    ) == ["filter_field", "filter_value", "output_format", "output_filename"]


def test_unknown_missing_items_do_not_force_programmatic_followup():
    assert _should_force_programmatic_missing_slot_followup(
        ["mystery_slot_name"],
        {},
    ) is False
    assert _should_force_programmatic_missing_slot_followup(
        ["filter_field", "filter_value"],
        {},
    ) is True


def test_run_clarification_fallback_still_guards_unknown_dataset_field(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)

    replies = iter([
        "security_audit_samples",
        "dataset type为rl",
    ])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=RaisingLLMProvider(),
    )

    response = coordinator.maybe_request_clarification(
        task="把security_audit_sample里的is_flag为true的数据抽取出来告诉我总共多少条,然后写入新文件里",
        dataset_schemas={"security_audit_samples": ["id", "dataset_type", "messages", "response"]},
        ask_user=True,
    )

    assert response["status"] == "ready"
    assert response["clarification_turns"] == 2
    transcript = response["clarification_transcript"]
    assert "does not have a field/column named `is_flag`" in transcript[1]["assistant_message"]
    assert response["resolved_slots"]["dataset_name"] == "security_audit_samples"


def test_run_coordinator_tolerates_null_assistant_message_from_llm(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": None,
            "ready_to_execute": False,
            "resolved_task": "execute security audit job",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": DEFAULT_SECURITY_BASELINE},
            "response_mode": None,
        }
    ])

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: "use defaults")

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="execute security audit job",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 1
    assert response["clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE


def test_run_coordinator_answers_options_then_executes(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Which security_audit checker_names should I use?",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": ["PIIRule", "SecretRule"]},
            "response_mode": "ask_user",
        },
        {
            "status": "clarifying",
            "assistant_message": "Available checker choices: PIIRule, SecretRule, HarmfulContentLLMJudge, ToxicityLLMJudge, PIILLMJudge. Recommended defaults: PIIRule, SecretRule. Which ones should I use?",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": ["PIIRule", "SecretRule"]},
            "response_mode": "answer_then_ask",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "Use HarmfulContentLLMJudge, ToxicityLLMJudge, PIILLMJudge on security_audit_samples.",
            "resolved_slots": {"checker_names": ["HarmfulContentLLMJudge", "ToxicityLLMJudge", "PIILLMJudge"]},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter([
        "what choices are available?",
        "use balanced recommendation",
        "HarmfulContentLLMJudge, ToxicityLLMJudge, PIILLMJudge",
    ])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run HarmfulContentLLMJudge against security_audit_samples",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 3
    assert "Balanced recommendation" in response["clarification"]["clarification_transcript"][1]["assistant_message"]
    assert response["clarification"]["resolved_slots"]["checker_names"] == [
        "HarmfulContentLLMJudge",
        "ToxicityLLMJudge",
        "PIILLMJudge",
    ]
    assert executor.calls == 1


def test_run_coordinator_answers_dataset_options_and_accepts_close_dataset_name(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Please specify the dataset to audit by providing its records.",
            "ready_to_execute": False,
            "resolved_task": "run security audit",
            "resolved_slots": {},
            "missing_items": ["data"],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["what datasets are available?", "security_audit_sample"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security audit",
        dataset_schemas={
            "security_audit_samples": ["id", "messages"],
            "companies": ["id", "name"],
        },
        ask_user=True,
        verbose=False,
    )

    transcript = response["clarification"]["clarification_transcript"]
    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 2
    assert transcript[0]["missing_items"] == ["dataset_name"]
    assert transcript[1]["llm"]["status"] == "programmatic_followup"
    assert "Available datasets" in transcript[1]["assistant_message"]
    assert response["clarification"]["resolved_slots"]["dataset_name"] == "security_audit_samples"
    assert "security_audit_samples" in response["clarification"]["resolved_task"]
    assert executor.calls == 1


def test_run_coordinator_accepts_dataset_filename_suffix(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Which data should I use?",
            "ready_to_execute": False,
            "resolved_task": "run security audit",
            "resolved_slots": {},
            "missing_items": ["data"],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: "security_audit_samples.json")

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security audit",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["resolved_slots"]["dataset_name"] == "security_audit_samples"
    assert executor.calls == 1


def test_run_coordinator_ignores_optional_execution_knobs(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Please specify checker_names and max_workers.",
            "ready_to_execute": False,
            "resolved_task": "run security audit",
            "resolved_slots": {},
            "missing_items": ["checker_names", "max_workers"],
            "suggested_defaults": {"checker_names": DEFAULT_SECURITY_BASELINE, "max_workers": 4},
            "response_mode": "ask_user",
        },
    ])

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: "ToxicityKeywordRule")

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security audit with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["resolved_slots"]["checker_names"] == ["ToxicityKeywordRule"]
    assert "max_workers" not in response["clarification"]["resolved_slots"]
    assert executor.calls == 1


def test_run_coordinator_skips_followup_that_only_asks_max_workers(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "What max_workers should I use?",
            "ready_to_execute": False,
            "resolved_task": "run security audit on security_audit_samples with ToxicityKeywordRule",
            "resolved_slots": {"dataset_name": "security_audit_samples", "checker_names": ["ToxicityKeywordRule"]},
            "missing_items": ["max_workers"],
            "suggested_defaults": {"max_workers": 4},
            "response_mode": "ask_user",
        },
    ])

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: (_ for _ in ()).throw(AssertionError("should not prompt user")))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security audit on security_audit_samples with ToxicityKeywordRule",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 0
    assert executor.calls == 1


def test_run_coordinator_escalates_to_pilot_after_five_turns(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Please clarify the exact checker set.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "clarifying",
            "assistant_message": "I still need the exact checker set.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "clarifying",
            "assistant_message": "One more time: specify the checker set or switch to pilot mode.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "clarifying",
            "assistant_message": "I still need checker_names to continue.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "clarifying",
            "assistant_message": "Last clarification turn: give checker_names or switch to pilot mode.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["not sure", "still not sure", "you decide", "no idea", "whatever"])
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="security task needing too much clarification",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    job = job_manager.get_job(response["job_id"])
    assert response["status"] == "needs_pilot"
    assert response["clarification"]["status"] == "escalate_to_pilot"
    assert response["clarification"]["clarification_turns"] == 5
    assert response["capability_gap"]["recommended_command"] == "elf pilot"
    assert executor.calls == 0


def test_security_audit_hints_include_only_runtime_available_checkers(tmp_path):
    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())

    hints = _build_security_audit_hints("run security audit on security_audit_samples", registry)

    assert hints is not None
    assert "JailbreakLLMJudge" not in hints["checker_names_available"]
    assert "PromptInjectionClassifier" not in hints["checker_names_available"]
    assert "HarmfulContentLLMJudge" in hints["llm_required_checkers"]
    assert "PIIRule" in hints["rule_based_checkers"]
    assert "tool_readme_excerpt" not in hints


def test_visible_tool_schemas_follow_configured_tools(tmp_path):
    cfg = Config()
    cfg.tools = ["security_audit"]

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    registry.register(DummyProfileTool())

    schemas = _visible_tool_schemas(cfg, registry)

    assert [schema["name"] for schema in schemas] == ["security_audit"]


def test_run_coordinator_does_not_accept_ready_when_missing_checker_names(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Please specify the custom checker names you would like to use for the security audit.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": ["PIIRule", "SecretRule"]},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "clarifying",
            "assistant_message": "Use defaults or provide exact checker names.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": ["PIIRule", "SecretRule"]},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["not sure", "use defaults"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 2
    assert response["clarification"]["clarification_transcript"][0]["guard_forced_continue"] is True
    assert response["clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE
    assert executor.calls == 1


def test_run_coordinator_parses_checker_names_from_user_reply(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Which checker_names should I use?",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": ["PIIRule", "SecretRule"]},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: "HarmfulContentLLMJudge, ToxicityLLMJudge, PIILLMJudge")

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["resolved_slots"]["checker_names"] == [
        "HarmfulContentLLMJudge",
        "ToxicityLLMJudge",
        "PIILLMJudge",
    ]
    assert executor.calls == 1


def test_run_coordinator_recommends_balanced_checker_set_for_cost_and_speed(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Please specify which checker_names you want to use.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": DEFAULT_SECURITY_BASELINE},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["what checker set can balance cost and speed?", "use defaults"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set; I care about cost and speed",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    transcript = response["clarification"]["clarification_transcript"]
    assert response["status"] == "completed"
    assert "cost/speed balance" in transcript[1]["assistant_message"]
    assert "HarmfulContentLLMJudge" in transcript[1]["assistant_message"]
    assert response["clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE


def test_run_coordinator_accepts_balanced_recommendation_reply(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Please specify which checker_names you want to use.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": DEFAULT_SECURITY_BASELINE},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["what checker set can balance cost and speed?", "use balanced recommendation"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set; I care about cost and speed",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["resolved_slots"]["checker_names"] == [
        "PIIRule",
        "SecretRule",
        "ToxicityKeywordRule",
        "HarmfulKeywordRule",
        "HarmfulContentLLMJudge",
    ]
    assert response["clarification"]["resolved_slots"]["selection_mode"] == "balanced_recommendation"


def test_run_coordinator_programmatically_follows_up_on_accuracy_question(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "clarifying",
            "assistant_message": "Please specify which checker_names you want to use.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": DEFAULT_SECURITY_BASELINE},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {"checker_names": ["PIIRule"]},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["what about accuracy?", "use stronger recommendation"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    transcript = response["clarification"]["clarification_transcript"]
    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 2
    assert transcript[1]["llm"]["status"] == "programmatic_followup"
    assert "accuracy and semantic coverage" in transcript[1]["assistant_message"]
    assert response["clarification"]["resolved_slots"]["selection_mode"] == "stronger_recommendation"


def test_run_coordinator_stabilizes_security_audit_result_access(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([])

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeSecurityAuditAgent())

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security audit on security_audit_samples",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=False,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert "result['result']" not in response["pipeline"]
    assert "flagged=" not in response["pipeline"]
    assert 'log_step("Completed tool: security_audit")' in response["pipeline"]


def test_run_coordinator_forces_question_when_task_says_custom_checker_set(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["use defaults"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 1
    assert response["clarification"]["clarification_transcript"][0]["assistant_message"] == (
        "Please specify the custom checker_names you would like to use for the security audit."
    )
    assert response["clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE
    assert executor.calls == 1


def test_run_coordinator_does_not_accept_model_invented_checker_after_weak_reply(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {"checker_names": ["HarmfulContentLLMJudge"]},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "clarifying",
            "assistant_message": "I still need exact checker_names or you can say use defaults.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["checker_names"],
            "suggested_defaults": {"checker_names": ["PIIRule", "SecretRule"]},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["not sure", "use defaults"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 2
    assert response["clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE
    assert response["clarification"]["clarification_transcript"][0]["weak_reply"] is True
    assert executor.calls == 1


def test_run_coordinator_answers_recommendation_question_before_executing(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {"checker_names": ["HarmfulContentLLMJudge"]},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["not sure", "do you have recommendations?", "use defaults"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    transcript = response["clarification"]["clarification_transcript"]
    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 3
    assert transcript[1]["option_request"] is True
    assert "Balanced recommendation" in transcript[2]["assistant_message"]
    assert "use balanced recommendation" in transcript[2]["assistant_message"]
    assert response["clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE
    assert executor.calls == 1


def test_run_coordinator_normalizes_checker_set_missing_item_for_recommendations(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "clarifying",
            "assistant_message": "I still need: custom checker set.",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": ["custom checker set"],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["i don't know", "do you have any recommendations?", "use defaults"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    transcript = response["clarification"]["clarification_transcript"]
    assert response["status"] == "completed"
    assert transcript[1]["missing_items"] == ["checker_names"]
    assert "Balanced recommendation" in transcript[2]["assistant_message"]
    assert "use balanced recommendation" in transcript[2]["assistant_message"]
    assert response["clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE


def test_run_coordinator_does_not_escalate_to_pilot_too_early_for_checker_selection(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)
    llm = FakeLLMProvider([
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
        {
            "status": "escalate_to_pilot",
            "assistant_message": "",
            "ready_to_execute": False,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
            "handoff_reason": "User requires assistance in selecting appropriate checker options for the security audit.",
        },
        {
            "status": "ready",
            "assistant_message": "",
            "ready_to_execute": True,
            "resolved_task": "security task",
            "resolved_slots": {},
            "missing_items": [],
            "suggested_defaults": {},
            "response_mode": "ask_user",
        },
    ])

    replies = iter(["i have no idea", "use defaults"])
    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())
    monkeypatch.setattr("builtins.input", lambda: next(replies))

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=llm,
    )

    response = coordinator.execute(
        task="run security_audit on security_audit_samples with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=True,
        verbose=False,
    )

    transcript = response["clarification"]["clarification_transcript"]
    assert response["status"] == "completed"
    assert response["clarification"]["clarification_turns"] == 2
    assert transcript[1]["assistant_message"].startswith("I still need the exact checker_names")
    assert response["clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE


def test_run_coordinator_emits_stage_events(monkeypatch, tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = ToolRegistry()
    registry.register(DummySecurityAuditTool())
    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    executor = FakeExecutor(success=True)

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeAgent())

    coordinator = RunCoordinator(
        config=cfg,
        job_manager=job_manager,
        executor=executor,
        registry=registry,
        llm_provider=None,
    )

    events: list[dict] = []
    response = coordinator.execute(
        task="run security audit",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        ask_user=False,
        verbose=False,
        event_handler=events.append,
    )

    started_stages = [event["stage"] for event in events if event.get("type") == "stage_started"]
    completed_stages = [event["stage"] for event in events if event.get("type") == "stage_completed"]

    assert response["status"] == "completed"
    assert started_stages == ["clarification", "pipeline_generation", "execution"]
    assert completed_stages == ["clarification", "pipeline_generation", "execution"]
    assert any(
        event.get("type") == "stage_completed"
        and event.get("stage") == "execution"
        and event.get("success") is True
        for event in events
    )
