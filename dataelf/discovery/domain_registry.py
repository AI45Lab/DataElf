from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml

from dataelf.discovery.contracts import DomainManifest, DomainPlugin


class DomainRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[1] / "domains"

    def load_manifest(self, domain: str) -> DomainManifest:
        path = self.root / domain / "domain.yaml"
        if not path.is_file():
            raise FileNotFoundError(f"Domain manifest not found: {path}")
        manifest = DomainManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        if manifest.domain != domain:
            raise ValueError(f"Domain manifest {path} declares {manifest.domain!r}, expected {domain!r}")
        return manifest

    def load_plugin(self, domain: str, config: Any) -> DomainPlugin:
        manifest = self.load_manifest(domain)
        module_name, separator, factory_name = manifest.plugin.partition(":")
        if not separator or not module_name or not factory_name:
            raise ValueError(f"Domain plugin must use 'module:function' syntax: {manifest.plugin!r}")
        factory = getattr(importlib.import_module(module_name), factory_name)
        plugin = factory(config, manifest)
        if plugin.manifest.domain != manifest.domain:
            raise ValueError(f"Domain plugin declares {plugin.manifest.domain!r}, expected {manifest.domain!r}")
        return plugin


__all__ = ["DomainRegistry"]
