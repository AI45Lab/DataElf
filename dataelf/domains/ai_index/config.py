from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


DEFAULT_AI_INDEX_BASE_URL = "https://index.shlab.org.cn/api/v2"
DEFAULT_AI_INDEX_API_KEY = "ak_0XWHy2OQpSKnaKHL"
DEFAULT_AI_INDEX_MODE = "api"
AI_INDEX_FIXTURE_FILES = ("papers.json", "institutions.json", "scholars.json")
AI_INDEX_ONTOLOGY_ROOT = Path(__file__).resolve().parent / "modeling" / "ontology"
DEFAULT_AI_INDEX_STAGE1_CONFIG = AI_INDEX_ONTOLOGY_ROOT / "stage1/config.yaml"
DEFAULT_AI_INDEX_STAGE2_CONFIG = AI_INDEX_ONTOLOGY_ROOT / "stage2/config.yaml"


class AIIndexSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["api", "fixture"] = DEFAULT_AI_INDEX_MODE
    base_url: str = DEFAULT_AI_INDEX_BASE_URL
    api_key: str = DEFAULT_AI_INDEX_API_KEY
    fixtures_dir: Path = Field(default_factory=lambda: Path("fixtures/ai_index"))

    def validate_for_run(self) -> None:
        if self.mode == "api":
            base_url = self.base_url.strip()
            parsed = urlsplit(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("domains.ai_index.source.base_url must be a valid HTTP(S) URL in api mode")
            if not self.api_key.strip():
                raise ValueError("domains.ai_index.source.api_key is required in api mode")
            return
        if not self.fixtures_dir.is_dir():
            raise ValueError(
                f"domains.ai_index.source.fixtures_dir does not exist or is not a directory: {self.fixtures_dir}"
            )
        missing = [name for name in AI_INDEX_FIXTURE_FILES if not (self.fixtures_dir / name).is_file()]
        if missing:
            raise ValueError(
                "domains.ai_index.source.fixtures_dir is missing required files: " + ", ".join(missing)
            )


class AIIndexModelingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    ontology_template: str | None = None
    stage1_config: Path = Field(default_factory=lambda: DEFAULT_AI_INDEX_STAGE1_CONFIG)
    stage2_config: Path = Field(default_factory=lambda: DEFAULT_AI_INDEX_STAGE2_CONFIG)
    raw_page_size: int = Field(default=50, ge=1, le=50)
    model_name: str | None = None
    model_max_tokens: int | None = Field(default=None, ge=128)
    stage1_process_timeout_seconds: int = Field(default=7200, ge=30)
    stage1_request_timeout_seconds: int = Field(default=900, ge=30)
    stage1_request_max_retries: int = Field(default=3, ge=0, le=10)
    stage2_request_timeout_seconds: int = Field(default=600, ge=30)
    stage2_request_max_retries: int = Field(default=3, ge=0, le=10)
    stage2_total_timeout_seconds: int = Field(default=1800, ge=30)

    @field_validator("ontology_template", "model_name", mode="before")
    @classmethod
    def normalize_optional_strings(cls, value: Any) -> str | None:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value


class AIIndexDomainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: AIIndexSourceConfig = Field(default_factory=AIIndexSourceConfig)
    modeling: AIIndexModelingConfig = Field(default_factory=AIIndexModelingConfig)

    def validate_for_run(self) -> None:
        self.source.validate_for_run()
        if not self.modeling.enabled:
            return

        errors: list[str] = []
        if not self.modeling.stage1_config.is_file():
            errors.append(f"stage1_config is not a file: {self.modeling.stage1_config}")
        if not self.modeling.stage2_config.is_file():
            errors.append(f"stage2_config is not a file: {self.modeling.stage2_config}")
        if errors:
            raise ValueError("Invalid domains.ai_index.modeling configuration: " + "; ".join(errors))

        try:
            from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import (
                load_config as load_stage1_config,
            )
            from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import (
                load_config as load_stage2_config,
            )

            load_stage1_config(self.modeling.stage1_config)
            stage2 = load_stage2_config(self.modeling.stage2_config)
            if not stage2.stage1_config.is_file():
                raise ValueError(f"Stage 2 stage1_config is not a file: {stage2.stage1_config}")
        except Exception as exc:
            raise ValueError(f"Invalid ontology Stage configuration: {exc}") from exc

        if self.modeling.ontology_template:
            try:
                from dataelf.domains.ai_index.modeling.template import load_template

                load_template(self.modeling.ontology_template)
            except Exception as exc:
                raise ValueError(f"Invalid ontology template configuration: {exc}") from exc

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "AIIndexDomainConfig":
        unknown = sorted(set(values) - {"source", "modeling"})
        if unknown:
            raise ValueError(f"Unknown AI Index config keys: {', '.join(unknown)}")
        raw_source = values.get("source", {})
        raw_modeling = values.get("modeling", {})
        if not isinstance(raw_source, dict) or not isinstance(raw_modeling, dict):
            raise ValueError("domains.ai_index.source and modeling must be mappings")
        source = dict(raw_source)
        modeling = dict(raw_modeling)
        source.update({
            "mode": os.getenv("DATAELF_AI_INDEX_MODE", source.get("mode", DEFAULT_AI_INDEX_MODE)),
            "base_url": os.getenv("AI_INDEX_BASE_URL", source.get("base_url", DEFAULT_AI_INDEX_BASE_URL)),
            "api_key": os.getenv("AI_INDEX_API_KEY", source.get("api_key", DEFAULT_AI_INDEX_API_KEY)),
            "fixtures_dir": os.getenv("DATAELF_FIXTURES_DIR", source.get("fixtures_dir", "fixtures/ai_index")),
        })
        env_fields = {
            "enabled": "DATAELF_AI_INDEX_MODELING_ENABLED",
            "ontology_template": "DATAELF_AI_INDEX_MODELING_ONTOLOGY_TEMPLATE",
            "stage1_config": "DATAELF_AI_INDEX_MODELING_STAGE1_CONFIG",
            "stage2_config": "DATAELF_AI_INDEX_MODELING_STAGE2_CONFIG",
            "raw_page_size": "DATAELF_AI_INDEX_MODELING_RAW_PAGE_SIZE",
            "model_name": "DATAELF_AI_INDEX_MODELING_MODEL_NAME",
            "model_max_tokens": "DATAELF_AI_INDEX_MODELING_MODEL_MAX_TOKENS",
            "stage1_process_timeout_seconds": "DATAELF_AI_INDEX_MODELING_STAGE1_PROCESS_TIMEOUT_SECONDS",
            "stage1_request_timeout_seconds": "DATAELF_AI_INDEX_MODELING_STAGE1_REQUEST_TIMEOUT_SECONDS",
            "stage1_request_max_retries": "DATAELF_AI_INDEX_MODELING_STAGE1_REQUEST_MAX_RETRIES",
            "stage2_request_timeout_seconds": "DATAELF_AI_INDEX_MODELING_STAGE2_REQUEST_TIMEOUT_SECONDS",
            "stage2_request_max_retries": "DATAELF_AI_INDEX_MODELING_STAGE2_REQUEST_MAX_RETRIES",
            "stage2_total_timeout_seconds": "DATAELF_AI_INDEX_MODELING_STAGE2_TOTAL_TIMEOUT_SECONDS",
        }
        for field, env_name in env_fields.items():
            value = os.getenv(env_name)
            if value not in (None, ""):
                modeling[field] = value
        return cls.model_validate({"source": source, "modeling": modeling})


__all__ = [
    "AI_INDEX_ONTOLOGY_ROOT", "AIIndexDomainConfig", "AIIndexModelingConfig", "AIIndexSourceConfig",
    "DEFAULT_AI_INDEX_API_KEY", "DEFAULT_AI_INDEX_BASE_URL", "DEFAULT_AI_INDEX_MODE",
    "DEFAULT_AI_INDEX_STAGE1_CONFIG", "DEFAULT_AI_INDEX_STAGE2_CONFIG",
]
