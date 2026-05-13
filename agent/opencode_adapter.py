from abc import ABC, abstractmethod
from pathlib import Path
from time import perf_counter
from typing import Any

from config import Config
from agent.prompt_builder import create_prompt_builder
from llm.provider import LLMProvider


class AgentAdapter(ABC):

    def __init__(
        self,
        config: Config,
        tool_schemas: list[dict[str, Any]],
        dataset_schemas: dict[str, list[str]] | None = None,
        llm_provider: LLMProvider | None = None,
    ):
        self.config = config
        self.tool_schemas = tool_schemas
        self.dataset_schemas = dataset_schemas or {}
        self.llm_provider = llm_provider

    @abstractmethod
    def generate_pipeline(self, task: str) -> tuple[str, dict[str, Any]]:
        pass


class MockAgentAdapter(AgentAdapter):
    def generate_pipeline(self, task: str) -> tuple[str, dict[str, Any]]:
        task_lower = task.lower()
        if "security" in task_lower or "audit" in task_lower or "risk" in task_lower:
            pipeline = '''log_step("Loading security audit samples")

data = load_dataset("security_audit_samples")

audit_result = run_tool(
    "security_audit",
    data=data,
    checker_names=["PIIRule", "SecretRule", "ToxicityKeywordRule"],
    max_workers=2
)

save_result(audit_result)'''
        elif "protein" in task_lower or "enzyme" in task_lower or "science" in task_lower:
            pipeline = '''log_step("Running scientific acquisition workflow")

result = run_tool(
    "enzyme_acquire",
    data=["hexokinase"]
)

save_result(result)'''
        else:
            pipeline = '''log_step("Loading company dataset")

data = load_dataset("companies")

save_result({
    "records": len(data),
    "dataset": "companies"
})'''

        metadata = {
            "model": "mock",
            "elapsed_seconds": 0.0,
            "prompt_length": 0,
            "response_length": len(pipeline),
            "raw_response": pipeline,
        }
        return pipeline, metadata


class OpenCodeAgentAdapter(AgentAdapter):

    def __init__(
        self,
        config: Config,
        tool_schemas: list[dict[str, Any]],
        dataset_schemas: dict[str, list[str]] | None = None,
        llm_provider: LLMProvider | None = None,
    ):
        super().__init__(config, tool_schemas, dataset_schemas, llm_provider=llm_provider)
        self.api_key = config.agent.api_key
        self.model = config.agent.model
        self.base_url = config.agent.base_url

    def _get_spec_paths(self) -> tuple[str, str]:
        docs_dir = Path(__file__).parent.parent / "docs"
        dsl_spec_path = str(docs_dir / "pipeline_dsl_spec.md")
        tool_spec_path = str(docs_dir / "tool_spec.md")
        return dsl_spec_path, tool_spec_path

    def _build_messages(self, task: str) -> tuple[str, str]:
        dsl_spec_path, tool_spec_path = self._get_spec_paths()

        # Determine tool names for README loading
        tool_names = []
        tool_readmes_max_length = 2000
        if self.config.agent.include_tool_readmes:
            tool_names = self.config.tools
            tool_readmes_max_length = self.config.agent.tool_readmes_max_length

        builder = create_prompt_builder(
            tool_schemas=self.tool_schemas,
            dsl_spec_path=dsl_spec_path,
            tool_spec_path=tool_spec_path,
            dataset_schemas=self.dataset_schemas,
            tool_names=tool_names,
            tool_readmes_max_length=tool_readmes_max_length,
        )

        return builder.build_messages(task)

    def generate_pipeline(self, task: str) -> tuple[str, dict[str, Any]]:
        system_prompt, user_prompt = self._build_messages(task)

        from llm import OpenAIProvider

        provider = self.llm_provider or OpenAIProvider(
            api_key=self.api_key,
            base_url=self.base_url,
            max_retries=self.config.agent.max_retries,
            retry_delay=self.config.agent.retry_delay,
        )

        start_time = perf_counter()
        response = provider.generate(
            model=self.model,
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
        elapsed = perf_counter() - start_time

        pipeline_code = self._parse_response(response)

        metadata = {
            "model": self.model,
            "elapsed_seconds": round(elapsed, 2),
            "prompt_length": len(system_prompt) + len(user_prompt),
            "response_length": len(response),
            "raw_response": response,
        }

        return pipeline_code, metadata

    def _parse_response(self, response: str) -> str:
        code = response.strip()

        # Handle markdown code blocks
        if "```python" in code:
            code = code.split("```python", 1)[1].split("```", 1)[0]
        elif "```" in code:
            # Check if there's a language identifier
            parts = code.split("```")
            if len(parts) >= 3:
                code = parts[1]
                # Remove language identifier if present
                if code.startswith("python"):
                    code = code[len("python"):]
            else:
                code = parts[1] if len(parts) > 1 else code

        return code.strip()


def create_agent_adapter(
    config: Config,
    tool_schemas: list[dict[str, Any]],
    dataset_schemas: dict[str, list[str]] | None = None,
    llm_provider: LLMProvider | None = None,
) -> AgentAdapter:
    agent_type = config.agent.type.lower()
    if agent_type == "mock":
        return MockAgentAdapter(config, tool_schemas, dataset_schemas, llm_provider=llm_provider)
    if agent_type == "opencode":
        return OpenCodeAgentAdapter(config, tool_schemas, dataset_schemas, llm_provider=llm_provider)
    raise ValueError(f"Unsupported agent type: {agent_type}")
