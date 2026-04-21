from typing import Any


class ModelRegistry:
    def __init__(self):
        self._models: dict[str, dict[str, Any]] = {}

    def register(self, name: str, provider: str, model: str, **config: Any) -> None:
        self._models[name] = {"provider": provider, "model": model, **config}

    def get(self, name: str) -> dict[str, Any] | None:
        return self._models.get(name)

    def list_models(self) -> list[str]:
        return list(self._models.keys())


_global_registry: ModelRegistry | None = None


def get_model_registry() -> ModelRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = ModelRegistry()
        _init_default_models(_global_registry)
    return _global_registry


def _init_default_models(registry: ModelRegistry) -> None:
    registry.register(
        "agent_reasoning",
        provider="openai",
        model="gpt-4o",
    )
    registry.register(
        "judge",
        provider="openai",
        model="gpt-4o-mini",
    )
