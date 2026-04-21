from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    @abstractmethod
    def generate(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> str:
        pass

    @abstractmethod
    def generate_json(self, model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        pass
