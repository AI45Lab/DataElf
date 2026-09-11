from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dataelf.domains.ai_index.modeling.ontology.common.config import DEFAULT_ONTOLOGY_CONFIG, read_config
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config, parse_config as parse_stage1
from dataelf.domains.ai_index.modeling.ontology.stage2.ontology_stage2.config import Stage2Config, parse_config as parse_stage2


class _RunOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ontology_template: str | None = None
    raw_page_size: int = Field(default=50, ge=1, le=50)
    # Overall worker deadline, including both stages and publication.
    worker_timeout_seconds: int = Field(default=9120, ge=1)

    @field_validator("ontology_template", mode="before")
    @classmethod
    def normalize_template(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value


@dataclass(frozen=True)
class OntologyPipelineConfig:
    path: Path
    ontology_template: str | None
    raw_page_size: int
    worker_timeout_seconds: int
    stage1: Stage1Config
    stage2: Stage2Config


def load_config(path: str | Path = DEFAULT_ONTOLOGY_CONFIG) -> OntologyPipelineConfig:
    target, root = read_config(path)
    options = _RunOptions.model_validate({key: value for key, value in root.items() if key not in {"stage1", "stage2"}})
    return OntologyPipelineConfig(
        path=target,
        **options.model_dump(),
        stage1=parse_stage1(target, root["stage1"]),
        stage2=parse_stage2(target, root["stage2"]),
    )
