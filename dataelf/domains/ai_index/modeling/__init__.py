from __future__ import annotations

from dataelf.config import DataElfConfig
from dataelf.domains.ai_index.modeling.pipeline import AIIndexModeler


def create_modeler(config: DataElfConfig) -> AIIndexModeler | None:
    if not config.ai_index_modeling.enabled:
        return None
    return AIIndexModeler(config)


__all__ = ["AIIndexModeler", "create_modeler"]
