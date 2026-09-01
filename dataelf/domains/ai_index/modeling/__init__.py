from __future__ import annotations

from dataelf.domains.ai_index.config import AIIndexDomainConfig
from dataelf.domains.ai_index.modeling.pipeline import AIIndexModeler


def create_modeler(config: AIIndexDomainConfig, runtime_env: dict[str, str]) -> AIIndexModeler | None:
    if not config.modeling.enabled:
        return None
    return AIIndexModeler(config, runtime_env)


__all__ = ["AIIndexModeler", "create_modeler"]
