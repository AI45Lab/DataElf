from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


DEFAULT_AI_INDEX_BASE_URL = "https://index.shlab.org.cn/api/v2"
DEFAULT_AI_INDEX_API_KEY = ""
DEFAULT_AI_INDEX_MODE = "api"
AI_INDEX_FIXTURE_FILES = ("papers.json", "institutions.json", "scholars.json")
AI_INDEX_ONTOLOGY_ROOT = Path(__file__).resolve().parent / "modeling" / "ontology"
DEFAULT_AI_INDEX_ONTOLOGY_CONFIG = AI_INDEX_ONTOLOGY_ROOT / "config.yaml"


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
    ontology_config: Path = Field(default_factory=lambda: DEFAULT_AI_INDEX_ONTOLOGY_CONFIG)

    @field_validator("ontology_config", mode="before")
    @classmethod
    def normalize_config_path(cls, value: Any) -> Path:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("ontology_config must be a non-empty file path")
        return Path(str(value).strip()).expanduser().resolve()


class AIIndexDomainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: AIIndexSourceConfig = Field(default_factory=AIIndexSourceConfig)
    modeling: AIIndexModelingConfig = Field(default_factory=AIIndexModelingConfig)

    def validate_for_run(self) -> None:
        self.source.validate_for_run()
        if not self.modeling.enabled:
            return

        if not self.modeling.ontology_config.is_file():
            raise ValueError(f"ontology_config is not a file: {self.modeling.ontology_config}")
        try:
            from dataelf.domains.ai_index.modeling.ontology.config import load_config

            ontology = load_config(self.modeling.ontology_config)
            if ontology.ontology_template:
                from dataelf.domains.ai_index.modeling.template import load_template

                load_template(ontology.ontology_template)
        except Exception as exc:
            raise ValueError(f"Invalid ontology configuration: {exc}") from exc

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
            "ontology_config": "DATAELF_AI_INDEX_MODELING_ONTOLOGY_CONFIG",
        }
        for field, env_name in env_fields.items():
            value = os.getenv(env_name)
            if value not in (None, ""):
                modeling[field] = value
        return cls.model_validate({"source": source, "modeling": modeling})


__all__ = [
    "AI_INDEX_ONTOLOGY_ROOT", "AIIndexDomainConfig", "AIIndexModelingConfig", "AIIndexSourceConfig",
    "DEFAULT_AI_INDEX_API_KEY", "DEFAULT_AI_INDEX_BASE_URL", "DEFAULT_AI_INDEX_MODE",
    "DEFAULT_AI_INDEX_ONTOLOGY_CONFIG",
]
