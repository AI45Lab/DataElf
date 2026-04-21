from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from llm.provider import LLMProvider


@dataclass
class ToolContext:
    job_id: str
    logger: Any
    config: dict[str, Any] = field(default_factory=dict)
    llm: "LLMProvider | None" = field(default=None)
    datasets: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def log(self, message: str, level: str = "info") -> None:
        log_func = getattr(self.logger, level.lower(), self.logger.info)
        log_func(message)


class BaseTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        pass

    @abstractmethod
    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        pass

    def usage_example(self) -> str:
        return f'run_tool("{self.name}", data=data)'

    def get_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "usage_example": self.usage_example(),
        }

    def validate_parameters(self, **kwargs: Any) -> bool:
        properties = self.parameters.get("properties", {})
        required = set(self.parameters.get("required", []))

        for param in required:
            if param not in kwargs:
                raise ValueError(f"Missing required parameter: {param}")

        for param in kwargs:
            if param not in properties:
                raise ValueError(f"Unknown parameter: {param}")

        return True
