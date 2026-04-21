from config import Config
from runtime import JobManager
from tools import BaseTool, ToolContext
from tools.tool_registry import get_global_registry

from agentic import AssetManager, PilotController

DEFAULT_SECURITY_BASELINE = [
    "PIIRule",
    "SecretRule",
    "ToxicityKeywordRule",
    "HarmfulKeywordRule",
]

DEFAULT_SECURITY_RISK_BASELINE = [
    "pii",
    "secret",
    "toxicity",
    "harmful",
]


class DummyAuditTool(BaseTool):
    @property
    def name(self) -> str:
        return "security_audit"

    @property
    def description(self) -> str:
        return "Dummy security audit tool."

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
                "max_workers": {"type": "integer"},
            },
            "required": ["data"],
        }

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


class FakeExecutor:
    def __init__(self):
        self.calls = 0

    def execute(self, job_id: str, pipeline: str):
        self.calls += 1
        return {
            "success": False,
            "result": None,
            "artifacts": {},
            "metadata": {},
            "error": f"attempt {self.calls} failed",
        }


class FakeSuccessExecutor:
    def execute(self, job_id: str, pipeline: str):
        return {
            "success": True,
            "result": {"ok": True},
            "artifacts": {},
            "metadata": {},
            "error": None,
        }


class FakeLLMProvider:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def generate_json(self, model: str, prompt: str, **kwargs):
        if not self.decisions:
            raise AssertionError("No more fake LLM decisions available")
        return self.decisions.pop(0)


class CapturingLLMProvider(FakeLLMProvider):
    def __init__(self, decisions):
        super().__init__(decisions)
        self.prompts: list[str] = []

    def generate_json(self, model: str, prompt: str, **kwargs):
        self.prompts.append(prompt)
        return super().generate_json(model, prompt, **kwargs)


class FakeNarrowSecurityAgent:
    def generate_pipeline(self, task: str):
        return (
            'data = load_dataset("security_audit_samples")\n'
            'result = run_tool("security_audit", data=data, checker_names=["SecretRule"])\n'
            "save_result(result)",
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


class FakeBroadPoorSecurityAgent:
    def generate_pipeline(self, task: str):
        return (
            'data = load_dataset("security_audit_samples")\n'
            'result = run_tool("security_audit", data=data, checker_names=["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule"])\n'
            "save_result(result)",
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


class FakeRiskCoverageSecurityAgent:
    def generate_pipeline(self, task: str):
        return (
            'data = load_dataset("security_audit_samples")\n'
            'result = run_tool("security_audit", data=data, checker_names=["PIILLMJudge", "SecretRule", "ToxicityClassifier", "HarmfulContentLLMJudge"])\n'
            "save_result(result)",
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


class FakeUnsafeLLMCheckerAgent:
    def generate_pipeline(self, task: str):
        return (
            'data = load_dataset("security_audit_samples")\n'
            'result = run_tool(\n'
            '    "security_audit",\n'
            '    data=data,\n'
            '    checker_names=["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule", "HarmfulContentLLMJudge"]\n'
            ')\n'
            "save_result(result)",
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


class FakeWriteFileAgent:
    def generate_pipeline(self, task: str):
        return (
            'data = load_dataset("companies")\n'
            'write_file(data, "test_data/companies_export.jsonl")\n'
            'save_result({"output_file": "test_data/companies_export.jsonl"})',
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


class FakeSimpleSuccessAgent:
    def generate_pipeline(self, task: str):
        return (
            'save_result({"ok": True})',
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


class CapturingTaskAgent:
    def __init__(self):
        self.tasks: list[str] = []

    def generate_pipeline(self, task: str):
        self.tasks.append(task)
        return (
            'save_result({"ok": True})',
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


class SequencedTaskAgent:
    def __init__(self, pipelines: list[str]):
        self.tasks: list[str] = []
        self.pipelines = list(pipelines)

    def generate_pipeline(self, task: str):
        self.tasks.append(task)
        pipeline = self.pipelines.pop(0)
        return (
            pipeline,
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


class FakeSecurityResultExecutor:
    def __init__(self, security_score: int, flagged_rate: float, passed: bool, flagged_samples: int = 0, total_samples: int = 16):
        self.security_score = security_score
        self.flagged_rate = flagged_rate
        self.passed = passed
        self.flagged_samples = flagged_samples
        self.total_samples = total_samples

    def execute(self, job_id: str, pipeline: str):
        safe_samples = max(0, self.total_samples - self.flagged_samples)
        return {
            "success": True,
            "result": {
                "security_score": self.security_score,
                "flagged_rate": self.flagged_rate,
                "flagged_samples": self.flagged_samples,
                "total_samples": self.total_samples,
                "safe_samples": safe_samples,
                "passed": self.passed,
            },
            "artifacts": {},
            "metadata": {},
            "error": None,
        }


class FakeContentFilterExecutor:
    def execute(self, job_id: str, pipeline: str):
        return {
            "success": True,
            "result": {
                "security_score": 53,
                "flagged_rate": 1.0,
                "flagged_samples": 16,
                "total_samples": 16,
                "safe_samples": 0,
                "passed": False,
            },
            "artifacts": {
                "security_audit.sample_results": [
                    {
                        "sample_id": "sample_1",
                        "results": [
                            {
                                "checker_name": "HarmfulContentLLMJudge",
                                "risk_type": "harmful",
                                "score": 1.0,
                                "flagged": True,
                                "details": {
                                    "error": "LLM API error: content_filter"
                                },
                                "evidence": "The response was filtered due to the prompt triggering content management policy.",
                            }
                        ],
                    }
                ]
            },
            "metadata": {},
            "error": None,
        }


class FakeCheckerExecutionErrorExecutor:
    def execute(self, job_id: str, pipeline: str):
        return {
            "success": True,
            "result": {
                "security_score": 62,
                "flagged_rate": 0.375,
                "flagged_samples": 6,
                "total_samples": 16,
                "safe_samples": 10,
                "passed": False,
            },
            "artifacts": {
                "security_audit.sample_results": [
                    {
                        "sample_id": "sample_1",
                        "results": [
                            {
                                "checker_name": "HarmfulContentLLMJudge",
                                "risk_type": "harmful",
                                "score": 1.0,
                                "flagged": True,
                                "details": {
                                    "error": "LLM API error (status 502): Error code: 502",
                                    "checker_execution_error": True,
                                },
                                "evidence": "Checker execution failed during analysis: LLM API error (status 502): Error code: 502",
                            }
                        ],
                    }
                ]
            },
            "metadata": {},
            "error": None,
        }


class FakeCompaniesAgent:
    def generate_pipeline(self, task: str):
        return (
            'data = load_dataset("companies")\n'
            'result = run_tool("security_audit", data=data)\n'
            "save_result(result)",
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


class FakeProfileToolAgent:
    def generate_pipeline(self, task: str):
        return (
            'data = load_dataset("companies")\n'
            'result = run_tool("profile_tool", data=data, profile_name="strict")\n'
            "save_result(result)",
            {
                "model": "fake-model",
                "elapsed_seconds": 0.01,
                "raw_response": "",
            },
        )


def test_pilot_controller_generates_candidate_when_failures_persist(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "mock"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    response = controller.execute(
        task="screen for high value and low privacy risk data",
        dataset_schemas={"companies": ["id", "name", "value"]},
        budget_steps=2,
        allow_experimental_tools=False,
    )

    job = job_manager.get_job(response["job_id"])
    candidates = asset_manager.list_candidates()

    assert response["status"] == "budget_exhausted"
    assert len(response["attempts"]) == 2
    assert len(candidates) == 1
    assert candidates[0]["candidate_type"] == "composite_tool"
    assert candidates[0]["validation_status"] == "smoke_passed"
    assert candidates[0]["status"] == "awaiting_approval"
    assert job is not None
    assert job.attempt_count == 2
    assert job.approval_state == "pending_review"
    assert "pilot_summary" in response


def test_pilot_controller_success_creates_pipeline_candidate_not_stable_asset(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "mock"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    job = job_manager.create_job("run security_audit on security_audit_samples with a custom checker set", mode="pilot")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    response = controller.execute(
        task="do security check against sample data",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        budget_steps=1,
        allow_experimental_tools=False,
    )

    assert response["status"] == "success"
    assert response["pipeline_candidate_id"].startswith("cand_pipe_")
    candidate = asset_manager.get_candidate(response["pipeline_candidate_id"])
    assert candidate is not None
    assert candidate["validation_status"] == "smoke_passed"
    assert candidate["status"] == "awaiting_approval"
    assert asset_manager.get_stable_asset(response["pipeline_candidate_id"]) is None


def test_pilot_controller_inline_approval_promotes_pipeline_asset(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "mock"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    job = job_manager.create_job("run security_audit on security_audit_samples with a custom checker set", mode="pilot")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    response = controller.execute(
        task="do security check against sample data",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        budget_steps=1,
        allow_experimental_tools=False,
        ask_user=True,
        checkpoint_handler=lambda checkpoint: {"decision": "approve"} if checkpoint["checkpoint_type"] == "candidate_approval" else {"decision": "continue", "answer": "use defaults"},
    )

    candidate = asset_manager.get_candidate(response["pipeline_candidate_id"])
    assert response["status"] == "success"
    assert response["approved_asset_ids"]
    assert candidate is not None
    assert candidate["status"] == "approved"
    assert response["attempts"][0]["candidate"]["status"] == "approved"
    assert asset_manager.get_stable_asset(response["approved_asset_ids"][0]) is not None


def test_pilot_controller_clarifies_security_checker_before_attempts(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    job = job_manager.create_job("run security_audit on security_audit_samples with a custom checker set", mode="pilot")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "status": "clarifying",
                "assistant_message": "What dataset should I run the security audit on?",
                "ready_to_execute": False,
                "resolved_task": "run security audit",
                "resolved_slots": {},
                "missing_items": ["dataset_name"],
                "suggested_defaults": {},
            },
        ]),
    )

    seen_prompts: list[str] = []

    def checkpoint_handler(checkpoint):
        prompt = checkpoint["payload"].get("prompt", "")
        seen_prompts.append(prompt)
        if "dataset" in prompt.lower():
            return {"decision": "answer", "answer": "security_audit_samples"}
        if "checker" in prompt.lower():
            return {"decision": "answer", "answer": "use defaults"}
        return {"decision": "continue"}

    response = controller.execute(
        task="run security audit",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        budget_steps=1,
        allow_experimental_tools=False,
        ask_user=True,
        checkpoint_handler=checkpoint_handler,
    )

    assert response["status"] == "success"
    assert any("dataset" in prompt.lower() for prompt in seen_prompts)
    assert any("checker" in prompt.lower() for prompt in seen_prompts)
    assert response["goal_clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE


def test_pilot_controller_goal_clarification_can_recommend_stronger_security_checkers(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    job = job_manager.create_job("run security_audit on security_audit_samples with a custom checker set", mode="pilot")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "status": "clarifying",
                "assistant_message": "Do you have preferred checker options or should I suggest one?",
                "ready_to_execute": False,
                "resolved_task": "run security_audit on security_audit_samples with a custom checker set",
                "resolved_slots": {},
                "missing_items": ["checker_names"],
                "suggested_defaults": {},
            },
        ]),
    )

    prompts: list[str] = []

    def checkpoint_handler(checkpoint):
        prompt = checkpoint["payload"].get("prompt", "")
        prompts.append(prompt)
        if "preferred checker options" in prompt.lower():
            return {"decision": "answer", "answer": "what about accuracy?"}
        return {"decision": "answer", "answer": "use stronger recommendation"}

    clarification = controller._maybe_request_goal_clarification(
        job_id=job.job_id,
        task="run security_audit on security_audit_samples with a custom checker set",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        allow_experimental_tools=False,
        ask_user=True,
        checkpoint_handler=checkpoint_handler,
        event_handler=None,
    )

    assert clarification["status"] == "resolved"
    assert prompts[-1].lower().startswith("if accuracy and semantic coverage matter more")
    assert clarification["resolved_slots"]["checker_names"][:3] == [
        "HarmfulContentLLMJudge",
        "ToxicityLLMJudge",
        "PIILLMJudge",
    ]


def test_pilot_controller_security_checker_clarification_accepts_balanced_recommendation(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    job = job_manager.create_job("run security_audit on security_audit_samples with a custom checker set", mode="pilot")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    prompts: list[str] = []

    def checkpoint_handler(checkpoint):
        prompt = checkpoint["payload"].get("prompt", "")
        prompts.append(prompt)
        if "please specify which security_audit checker_names" in prompt.lower():
            return {"decision": "answer", "answer": "what checker set can balance cost and speed?"}
        return {"decision": "answer", "answer": "use balanced recommendation"}

    clarification = controller._maybe_request_security_checker_clarification(
        job_id=job.job_id,
        task="run security_audit on security_audit_samples with a custom checker set",
        current_task="run security_audit on security_audit_samples with a custom checker set",
        resolved_slots={},
        ask_user=True,
        checkpoint_handler=checkpoint_handler,
        event_handler=None,
    )

    assert clarification["status"] == "resolved"
    assert "balanced recommendation" in prompts[-1].lower()
    assert clarification["resolved_slots"]["checker_names"] == [
        "PIIRule",
        "SecretRule",
        "ToxicityKeywordRule",
        "HarmfulKeywordRule",
        "HarmfulContentLLMJudge",
    ]


def test_pilot_controller_goal_clarification_does_not_treat_suggested_checker_defaults_as_resolved(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "status": "clarifying",
                "assistant_message": (
                    "Which dataset would you like to audit? "
                    "I'll default to security_audit_samples with the HarmfulContentLLMJudge checker unless you specify otherwise."
                ),
                "ready_to_execute": False,
                "resolved_task": "run security audit",
                "resolved_slots": {"checker_names": ["HarmfulContentLLMJudge"]},
                "missing_items": ["dataset_name"],
                "suggested_defaults": {
                    "dataset": "security_audit_samples",
                    "checker_names": ["HarmfulContentLLMJudge"],
                    "max_workers": 4,
                },
            },
            {
                "status": "ready",
                "assistant_message": "",
                "ready_to_execute": True,
                "resolved_task": "run security audit on security_audit_samples",
                "resolved_slots": {},
                "missing_items": [],
                "suggested_defaults": {},
            },
            {
                "action_type": "propose_pipeline",
                "reason": "Run the selected audit.",
            },
            {
                "goal_satisfied": True,
                "score": 1.0,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "stop_success",
                "reason": "Done.",
            },
        ]),
    )

    seen_prompts: list[str] = []

    def checkpoint_handler(checkpoint):
        prompt = checkpoint["payload"].get("prompt", "")
        seen_prompts.append(prompt)
        if "which dataset" in prompt.lower():
            return {"decision": "answer", "answer": "security_audit_samples as dataset."}
        if "checker" in prompt.lower():
            return {"decision": "answer", "answer": "use defaults"}
        raise AssertionError(f"Unexpected checkpoint prompt: {prompt}")

    response = controller.execute(
        task="run security audit",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        budget_steps=1,
        allow_experimental_tools=False,
        ask_user=True,
        checkpoint_handler=checkpoint_handler,
    )

    assert any("dataset" in prompt.lower() for prompt in seen_prompts)
    assert any("checker" in prompt.lower() for prompt in seen_prompts)
    assert response["goal_clarification"]["resolved_slots"]["dataset_name"] == "security_audit_samples"
    assert response["goal_clarification"]["resolved_slots"]["checker_names"] == DEFAULT_SECURITY_BASELINE


def test_pilot_controller_goal_clarification_requires_schema_slot_from_primary_tool(tmp_path, monkeypatch):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyProfileTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "status": "ready",
                "assistant_message": "",
                "ready_to_execute": True,
                "resolved_task": "run profile_tool on companies",
                "resolved_slots": {},
                "missing_items": [],
                "suggested_defaults": {},
            },
            {
                "action_type": "propose_pipeline",
                "reason": "Run the selected profile tool.",
            },
            {
                "goal_satisfied": True,
                "score": 1.0,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "stop_success",
                "reason": "Done.",
            },
        ]),
    )

    prompts: list[str] = []

    def checkpoint_handler(checkpoint):
        if checkpoint["checkpoint_type"] == "candidate_approval":
            return {"decision": "continue"}
        prompt = checkpoint["payload"].get("prompt", "")
        prompts.append(prompt)
        if "profile_name" in prompt:
            return {"decision": "answer", "answer": "strict"}
        raise AssertionError(f"Unexpected checkpoint prompt: {prompt}")

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeProfileToolAgent())

    response = controller.execute(
        task="run profile_tool on companies",
        dataset_schemas={"companies": ["id", "name"]},
        budget_steps=1,
        allow_experimental_tools=False,
        ask_user=True,
        checkpoint_handler=checkpoint_handler,
    )

    assert response["status"] == "success"
    assert any("profile_name" in prompt for prompt in prompts)
    assert response["goal_clarification"]["resolved_slots"]["dataset_name"] == "companies"
    assert response["goal_clarification"]["resolved_slots"]["profile_name"] == "strict"


def test_pilot_controller_planner_request_user_input_replans_same_attempt(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "action_type": "request_user_input",
                "reason": "Which dataset should I use?",
                "missing_items": ["dataset_name"],
            },
            {
                "action_type": "propose_pipeline",
                "reason": "Now I can execute.",
            },
            {
                "goal_satisfied": True,
                "score": 1.0,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "stop_success",
                "reason": "Done.",
            },
        ]),
    )

    import agentic.controller as controller_module

    class FakeDatasetCountAgent:
        def generate_pipeline(self, task: str):
            return (
                'data = load_dataset("companies")\n'
                'save_result({"count": len(data)})',
                {
                    "model": "fake-model",
                    "elapsed_seconds": 0.01,
                    "raw_response": "",
                },
            )

    original_create_agent_adapter = controller_module.create_agent_adapter
    controller_module.create_agent_adapter = lambda *args, **kwargs: FakeDatasetCountAgent()
    try:
        response = controller.execute(
            task="screen companies for issues",
            dataset_schemas={"companies": ["id", "name"]},
            budget_steps=1,
            allow_experimental_tools=False,
            ask_user=True,
            checkpoint_handler=lambda checkpoint: {"decision": "answer", "answer": "companies"},
        )
    finally:
        controller_module.create_agent_adapter = original_create_agent_adapter

    assert response["status"] == "success"
    assert len(response["attempts"]) == 1
    assert response["attempts"][0]["action"]["action_type"] == "propose_pipeline"


def test_pilot_controller_prefers_mutation_over_stop_failed_after_success(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "action_type": "propose_pipeline",
                "reason": "Start with a baseline audit.",
            },
            {
                "goal_satisfied": False,
                "score": 0.4,
                "failure_type": "low_score",
                "capability_gap": {},
                "recommended_next_action": "mutate_pipeline",
                "reason": "Need a better audit strategy.",
            },
            {
                "action_type": "stop_failed",
                "reason": "No point continuing.",
            },
            {
                "goal_satisfied": False,
                "score": 0.5,
                "failure_type": "low_score",
                "capability_gap": {},
                "recommended_next_action": "stop_failed",
                "reason": "Still not good enough.",
            },
        ]),
    )

    response = controller.execute(
        task="run security audit on security_audit_samples",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        budget_steps=2,
        allow_experimental_tools=False,
    )

    assert response["attempts"][1]["action"]["action_type"] == "mutate_pipeline"


def test_pilot_controller_stop_failed_terminates_loop(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "action_type": "propose_pipeline",
                "reason": "Start with a baseline audit.",
            },
            {
                "goal_satisfied": False,
                "score": 0.1,
                "failure_type": "execution_failure",
                "capability_gap": {},
                "recommended_next_action": "mutate_pipeline",
                "reason": "First attempt failed.",
            },
            {
                "action_type": "stop_failed",
                "reason": "Unable to make progress.",
            },
        ]),
    )

    response = controller.execute(
        task="run security audit on security_audit_samples",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        budget_steps=3,
        allow_experimental_tools=False,
    )

    assert response["status"] == "failed"
    assert len(response["attempts"]) == 2
    assert response["attempts"][-1]["action"]["action_type"] == "stop_failed"
    assert response["attempts"][-1]["judge"]["score"] == 0.0


def test_pilot_controller_broad_security_audit_requires_baseline_checker_coverage(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "action_type": "propose_pipeline",
                "reason": "Try a narrow audit first.",
            },
            {
                "goal_satisfied": True,
                "score": 1.0,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "stop_success",
                "reason": "Looks good.",
            },
        ]),
    )

    import agentic.controller as controller_module

    original_create_agent_adapter = controller_module.create_agent_adapter
    controller_module.create_agent_adapter = lambda *args, **kwargs: FakeNarrowSecurityAgent()
    try:
        response = controller.execute(
            task="run security audit on security_audit_samples",
            dataset_schemas={"security_audit_samples": ["id", "messages"]},
            budget_steps=1,
            allow_experimental_tools=False,
        )
    finally:
        controller_module.create_agent_adapter = original_create_agent_adapter

    judge = response["attempts"][0]["judge"]
    assert response["status"] == "budget_exhausted"
    assert judge["goal_satisfied"] is False
    assert judge["failure_type"] == "insufficient_security_coverage"
    assert judge["recommended_next_action"] == "mutate_pipeline"
    assert judge["score"] < 0.4
    assert judge["domain_metrics"]["coverage_ratio"] < 1.0
    assert judge["capability_gap"]["required_risk_categories"] == DEFAULT_SECURITY_RISK_BASELINE


def test_pilot_controller_broad_security_audit_with_baseline_but_poor_quality_stays_failed(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSecurityResultExecutor(
            security_score=44,
            flagged_rate=1.0,
            passed=False,
            flagged_samples=16,
        ),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "action_type": "propose_pipeline",
                "reason": "Try the full baseline audit.",
            },
            {
                "goal_satisfied": True,
                "score": 1.0,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "stop_success",
                "reason": "Looks good.",
            },
        ]),
    )

    import agentic.controller as controller_module

    original_create_agent_adapter = controller_module.create_agent_adapter
    controller_module.create_agent_adapter = lambda *args, **kwargs: FakeBroadPoorSecurityAgent()
    try:
        response = controller.execute(
            task="run security audit on security_audit_samples",
            dataset_schemas={"security_audit_samples": ["id", "messages"]},
            budget_steps=1,
            allow_experimental_tools=False,
        )
    finally:
        controller_module.create_agent_adapter = original_create_agent_adapter

    judge = response["attempts"][0]["judge"]
    assert response["status"] == "budget_exhausted"
    assert judge["goal_satisfied"] is False
    assert judge["failure_type"] == "insufficient_security_quality"
    assert judge["recommended_next_action"] == "mutate_pipeline"
    assert 0.4 <= judge["score"] <= 0.7
    assert judge["domain_metrics"]["coverage_ratio"] == 1.0
    assert judge["domain_metrics"]["security_score"] == 44.0
    assert judge["domain_metrics"]["required_risk_categories"] == DEFAULT_SECURITY_RISK_BASELINE


def test_pilot_controller_broad_security_audit_uses_risk_category_coverage_not_checker_names(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSecurityResultExecutor(
            security_score=44,
            flagged_rate=1.0,
            passed=False,
            flagged_samples=16,
        ),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "action_type": "propose_pipeline",
                "reason": "Try category coverage with mixed checker types.",
            },
            {
                "goal_satisfied": True,
                "score": 1.0,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "stop_success",
                "reason": "Looks good.",
            },
        ]),
    )

    import agentic.controller as controller_module

    original_create_agent_adapter = controller_module.create_agent_adapter
    controller_module.create_agent_adapter = lambda *args, **kwargs: FakeRiskCoverageSecurityAgent()
    try:
        response = controller.execute(
            task="run security audit on security_audit_samples",
            dataset_schemas={"security_audit_samples": ["id", "messages"]},
            budget_steps=1,
            allow_experimental_tools=False,
        )
    finally:
        controller_module.create_agent_adapter = original_create_agent_adapter

    judge = response["attempts"][0]["judge"]
    assert judge["failure_type"] == "insufficient_security_quality"
    assert judge["domain_metrics"]["coverage_ratio"] == 1.0
    assert judge["domain_metrics"]["covered_risk_categories"] == DEFAULT_SECURITY_RISK_BASELINE


def test_derive_python_tool_uses_source_tool_context_and_semantic_name(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    llm_provider = CapturingLLMProvider([
        {
            "name": "candidate_7fa3c1",
            "description": "Refined audit helper.",
            "validation_criteria": ["Compiles."],
            "review_comments": ["Current tool should handle corner cases more gracefully."],
            "enhancement_rationale": "Improve error handling around unstable checker calls.",
            "behavior_changes": ["Adds fallback handling for checker failures."],
            "compatibility_notes": "Keeps the same input contract.",
            "code": '''from typing import Any

from tools.base_tool import BaseTool, ToolContext


class EnhancedAuditTool(BaseTool):
    @property
    def name(self) -> str:
        return "tool_tmp_123"

    @property
    def description(self) -> str:
        return "Refined audit helper."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["data"],
        }

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return {"result": {"ok": True}}


def build_tool():
    return EnhancedAuditTool()
''',
        }
    ])

    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=llm_provider,
    )

    candidate, code = controller._derive_python_tool(
        task="run security audit on security_audit_samples",
        pipeline='result = run_tool("security_audit", data=data, checker_names=["PIIRule"])',
        source_attempts=["attempt_01"],
        execution={
            "success": False,
            "error": "Tool execution failed",
            "elapsed_seconds": 12.4,
            "log_excerpt": [{"level": "WARNING", "message": "checker failed"}],
        },
        judge={
            "failure_type": "tool_execution_error",
            "capability_gap": {"reason": "Handle checker failures better."},
            "reason": "Refine the current tool implementation.",
        },
    )

    prompt = llm_provider.prompts[0]
    assert "Relevant called tool contexts" in prompt
    assert "security_audit" in prompt
    assert "review_comments" in prompt
    assert "Do not use UUIDs, hashes, timestamps" in prompt
    assert "Import `BaseTool` and `ToolContext` from `tools.base_tool`." in prompt
    assert "Implement `run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]`." in prompt
    assert "Read the provided source tool code before proposing an enhancement." in prompt
    assert "bounded concurrency" in prompt
    assert "elapsed_seconds" in prompt
    assert "You may import additional packages" in prompt
    assert candidate["name"] == "security_audit_enhanced"
    assert 'return "security_audit_enhanced"' in code
    assert candidate["review_comments"] == ["Current tool should handle corner cases more gracefully."]


def test_collect_tool_contexts_include_optimization_hints_for_skill_extraction_tool(tmp_path):
    import agentic.controller as controller_module
    from tools.trajectory_skill_extraction import SkillRLSkillExtractionTool

    registry = get_global_registry()
    registry.clear()
    registry.register(SkillRLSkillExtractionTool())

    contexts = controller_module._collect_tool_contexts_from_pipeline(
        'result = run_tool("skillrl_skill_extraction", data=data)',
        registry,
        include_code=True,
    )

    assert len(contexts) == 1
    hints = contexts[0]["optimization_hints"]
    assert hints["uses_concurrency_primitives"] is False
    assert hints["helper_generation_call_sites"] >= 4
    assert hints["hints"]


def test_derive_composite_tool_normalizes_nonsemantic_name(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    llm_provider = CapturingLLMProvider([
        {
            "name": "derived_1234abcd",
            "description": "Composite audit wrapper.",
            "steps": [
                {
                    "type": "run_tool",
                    "tool_name": "security_audit",
                    "kwargs": {"data": "$input.data"},
                    "output": "audit",
                }
            ],
            "result": {"audit": "$audit"},
        }
    ])

    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=llm_provider,
    )

    candidate = controller._derive_composite_tool(
        task="run security audit on security_audit_samples",
        pipeline='result = run_tool("security_audit", data=data)',
        source_attempts=["attempt_01"],
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
    )

    assert "Relevant called tools" in llm_provider.prompts[0]
    assert candidate["name"] == "security_audit_composite"


def test_security_dataset_name_does_not_trigger_security_audit_intent(tmp_path):
    import agentic.controller as controller_module

    task = "把security_audit_samples里的dataset type为sft的数据抽取出来告诉我总共多少条,然后写入一个test_data/下的新文件里"

    assert controller_module._task_targets_security_checker_selection(task) is False
    assert controller_module._is_broad_security_audit_task(task) is False
    assert controller_module._build_security_audit_hints(task, get_global_registry()) is None


def test_non_audit_filter_task_is_not_forced_into_security_audit_rubric(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    judge, _meta = controller._judge_attempt(
        task="把security_audit_samples里的dataset type为sft的数据抽取出来告诉我总共多少条,然后写入一个test_data/下的新文件里",
        pipeline=(
            'data = load_dataset("security_audit_samples", filters={"dataset_type": "sft"})\n'
            'write_file(data, "test_data/sft_filtered_security_data.jsonl")\n'
            'save_result({"total_sft_records": len(data)})'
        ),
        execution={
            "success": True,
            "result": {"total_sft_records": 15, "output_file": "test_data/sft_filtered_security_data.jsonl"},
            "artifacts": {},
            "metadata": {},
            "error": None,
        },
        previous_attempts=[],
    )

    assert judge["goal_satisfied"] is True
    assert judge["failure_type"] == "none"
    assert judge["score"] == 1.0


def test_pilot_controller_marks_llm_checker_content_filter_as_capability_gap(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeContentFilterExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    import agentic.controller as controller_module

    execution = controller_module._attach_execution_signals(FakeContentFilterExecutor().execute("job_test", ""))
    judge, _llm = controller._judge_attempt(
        task="run security audit",
        pipeline=(
            'data = load_dataset("security_audit_samples")\n'
            'result = run_tool("security_audit", data=data, checker_names=["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule", "HarmfulContentLLMJudge"])\n'
            "save_result(result)"
        ),
        execution=execution,
        previous_attempts=[],
    )

    assert judge["failure_type"] == "llm_checker_content_filter"
    assert judge["recommended_next_action"] == "mutate_pipeline"
    assert judge["score"] <= 0.34
    assert judge["capability_gap"]["avoid_checkers"] == ["HarmfulContentLLMJudge"]
    assert judge["capability_gap"]["recommended_checker_names"] == DEFAULT_SECURITY_BASELINE


def test_pilot_controller_marks_llm_checker_execution_error_as_capability_gap(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeCheckerExecutionErrorExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    import agentic.controller as controller_module

    execution = controller_module._attach_execution_signals(FakeCheckerExecutionErrorExecutor().execute("job_test", ""))
    judge, _llm = controller._judge_attempt(
        task="run security audit",
        pipeline=(
            'data = load_dataset("security_audit_samples")\n'
            'result = run_tool("security_audit", data=data, checker_names=["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule", "HarmfulContentLLMJudge"])\n'
            "save_result(result)"
        ),
        execution=execution,
        previous_attempts=[],
    )

    assert judge["failure_type"] == "llm_checker_execution_error"
    assert judge["recommended_next_action"] == "mutate_pipeline"
    assert judge["score"] <= 0.3
    assert judge["capability_gap"]["avoid_checkers"] == ["HarmfulContentLLMJudge"]
    assert judge["capability_gap"]["recommended_checker_names"] == DEFAULT_SECURITY_BASELINE


def test_pilot_controller_rewrites_repeated_content_filter_checker(tmp_path):
    import agentic.controller as controller_module

    previous_attempts = [
        {
            "judge": {
                "capability_gap": {
                    "type": "llm_checker_content_filter",
                    "avoid_checkers": ["HarmfulContentLLMJudge"],
                    "recommended_checker_names": DEFAULT_SECURITY_BASELINE,
                }
            }
        }
    ]
    pipeline = (
        'data = load_dataset("security_audit_samples")\n'
        'result = run_tool(\n'
        '    "security_audit",\n'
        '    data=data,\n'
        '    checker_names=["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule", "HarmfulContentLLMJudge"]\n'
        ')\n'
        "save_result(result)"
    )

    stabilized = controller_module._stabilize_security_checker_failover(pipeline, previous_attempts)

    assert "HarmfulContentLLMJudge" not in stabilized
    assert 'checker_names=["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule"]' in stabilized


def test_pilot_controller_normalizes_list_planner_instructions_after_content_filter(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    action = controller._stabilize_planner_action(
        "run security audit on security_audit_samples",
        previous_attempts=[
            {
                "execution": {
                    "success": True,
                    "signals": {
                        "security_audit": {
                            "llm_checker_content_filter": {
                                "affected_checkers": ["HarmfulContentLLMJudge"],
                                "recommended_checker_names": DEFAULT_SECURITY_BASELINE,
                            }
                        }
                    },
                },
                "judge": {
                    "goal_satisfied": False,
                    "capability_gap": {
                        "type": "llm_checker_content_filter",
                        "avoid_checkers": ["HarmfulContentLLMJudge"],
                        "recommended_checker_names": DEFAULT_SECURITY_BASELINE,
                    },
                },
            }
        ],
        action={
            "action_type": "propose_pipeline",
            "reason": "Retry safely.",
            "instructions": [
                "Drop HarmfulContentLLMJudge.",
                "Use a rule-based fallback checker set.",
            ],
        },
    )

    assert action["action_type"] == "mutate_pipeline"
    assert "Retry safely." in action["reason"]
    assert "Drop HarmfulContentLLMJudge." in action["instructions"]
    assert "Use a rule-based fallback checker set." in action["instructions"]
    assert "Do not use these checkers: ['HarmfulContentLLMJudge']." in action["instructions"]


def test_pilot_controller_preserves_baseline_after_security_quality_failure(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    action = controller._stabilize_planner_action(
        "run security audit on security_audit_samples",
        previous_attempts=[
            {
                "execution": {"success": True},
                "judge": {
                    "goal_satisfied": False,
                    "failure_type": "insufficient_security_quality",
                    "domain_metrics": {"coverage_ratio": 1.0},
                    "capability_gap": {
                        "type": "insufficient_security_quality",
                        "required_risk_categories": DEFAULT_SECURITY_RISK_BASELINE,
                        "covered_checkers": DEFAULT_SECURITY_BASELINE + ["BiasKeywordRule"],
                    },
                },
            }
        ],
        action={
            "action_type": "propose_pipeline",
            "reason": "Try again.",
            "instructions": "Improve the audit.",
        },
    )

    assert action["action_type"] == "mutate_pipeline"
    assert "Keep coverage for these risk categories intact" in action["instructions"]
    assert "Do not remove already covered baseline checkers." in action["instructions"]


def test_pilot_controller_escalates_repeated_tool_failures_to_experimental_tool(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    action = controller._stabilize_planner_action(
        "run security audit on security_audit_samples",
        previous_attempts=[
            {
                "pipeline": 'result = run_tool("security_audit", data=data, checker_names=["HarmfulContentLLMJudge"])',
                "execution": {"success": True},
                "judge": {
                    "goal_satisfied": False,
                    "failure_type": "llm_checker_execution_error",
                    "score": 0.21,
                },
            },
            {
                "pipeline": 'result = run_tool("security_audit", data=data, checker_names=["HarmfulContentLLMJudge"])',
                "execution": {"success": True},
                "judge": {
                    "goal_satisfied": False,
                    "failure_type": "llm_checker_execution_error",
                    "score": 0.19,
                },
            },
        ],
        action={
            "action_type": "mutate_pipeline",
            "reason": "Try a new attempt.",
            "instructions": "Improve the workflow.",
        },
        allow_experimental_tools=True,
    )

    assert action["action_type"] == "derive_python_tool_draft"
    assert "tool-level execution instability" in action["instructions"]
    assert "security_audit" in action["instructions"]


def test_pilot_controller_escalates_quality_plateau_to_experimental_tool(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    action = controller._stabilize_planner_action(
        "run security audit on security_audit_samples",
        previous_attempts=[
            {
                "pipeline": 'result = run_tool("security_audit", data=data, checker_names=["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule"])',
                "execution": {"success": True},
                "judge": {
                    "goal_satisfied": False,
                    "failure_type": "insufficient_security_quality",
                    "score": 0.55,
                    "domain_metrics": {
                        "coverage_ratio": 1.0,
                        "security_score": 53.0,
                    },
                },
            },
            {
                "pipeline": 'result = run_tool("security_audit", data=data, checker_names=["PIIRule", "SecretRule", "ToxicityKeywordRule", "HarmfulKeywordRule", "BiasKeywordRule"])',
                "execution": {"success": True},
                "judge": {
                    "goal_satisfied": False,
                    "failure_type": "insufficient_security_quality",
                    "score": 0.56,
                    "domain_metrics": {
                        "coverage_ratio": 1.0,
                        "security_score": 57.0,
                    },
                },
            },
        ],
        action={
            "action_type": "mutate_pipeline",
            "reason": "Try a stronger combination.",
            "instructions": "Improve the audit quality.",
        },
        allow_experimental_tools=True,
    )

    assert action["action_type"] == "derive_python_tool_draft"
    assert "plateaued on quality" in action["instructions"]
    assert "security_audit" in action["instructions"]


def test_pilot_controller_does_not_override_planner_for_unmatched_structured_task_on_first_attempt(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())
    registry.register(DummyProfileTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {"action_type": "propose_pipeline", "reason": "Try a pipeline first."},
        ]),
    )

    action, _meta = controller._plan_action(
        task="find duplicate records in dedup_demo and merge rows with different ids but otherwise identical fields except timestamps",
        dataset_schemas={"dedup_demo": ["id", "created_at", "messages"]},
        previous_attempts=[],
        allow_experimental_tools=True,
    )

    assert action["action_type"] == "propose_pipeline"


def test_pilot_controller_prefers_plain_pipeline_for_simple_structured_filter_task(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())
    registry.register(DummyProfileTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {"action_type": "propose_pipeline", "reason": "Try a pipeline first."},
        ]),
    )

    action, _meta = controller._plan_action(
        task="把 security_audit_samples 里 dataset_type 为 rl 的数据抽取出来，统计数量并写入 test_data/ 新文件",
        dataset_schemas={"security_audit_samples": ["id", "dataset_type", "messages"]},
        previous_attempts=[],
        allow_experimental_tools=True,
    )

    assert action["action_type"] == "propose_pipeline"


def test_planner_prompt_includes_user_strategy_preferences():
    import agentic.controller as controller_module

    prompt = controller_module._build_planner_prompt(
        task="pilot 模式下自由尝试，优先派生 experimental python tool，失败后继续修复",
        dataset_schemas={"companies": ["id", "name"]},
        tool_schemas=[],
        previous_attempts=[],
        allow_experimental_tools=True,
    )

    assert "The user prefers experimental Python tool derivation" in prompt
    assert "The user prefers freer pilot exploration" in prompt
    assert "Treat failed attempts as evidence for the next repair" in prompt


def test_pilot_controller_does_not_force_experimental_tool_after_first_failure_for_unmatched_structured_task(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())
    registry.register(DummyProfileTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    action = controller._stabilize_planner_action(
        "merge duplicate json records with different ids but matching content except timestamps",
        previous_attempts=[
            {
                "pipeline": 'data = load_dataset("dedup_demo")\nsave_result(data)',
                "execution": {"success": False, "error": "attempt failed"},
                "judge": {
                    "goal_satisfied": False,
                    "failure_type": "execution_failure",
                    "score": 0.0,
                },
            }
        ],
        action={
            "action_type": "mutate_pipeline",
            "reason": "Try another pipeline.",
            "instructions": "Keep going.",
        },
        allow_experimental_tools=True,
    )

    assert action["action_type"] == "mutate_pipeline"


def test_pilot_controller_pauses_for_external_write_approval(tmp_path, monkeypatch):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {"action_type": "propose_pipeline", "reason": "Export the dataset."},
        ]),
    )

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeWriteFileAgent())

    response = controller.execute(
        task="export companies into a new file",
        dataset_schemas={"companies": ["id", "name"]},
        budget_steps=1,
        allow_experimental_tools=False,
        ask_user=False,
    )

    assert response["status"] == "paused"
    job = job_manager.get_job(response["job_id"])
    assert job is not None
    assert job.checkpoint_type == "write_approval"
    assert job.checkpoint_state == "awaiting_input"
    assert job.checkpoint_payload["paths"] == ["test_data/companies_export_01.jsonl"]


def test_validate_candidate_smoke_context_exposes_datasets(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    code_path = tmp_path / "dataset_probe_tool.py"
    code_path.write_text(
        """from typing import Any

from tools.base_tool import BaseTool, ToolContext


class DatasetProbeTool(BaseTool):
    @property
    def name(self) -> str:
        return "dataset_probe_tool"

    @property
    def description(self) -> str:
        return "Probe smoke-test dataset access."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dataset_name": {"type": "string", "default": "security_audit_samples"},
            },
            "required": [],
        }

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        dataset_name = kwargs.get("dataset_name", "security_audit_samples")
        return {"result": {"rows": len(context.datasets.get(dataset_name, []))}}


def build_tool():
    return DatasetProbeTool()
""",
        encoding="utf-8",
    )

    validation = controller._validate_candidate(
        candidate={
            "candidate_id": "cand_py_dataset_probe",
            "candidate_type": "experimental_python_tool",
            "code_path": str(code_path),
        },
        pipeline='data = load_dataset("security_audit_samples")\nsave_result(data)',
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        allow_experimental_tools=True,
    )

    assert validation["validation_status"] == "smoke_passed"


def test_pilot_controller_continues_after_rejected_successful_candidate_if_budget_remains(tmp_path, monkeypatch):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {"action_type": "propose_pipeline", "reason": "First successful attempt."},
            {
                "goal_satisfied": True,
                "score": 0.97,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "none",
                "reason": "Goal satisfied.",
            },
            {"action_type": "mutate_pipeline", "reason": "Keep optimizing after rejection."},
            {
                "goal_satisfied": True,
                "score": 0.99,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "none",
                "reason": "Still satisfied.",
            },
        ]),
    )

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeSimpleSuccessAgent())

    def checkpoint_handler(checkpoint):
        if checkpoint["checkpoint_type"] == "candidate_approval":
            return {"decision": "reject"}
        return {"decision": "continue"}

    response = controller.execute(
        task="count records and keep iterating for better reusable assets",
        dataset_schemas={"companies": ["id", "name"]},
        budget_steps=2,
        allow_experimental_tools=False,
        ask_user=True,
        checkpoint_handler=checkpoint_handler,
    )

    assert response["status"] == "success"
    assert len(response["attempts"]) == 2
    assert response["attempts"][1]["action"]["action_type"] == "mutate_pipeline"
    candidate_ids = [
        attempt["candidate"]["candidate_id"]
        for attempt in response["attempts"]
        if attempt.get("candidate")
    ]
    assert len(candidate_ids) == 2
    assert candidate_ids[0] != candidate_ids[1]


def test_pilot_controller_stabilizes_write_targets_per_attempt(tmp_path, monkeypatch):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "status": "ready",
                "assistant_message": "",
                "ready_to_execute": True,
                "resolved_task": "export the companies dataset to a file",
                "resolved_slots": {},
                "missing_items": [],
                "suggested_defaults": {},
            },
            {"action_type": "propose_pipeline", "reason": "First export attempt."},
            {
                "goal_satisfied": True,
                "score": 0.97,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "none",
                "reason": "Goal satisfied.",
            },
            {"action_type": "stop_failed", "reason": "No need to continue."},
            {
                "goal_satisfied": True,
                "score": 0.99,
                "failure_type": "none",
                "capability_gap": {},
                "recommended_next_action": "none",
                "reason": "Goal still satisfied.",
            },
        ]),
    )

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeWriteFileAgent())

    def checkpoint_handler(checkpoint):
        if checkpoint["checkpoint_type"] == "write_approval":
            return {"decision": "allow"}
        if checkpoint["checkpoint_type"] == "candidate_approval":
            return {"decision": "reject"}
        return {"decision": "continue"}

    response = controller.execute(
        task="export the companies dataset to a file",
        dataset_schemas={"companies": ["id", "name"]},
        budget_steps=2,
        allow_experimental_tools=False,
        ask_user=True,
        checkpoint_handler=checkpoint_handler,
    )

    assert response["status"] == "success"
    assert "test_data/companies_export_01.jsonl" in response["attempts"][0]["pipeline"]
    assert "test_data/companies_export_02.jsonl" in response["attempts"][1]["pipeline"]


def test_pilot_summary_collects_all_attempt_candidates_and_new_latency_metrics(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    summary = controller._build_pilot_summary([
        {
            "attempt_id": "attempt_01",
            "judge": {"score": 0.8},
            "attempt_metrics": {
                "attempt_id": "attempt_01",
                "judge_score": 0.8,
                "security_score": 55.0,
                "attempt_total_latency_s": 10.0,
                "pipeline_execution_latency_s": 1.5,
            },
            "candidates": [
                {"candidate_id": "cand_py_1", "candidate_type": "experimental_python_tool"},
                {"candidate_id": "cand_pipe_1", "candidate_type": "pipeline"},
            ],
        },
        {
            "attempt_id": "attempt_02",
            "judge": {"score": 0.9},
            "attempt_metrics": {
                "attempt_id": "attempt_02",
                "judge_score": 0.9,
                "security_score": 60.0,
                "attempt_total_latency_s": 8.0,
                "pipeline_execution_latency_s": 1.0,
            },
            "candidate": {"candidate_id": "cand_pipe_2", "candidate_type": "pipeline"},
        },
    ])

    assert summary["candidate_ids"] == ["cand_py_1", "cand_pipe_1", "cand_pipe_2"]
    assert summary["best_vs_first"]["attempt_total_latency_delta"] == -2.0
    assert summary["best_vs_first"]["pipeline_execution_latency_delta"] == -0.5


def test_pilot_controller_prefers_repairing_failed_experimental_candidate(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    action = controller._stabilize_planner_action(
        "filter sft rows and keep deriving a reusable tool",
        previous_attempts=[
            {
                "pipeline": 'data = load_dataset("security_audit_samples")\nsave_result({"count": len(data)})',
                "execution": {"success": True},
                "judge": {
                    "goal_satisfied": True,
                    "score": 0.97,
                    "failure_type": "none",
                },
                "candidates": [
                    {
                        "candidate_id": "cand_py_failed",
                        "candidate_type": "experimental_python_tool",
                        "name": "sft_filter_tool",
                        "validation_status": "smoke_failed",
                        "validation_summary": "Smoke test failed: AttributeError: ToolContext.datasets missing",
                    }
                ],
            }
        ],
        action={
            "action_type": "mutate_pipeline",
            "reason": "Keep iterating.",
            "instructions": "Try another improvement.",
        },
        allow_experimental_tools=True,
    )

    assert action["action_type"] == "derive_python_tool_draft"
    assert "failed validation" in action["instructions"]


def test_judge_normalizes_string_capability_gap_and_freeform_next_action(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {
                "goal_satisfied": False,
                "score": 0.15,
                "failure_type": "tool_parameter_error",
                "capability_gap": "Remove the unexpected `data` parameter.",
                "recommended_next_action": "Remove the `data=data` argument and let the tool load its own dataset.",
                "reason": "Tool parameter mismatch.",
            }
        ]),
    )

    judge, _meta = controller._judge_attempt(
        task="把security_audit_sample里的dataset type为sft的数据抽取出来告诉我总共多少条,然后写入一个test_data/下的新文件里",
        pipeline='result = run_tool("security_audit_sft_extractor", data=data)',
        execution={
            "success": False,
            "result": None,
            "artifacts": {},
            "metadata": {},
            "error": "Tool parameter error",
        },
        previous_attempts=[],
    )

    assert judge["recommended_next_action"] == "mutate_pipeline"
    assert judge["capability_gap"] == {"reason": "Remove the unexpected `data` parameter."}
    assert "Suggested next step" in judge["reason"]


def test_stabilize_planner_action_tolerates_string_capability_gap(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    action = controller._stabilize_planner_action(
        "filter sft rows into a new file",
        previous_attempts=[
            {
                "pipeline": 'result = run_tool("security_audit_sft_extractor", data=data)',
                "execution": {"success": False, "error": "Tool parameter error"},
                "judge": {
                    "goal_satisfied": False,
                    "score": 0.15,
                    "failure_type": "tool_parameter_error",
                    "capability_gap": "Remove data=data",
                },
            }
        ],
        action={
            "action_type": "mutate_pipeline",
            "reason": "Retry with a fix.",
            "instructions": "Adjust the tool call.",
        },
        allow_experimental_tools=True,
    )

    assert action["action_type"] == "mutate_pipeline"


def test_pilot_write_approval_can_return_freeform_answer(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )
    job = job_manager.create_job("write approval test", mode="pilot")

    response = controller._maybe_request_pilot_write_approval(
        job_id=job.job_id,
        attempt_id="attempt_01",
        pipeline='write_file(data, "test_data/original.json")',
        ask_user=True,
        checkpoint_handler=lambda checkpoint: {
            "decision": "answer",
            "answer": "把文件名改成 test_data/renamed.json",
        },
        event_handler=None,
    )

    assert response["decision"] == "answer"
    assert "renamed.json" in response["answer"]


def test_collect_tool_contexts_tolerates_dynamic_tool_without_sourcefile(tmp_path, monkeypatch):
    import agentic.controller as controller_module

    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    monkeypatch.setattr(
        controller_module.inspect,
        "getsourcefile",
        lambda _obj: (_ for _ in ()).throw(TypeError("built-in class")),
    )

    contexts = controller_module._collect_tool_contexts_from_pipeline(
        'result = run_tool("security_audit", data=data)',
        registry,
        include_code=True,
    )

    assert len(contexts) == 1
    assert contexts[0]["tool_name"] == "security_audit"
    assert contexts[0]["source_file"] is None
    assert contexts[0]["source_code"] == ""


def test_pilot_controller_continues_when_python_tool_derivation_raises(tmp_path, monkeypatch):
    import agentic.controller as controller_module

    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())
    registry.register(DummyProfileTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {"action_type": "derive_python_tool_draft", "reason": "Try deriving a tool first."},
            {
                "goal_satisfied": False,
                "score": 0.0,
                "failure_type": "execution_failure",
                "capability_gap": {},
                "recommended_next_action": "mutate_pipeline",
                "reason": "attempt failed",
            },
        ]),
    )

    monkeypatch.setattr(controller_module, "create_agent_adapter", lambda *args, **kwargs: FakeSimpleSuccessAgent())
    monkeypatch.setattr(controller, "_derive_python_tool", lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("boom")))

    response = controller.execute(
        task="find duplicate records in dedup_demo and merge rows with different ids but otherwise identical fields except timestamps",
        dataset_schemas={"dedup_demo": ["id", "created_at", "messages"]},
        budget_steps=1,
        allow_experimental_tools=True,
        ask_user=False,
    )

    assert response["status"] == "budget_exhausted"
    assert response["attempts"][0]["candidate_errors"][0]["candidate_type"] == "experimental_python_tool"
    assert "TypeError: boom" in response["attempts"][0]["candidate_errors"][0]["error"]


def test_register_candidate_tools_skips_smoke_failed_experimental_tool(tmp_path):
    asset_manager = AssetManager(root=tmp_path / ".elf")
    registry = get_global_registry()
    registry.clear()

    code_path = tmp_path / ".elf" / "candidates" / "tool_code" / "experimental" / "cand_py_bad.py"
    code_path.parent.mkdir(parents=True, exist_ok=True)
    code_path.write_text(
        "from tools.base_tool import BaseTool\n"
        "class BadTool(BaseTool):\n"
        "    @property\n"
        "    def name(self): return 'bad_tool'\n"
        "    @property\n"
        "    def description(self): return 'bad'\n"
        "    @property\n"
        "    def parameters(self): return {'type': 'object', 'properties': {}}\n"
        "    def run(self, context, **kwargs): return {'result': 'ok'}\n"
        "def build_tool(): return BadTool()\n",
        encoding="utf-8",
    )

    asset_manager.save_candidate({
        "candidate_id": "cand_py_bad",
        "candidate_type": "experimental_python_tool",
        "name": "bad_tool",
        "description": "bad tool",
        "status": "smoke_failed",
        "validation_status": "smoke_failed",
    }, python_code=code_path.read_text(encoding="utf-8"))

    loaded = asset_manager.register_candidate_tools(registry, allow_experimental=True)

    assert loaded == []
    assert registry.get("bad_tool") is None


def test_prepare_python_tool_candidate_for_attempt_registers_draft_for_current_pilot(tmp_path, monkeypatch):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    job = job_manager.create_job("pilot draft registration", mode="pilot")
    registry = get_global_registry()
    registry.clear()

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    draft_code = '''from typing import Any

from tools.base_tool import BaseTool, ToolContext


class DraftTool(BaseTool):
    @property
    def name(self) -> str:
        return "draft_tool"

    @property
    def description(self) -> str:
        return "draft"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["data"],
        }

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return {"result": {"ok": True}}


def build_tool() -> BaseTool:
    return DraftTool()
'''

    monkeypatch.setattr(
        controller,
        "_derive_python_tool",
        lambda **_kwargs: (
            {
                "candidate_id": "cand_py_draft_tool",
                "candidate_type": "experimental_python_tool",
                "name": "draft_tool",
                "description": "draft",
            },
            draft_code,
        ),
    )

    candidate, validation = controller._prepare_python_tool_candidate_for_attempt(
        job_id=job.job_id,
        task="从 companies 中抽取数据并写文件",
        attempt_id="attempt_01",
        previous_attempts=[],
        dataset_schemas={"companies": ["id", "name"]},
        allow_experimental_tools=True,
    )

    assert candidate["name"] == "draft_tool"
    assert validation["validation_status"] == "smoke_passed"
    assert registry.get("draft_tool") is not None


def test_materialize_pipeline_includes_note_for_derive_python_tool_attempt(tmp_path, monkeypatch):
    import agentic.controller as controller_module

    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    agent = CapturingTaskAgent()
    monkeypatch.setattr(controller_module, "create_agent_adapter", lambda *args, **kwargs: agent)

    controller._materialize_pipeline(
        task="filter records into a file",
        action={"action_type": "derive_python_tool_draft", "reason": "derive it", "instructions": "repair the failing path"},
        dataset_schemas={"companies": ["id", "name"]},
        previous_attempts=[{"execution": {"success": False, "error": "tool not found"}}],
        attempt_id="attempt_01",
    )

    assert agent.tasks
    assert "Do not call a speculative new tool name" in agent.tasks[0]
    assert "may use an experimental python tool draft" in agent.tasks[0].lower()


def test_materialize_pipeline_includes_diversity_note_after_successful_continue_optimization(tmp_path, monkeypatch):
    import agentic.controller as controller_module

    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    agent = CapturingTaskAgent()
    monkeypatch.setattr(controller_module, "create_agent_adapter", lambda *args, **kwargs: agent)

    controller._materialize_pipeline(
        task="keep optimizing this successful extraction workflow",
        action={"action_type": "mutate_pipeline", "reason": "Keep optimizing after candidate review."},
        dataset_schemas={"companies": ["id", "name"]},
        previous_attempts=[
            {
                "attempt_id": "attempt_01",
                "pipeline": 'data = load_dataset("companies")\nresult = run_tool("security_audit", data=data)\nsave_result(result)',
                "execution": {"success": True},
                "judge": {"goal_satisfied": True},
                "continue_optimization": {"enabled": True},
            }
        ],
        attempt_id="attempt_02",
    )

    assert agent.tasks
    assert "Optimization diversity note:" in agent.tasks[0]
    assert "must make one materially different improvement" in agent.tasks[0]
    assert "Do not just change filenames" in agent.tasks[0]


def test_materialize_pipeline_regenerates_duplicate_optimization_attempt(tmp_path, monkeypatch):
    import agentic.controller as controller_module

    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    duplicate_pipeline = (
        'log_step("Loading dataset")\n'
        'data = load_dataset("companies")\n'
        'result = run_tool("security_audit", data=data)\n'
        'save_result(result)'
    )
    improved_pipeline = (
        'data = load_dataset("companies")\n'
        'filtered = [row for row in data]\n'
        'save_result({"count": len(filtered), "records": filtered})'
    )
    agent = SequencedTaskAgent([duplicate_pipeline, improved_pipeline])
    monkeypatch.setattr(controller_module, "create_agent_adapter", lambda *args, **kwargs: agent)

    pipeline, _ = controller._materialize_pipeline(
        task="keep optimizing this successful extraction workflow",
        action={"action_type": "mutate_pipeline", "reason": "Keep optimizing after candidate review."},
        dataset_schemas={"companies": ["id", "name"]},
        previous_attempts=[
            {
                "attempt_id": "attempt_01",
                "pipeline": duplicate_pipeline,
                "execution": {"success": True},
                "judge": {"goal_satisfied": True},
                "continue_optimization": {"enabled": True},
            }
        ],
        attempt_id="attempt_02",
    )

    assert len(agent.tasks) == 2
    assert "too close to the previous successful attempt" in agent.tasks[1]
    assert 'filtered = [row for row in data]' in pipeline
    assert 'save_result({"count": len(filtered), "records": filtered})' in pipeline
    assert 'run_tool("security_audit", data=data)' not in pipeline


def test_materialize_pipeline_does_not_regenerate_duplicate_after_failure(tmp_path, monkeypatch):
    import agentic.controller as controller_module

    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    retry_pipeline = (
        'data = load_dataset("companies")\n'
        'result = run_tool("security_audit", data=data)\n'
        'save_result(result)'
    )
    agent = SequencedTaskAgent([retry_pipeline])
    monkeypatch.setattr(controller_module, "create_agent_adapter", lambda *args, **kwargs: agent)

    pipeline, _ = controller._materialize_pipeline(
        task="fix the failed workflow",
        action={"action_type": "mutate_pipeline", "reason": "Retry after failure."},
        dataset_schemas={"companies": ["id", "name"]},
        previous_attempts=[
            {
                "attempt_id": "attempt_01",
                "pipeline": retry_pipeline,
                "execution": {"success": False, "error": "tool parameter error"},
                "judge": {"goal_satisfied": False},
            }
        ],
        attempt_id="attempt_02",
    )

    assert len(agent.tasks) == 1
    assert 'run_tool("security_audit", data=data)' in pipeline


def test_materialize_pipeline_regenerates_after_repeated_same_tool_failures(tmp_path, monkeypatch):
    import agentic.controller as controller_module

    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    repeated_failure_pipeline = (
        'data = load_dataset("companies")\n'
        'result = run_tool("security_audit", data=data)\n'
        'save_result(result)'
    )
    different_repair_pipeline = (
        'data = load_dataset("companies", filters={"kind": "safe"})\n'
        'save_result({"count": len(data), "records": data})'
    )
    agent = SequencedTaskAgent([repeated_failure_pipeline, different_repair_pipeline])
    monkeypatch.setattr(controller_module, "create_agent_adapter", lambda *args, **kwargs: agent)

    pipeline, _ = controller._materialize_pipeline(
        task="fix the failed workflow",
        action={"action_type": "mutate_pipeline", "reason": "Retry after repeated failures."},
        dataset_schemas={"companies": ["id", "name", "kind"]},
        previous_attempts=[
            {
                "attempt_id": "attempt_01",
                "pipeline": repeated_failure_pipeline,
                "execution": {"success": False, "error": "tool output shape mismatch"},
                "judge": {"goal_satisfied": False},
            },
            {
                "attempt_id": "attempt_02",
                "pipeline": repeated_failure_pipeline,
                "execution": {"success": False, "error": "tool output shape mismatch"},
                "judge": {"goal_satisfied": False},
            },
        ],
        attempt_id="attempt_03",
    )

    assert len(agent.tasks) == 2
    assert "Recent failed attempts repeated the same primary tool path" in agent.tasks[1]
    assert 'filters={"kind": "safe"}' in pipeline
    assert 'run_tool("security_audit", data=data)' not in pipeline


def test_pilot_controller_next_attempt_avoids_failed_llm_checker(tmp_path, monkeypatch):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeContentFilterExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {"action_type": "propose_pipeline", "reason": "Initial attempt."},
            {
                "goal_satisfied": False,
                "score": 0.56,
                "failure_type": "insufficient_security_quality",
                "capability_gap": {},
                "recommended_next_action": "mutate_pipeline",
                "reason": "Needs improvement.",
            },
            {"action_type": "propose_pipeline", "reason": "Try again."},
            {
                "goal_satisfied": False,
                "score": 0.56,
                "failure_type": "insufficient_security_quality",
                "capability_gap": {},
                "recommended_next_action": "mutate_pipeline",
                "reason": "Needs improvement.",
            },
        ]),
    )

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeUnsafeLLMCheckerAgent())

    response = controller.execute(
        task="run security audit on security_audit_samples",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        budget_steps=2,
        allow_experimental_tools=False,
        ask_user=False,
    )

    assert response["status"] == "budget_exhausted"
    assert response["attempts"][0]["judge"]["failure_type"] == "llm_checker_content_filter"
    assert "HarmfulContentLLMJudge" in response["attempts"][0]["pipeline"]
    assert "HarmfulContentLLMJudge" not in response["attempts"][1]["pipeline"]
    assert response["attempts"][1]["action"]["action_type"] == "mutate_pipeline"


def test_pilot_controller_next_attempt_avoids_failed_llm_checker_execution_error(tmp_path, monkeypatch):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeCheckerExecutionErrorExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=FakeLLMProvider([
            {"action_type": "propose_pipeline", "reason": "Initial attempt."},
            {
                "goal_satisfied": False,
                "score": 0.56,
                "failure_type": "insufficient_security_quality",
                "capability_gap": {},
                "recommended_next_action": "mutate_pipeline",
                "reason": "Needs improvement.",
            },
            {"action_type": "propose_pipeline", "reason": "Try again."},
            {
                "goal_satisfied": False,
                "score": 0.56,
                "failure_type": "insufficient_security_quality",
                "capability_gap": {},
                "recommended_next_action": "mutate_pipeline",
                "reason": "Needs improvement.",
            },
        ]),
    )

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeUnsafeLLMCheckerAgent())

    response = controller.execute(
        task="run security audit on security_audit_samples",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        budget_steps=2,
        allow_experimental_tools=False,
        ask_user=False,
    )

    assert response["status"] == "budget_exhausted"
    assert response["attempts"][0]["judge"]["failure_type"] == "llm_checker_execution_error"
    assert "HarmfulContentLLMJudge" in response["attempts"][0]["pipeline"]
    assert "HarmfulContentLLMJudge" not in response["attempts"][1]["pipeline"]
    assert response["attempts"][1]["action"]["action_type"] == "mutate_pipeline"


def test_security_audit_hints_only_include_runtime_available_checkers(tmp_path):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    recommendation_sets = controller._security_checker_recommendation_sets(
        "run security_audit on security_audit_samples with a custom checker set"
    )

    assert "JailbreakLLMJudge" not in recommendation_sets["stronger"]
    assert "PromptInjectionLLMJudge" not in recommendation_sets["stronger"]


def test_security_audit_logging_stabilizer_uses_generic_completion_log():
    import agentic.controller as controller_module

    pipeline = (
        'log_step("Loading dataset")\n\n'
        'data = load_dataset("security_audit_samples")\n\n'
        'audit_result = run_tool(\n'
        '    "security_audit",\n'
        '    data=data,\n'
        '    checker_names=["PIIRule", "SecretRule"]\n'
        ')\n\n'
        'log_step(f"Audit completed: flagged={result.get(\'flagged_samples\')} total={result.get(\'total_samples\')} score={result.get(\'security_score\')}")\n\n'
        "save_result(audit_result)\n"
    )

    stabilized = controller_module._stabilize_security_audit_result_logging(pipeline)

    assert 'log_step("Completed tool: security_audit")' in stabilized
    assert "flagged_samples" not in stabilized
    assert "total_samples" not in stabilized
    assert "security_score" not in stabilized
    assert "{result.get('flagged_samples')}" not in stabilized


def test_pilot_controller_emits_stage_started_events(tmp_path, monkeypatch):
    cfg = Config()
    cfg.agent.type = "mock"
    cfg.agent.model = "fake-model"

    job_manager = JobManager(jobs_dir=tmp_path / ".jobs")
    registry = get_global_registry()
    registry.clear()
    registry.register(DummyAuditTool())

    asset_manager = AssetManager(root=tmp_path / ".elf")
    controller = PilotController(
        config=cfg,
        job_manager=job_manager,
        executor=FakeSuccessExecutor(),
        registry=registry,
        asset_manager=asset_manager,
        llm_provider=None,
    )

    monkeypatch.setattr("agentic.controller.create_agent_adapter", lambda *args, **kwargs: FakeBroadPoorSecurityAgent())

    events: list[dict] = []
    response = controller.execute(
        task="run security audit",
        dataset_schemas={"security_audit_samples": ["id", "messages"]},
        budget_steps=1,
        allow_experimental_tools=False,
        event_handler=events.append,
    )

    started_stages = [event.get("stage") for event in events if event.get("type") == "stage_started"]
    completed_stages = [event.get("stage") for event in events if event.get("type") == "stage_completed"]

    assert response["status"] == "success"
    assert started_stages == [
        "goal_clarification",
        "security_checker_clarification",
        "planner",
        "pipeline_generation",
        "execution",
        "judge",
    ]
    assert completed_stages == [
        "goal_clarification",
        "security_checker_clarification",
        "execution",
    ]
