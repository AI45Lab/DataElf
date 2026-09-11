from __future__ import annotations

import os
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dataelf.config import DataElfConfig
from dataelf.domains.ai_index.config import AIIndexDomainConfig
from dataelf_server.intent.config import IntentModelConfig


class ServerPiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: Literal["inherit", "nonstream"] = "inherit"
    synthesis_retry_timeout_seconds: int = Field(default=1800, ge=60)


class ServerSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_pages: int = Field(default=50, ge=1, le=1000)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    state_dir: Path | None = None
    max_concurrent_jobs: int = Field(default=5, ge=1, le=5)
    pi: ServerPiConfig = Field(default_factory=ServerPiConfig)
    intent: IntentModelConfig = Field(default_factory=IntentModelConfig)
    source: ServerSourceConfig = Field(default_factory=ServerSourceConfig)


@dataclass(frozen=True)
class Settings:
    core: DataElfConfig
    server: ServerConfig

    @classmethod
    def from_env(cls) -> "Settings":
        core = DataElfConfig.from_env()
        values = dict(core.server)
        for key in ("host", "port", "state_dir", "max_concurrent_jobs"):
            value = os.getenv(f"DATAELF_SERVER_{key.upper()}")
            if value is not None:
                values[key] = value
        pi = dict(values.get("pi") or {})
        for key in ("transport", "synthesis_retry_timeout_seconds"):
            value = os.getenv(f"DATAELF_SERVER_PI_{key.upper()}")
            if value is not None:
                pi[key] = value
        values["pi"] = pi
        source = dict(values.get("source") or {})
        if (max_pages := os.getenv("DATAELF_SERVER_SOURCE_MAX_PAGES")) is not None:
            source["max_pages"] = max_pages
        values["source"] = source
        return cls(core=core, server=ServerConfig.model_validate(values))

    @property
    def state_dir(self) -> Path:
        return (self.server.state_dir or self.core.runtime.workspace_dir / "server").resolve()

    @property
    def workspaces_dir(self) -> Path:
        return self.state_dir / "workspaces"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "jobs.sqlite"

    @property
    def project_root(self) -> Path:
        return (self.core.explorer.pi.cwd or Path(__file__).resolve().parents[1]).resolve()

    @property
    def domain(self) -> AIIndexDomainConfig:
        return AIIndexDomainConfig.from_mapping(self.core.domain_config("ai_index"))

    def validate_runtime(self) -> None:
        self.domain.source.validate_for_run()
        self.intent_config.validate_for_run()
        if not shutil.which("node"):
            raise ValueError("Node.js is required for DataElf Server Pi runtime")
        if self.core.explorer.pi.mode != "json":
            raise ValueError("DataElf Server requires explorer.pi.mode=json")
        for resource in ("prompts/research.md", "prompts/scope_v2_inputs.md", "runtime/server.ts", "runtime/nonstream_openai.ts"):
            if not (Path(__file__).parent / resource).is_file():
                raise ValueError(f"DataElf Server package resource is missing: {resource}")
        binary = self.core.explorer.pi.binary
        if binary:
            found = (self.project_root / binary).is_file() if os.sep in binary else bool(shutil.which(binary))
        else:
            found = (self.project_root / "node_modules/.bin/pi").is_file() or bool(shutil.which("pi"))
        if not found:
            raise ValueError("Pi CLI not found; install npm dependencies or configure explorer.pi.binary")
        from dataelf_server.ontology.scope_v2_template import compose_scope_v2_templates, SOURCE_ENDPOINTS
        compose_scope_v2_templates(SOURCE_ENDPOINTS)

    def execution_config(self) -> DataElfConfig:
        config = self.core.model_copy(deep=True)
        config.runtime.workspace_dir = self.state_dir
        config.runtime.workspaces_dir = self.workspaces_dir
        # API state belongs to JobStore; core artifacts remain the source of truth.
        config.runtime.enable_sqlite = False
        config.explorer.pi.cwd = self.project_root
        return config

    @property
    def intent_config(self) -> IntentModelConfig:
        return self.server.intent.resolve(self.core.env)
