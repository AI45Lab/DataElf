from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from dataelf.ontology.config import OntologySettings


DEFAULT_AI_INDEX_BASE_URL = "https://index.shlab.org.cn/api/v2"
DEFAULT_AI_INDEX_API_KEY = ""
DEFAULT_AI_INDEX_MODE = "api"
AI_INDEX_FIXTURE_FILES = ("papers.json", "institutions.json", "scholars.json")


class AIIndexSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["api", "fixture"] = DEFAULT_AI_INDEX_MODE
    base_url: str = DEFAULT_AI_INDEX_BASE_URL
    api_key: str = DEFAULT_AI_INDEX_API_KEY
    fixtures_dir: Path = Field(default_factory=lambda: Path("fixtures/ai_index"))
    page_size: int = Field(default=50, ge=1, le=50)

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


# Kept as the historical outer-config name; validation and defaults are shared.
AIIndexModelingConfig = OntologySettings


class AIIndexAnalysisConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    analyst_model: str | None = None
    reviewer_model: str | None = None
    analyst_max_output_tokens: int | None = Field(default=None,gt=0,strict=True)
    reviewer_max_output_tokens: int | None = Field(default=None,gt=0,strict=True)


class AIIndexDomainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: AIIndexSourceConfig = Field(default_factory=AIIndexSourceConfig)
    modeling: AIIndexModelingConfig = Field(default_factory=AIIndexModelingConfig)
    analysis: AIIndexAnalysisConfig = Field(default_factory=AIIndexAnalysisConfig)

    def validate_for_run(self) -> None:
        self.source.validate_for_run()
        if not self.modeling.enabled:
            return

        try:
            from dataelf.ontology.legacy_ai_index import load_modeling_config
            ontology = load_modeling_config(self.modeling.ontology_config)
            if ontology.mode == "template":
                raise ValueError('AI_INDEX_TEMPLATE_RETIRED: use dynamic ontology or --resume-graph-manifest for an approved revision; serve fixed rules and other domains are unchanged')
        except FileNotFoundError as exc:
            raise ValueError(f"ontology_config is not a file: {self.modeling.ontology_config}") from exc
        except Exception as exc:
            raise ValueError(f"Invalid ontology configuration: {exc}") from exc

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "AIIndexDomainConfig":
        unknown = sorted(set(values) - {"source", "modeling", "analysis"})
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
        return cls.model_validate({"source": source, "modeling": modeling, "analysis":values.get('analysis',{})})


__all__ = [
    "AIIndexDomainConfig", "AIIndexModelingConfig", "AIIndexSourceConfig",
    "DEFAULT_AI_INDEX_API_KEY", "DEFAULT_AI_INDEX_BASE_URL", "DEFAULT_AI_INDEX_MODE",
]
