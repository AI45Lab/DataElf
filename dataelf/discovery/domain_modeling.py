from __future__ import annotations

import importlib
from typing import Protocol

from dataelf.config import DataElfConfig
from dataelf.discovery.base import DiscoveryContext, ModelingStageResult
from dataelf.schemas import DiscoveryJob


class DomainModeler(Protocol):
    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> ModelingStageResult: ...


def create_domain_modeler(domain_pack: dict[str, object], config: DataElfConfig) -> DomainModeler | None:
    modeling = domain_pack.get("modeling")
    if not isinstance(modeling, dict):
        return None
    entrypoint = modeling.get("entrypoint")
    if not isinstance(entrypoint, str) or ":" not in entrypoint:
        raise ValueError("domain modeling.entrypoint must use 'module:function' syntax")
    module_name, factory_name = entrypoint.split(":", 1)
    factory = getattr(importlib.import_module(module_name), factory_name)
    modeler = factory(config)
    return modeler


__all__ = ["DomainModeler", "create_domain_modeler"]
