from __future__ import annotations

import json
from abc import ABC, abstractmethod
from time import perf_counter
from typing import Any

from agent.prompt_builder import create_prompt_builder
from config import Config
from llm.provider import LLMProvider
from runtime.execution_plan import PLAN_VERSION


class AgentAdapter(ABC):
    def __init__(
        self,
        config: Config,
        skill_views: list[dict[str, Any]],
        dataset_schemas: dict[str, list[str]] | None = None,
        llm_provider: LLMProvider | None = None,
        skill_docs: list[dict[str, str]] | None = None,
    ) -> None:
        self.config = config
        self.skill_views = skill_views
        self.dataset_schemas = dataset_schemas or {}
        self.llm_provider = llm_provider
        self.skill_docs = skill_docs or []

    @abstractmethod
    def generate_execution_plan(self, task: str) -> tuple[str, dict[str, Any]]:
        pass


class MockAgentAdapter(AgentAdapter):
    def generate_execution_plan(self, task: str) -> tuple[str, dict[str, Any]]:
        task_lower = task.lower()
        if "security" in task_lower or "audit" in task_lower or "risk" in task_lower:
            plan = {
                "version": PLAN_VERSION,
                "steps": [
                    {"id": "load_data", "op": "load_dataset", "dataset": "security_audit_samples", "output": "data"},
                    {
                        "id": "audit",
                        "op": "invoke_skill",
                        "skill": "security_audit",
                        "input": {
                            "data": "$data",
                            "checker_names": ["PIIRule", "SecretRule", "ToxicityKeywordRule"],
                            "max_workers": 2,
                        },
                        "output": "audit_result",
                    },
                    {"id": "save", "op": "save_result", "input": "$audit_result"},
                ],
            }
        elif "protein" in task_lower or "enzyme" in task_lower or "science" in task_lower:
            plan = {
                "version": PLAN_VERSION,
                "steps": [
                    {
                        "id": "enzyme_lookup",
                        "op": "invoke_skill",
                        "skill": "enzyme_acquire",
                        "input": {"data": ["hexokinase"]},
                        "output": "result",
                    },
                    {"id": "save", "op": "save_result", "input": "$result"},
                ],
            }
        else:
            plan = {
                "version": PLAN_VERSION,
                "steps": [
                    {"id": "load_data", "op": "load_dataset", "dataset": "companies", "output": "data"},
                    {"id": "save", "op": "save_result", "input": "$data"},
                ],
            }

        plan_json = json.dumps(plan, ensure_ascii=False, indent=2)
        return plan_json, {
            "model": "mock",
            "elapsed_seconds": 0.0,
            "prompt_length": 0,
            "response_length": len(plan_json),
            "raw_response": plan_json,
        }


class OpenCodeAgentAdapter(AgentAdapter):
    def __init__(
        self,
        config: Config,
        skill_views: list[dict[str, Any]],
        dataset_schemas: dict[str, list[str]] | None = None,
        llm_provider: LLMProvider | None = None,
        skill_docs: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(
            config,
            skill_views,
            dataset_schemas,
            llm_provider=llm_provider,
            skill_docs=skill_docs,
        )
        self.api_key = config.agent.api_key
        self.model = config.agent.model
        self.base_url = config.agent.base_url

    def _build_messages(self, task: str) -> tuple[str, str]:
        builder = create_prompt_builder(
            skill_views=self.skill_views,
            dataset_schemas=self.dataset_schemas,
            skill_docs=self.skill_docs,
        )
        return builder.build_messages(task)

    def generate_execution_plan(self, task: str) -> tuple[str, dict[str, Any]]:
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
        plan_json = self._parse_response(response)

        return plan_json, {
            "model": self.model,
            "elapsed_seconds": round(elapsed, 2),
            "prompt_length": len(system_prompt) + len(user_prompt),
            "response_length": len(response),
            "raw_response": response,
        }

    def _parse_response(self, response: str) -> str:
        text = response.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1]
                if text.startswith("json"):
                    text = text[len("json"):]
        return text.strip()


def create_agent_adapter(
    config: Config,
    skill_views: list[dict[str, Any]],
    dataset_schemas: dict[str, list[str]] | None = None,
    llm_provider: LLMProvider | None = None,
    skill_docs: list[dict[str, str]] | None = None,
) -> AgentAdapter:
    agent_type = config.agent.type.lower()
    if agent_type == "mock":
        return MockAgentAdapter(
            config,
            skill_views,
            dataset_schemas,
            llm_provider=llm_provider,
            skill_docs=skill_docs,
        )
    if agent_type == "opencode":
        return OpenCodeAgentAdapter(
            config,
            skill_views,
            dataset_schemas,
            llm_provider=llm_provider,
            skill_docs=skill_docs,
        )
    raise ValueError(f"Unsupported agent type: {agent_type}")
