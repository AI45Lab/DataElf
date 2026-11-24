from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from .contracts import StrictModel


DEFAULT_ONTOLOGY_CONFIG = Path(__file__).with_name("config.yaml")


class OntologySettings(StrictModel):
    enabled: bool = False
    maintenance_review_policy: Literal['required','deterministic'] = 'required'
    ontology_config: Path = Field(default_factory=lambda: DEFAULT_ONTOLOGY_CONFIG)

    @field_validator("ontology_config", mode="before")
    @classmethod
    def config_path(cls, value):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("ontology_config must be a non-empty path")
        return Path(str(value).strip()).expanduser().resolve()


class RoleConfig(StrictModel):
    component_tools: bool = True
    author_interface: Literal['union','split'] = 'union'
    tool_choice: Literal['auto', 'required', 'named'] = 'auto'
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    stream_usage: bool | None = None
    max_narrative_characters: int | None = Field(default=None, ge=128)
    max_requests: int | None = Field(default=60, ge=1)
    max_no_progress: int | None = Field(default=None, ge=1)
    max_recovery_no_progress: int | None = Field(default=None, ge=1)
    max_submission_failures: int | None = Field(default=3, ge=1)
    max_format_failures: int | None = Field(default=6, ge=1)
    max_identical_failures: int | None = Field(default=3, ge=1)
    model: str | None = None
    timeout_seconds: int | None = Field(default=1800, ge=1)

    @field_validator('tool_choice',mode='before')
    @classmethod
    def retire_named_router(cls,value):
        if value=='named':
            import warnings
            warnings.warn('tool_choice=named and the step router are retired; use auto. Explicit required remains an optional provider setting.',DeprecationWarning,stacklevel=2)
            return 'auto'
        return value


class StageConfig(StrictModel):
    generator: RoleConfig = Field(default_factory=RoleConfig)
    reviewer: RoleConfig = Field(default_factory=RoleConfig)


class Stage1Config(StageConfig):
    save_as_template: bool = False


class DeliveryConfig(StrictModel):
    enabled: bool = True
    closing_reserve_seconds: int = Field(default=120, ge=0)
    closing_reserve_requests: int = Field(default=4, ge=0)
    closing_reserve_tokens: int | None = Field(default=None, ge=1)
    reviewer_reserve_seconds: int = Field(default=300, ge=0)
    reviewer_reserve_requests: int = Field(default=4, ge=0)
    validation_reserve_seconds: int = Field(default=30, ge=0)
    minimum_session_seconds: int = Field(default=10, ge=1)
    stream_grace_seconds: int = Field(default=10, ge=0)
    max_closing_interrupts: int = Field(default=1, ge=0, le=1)
    first_artifact_notice_seconds: int = Field(default=180, ge=0)
    first_save_recovery_seconds: int = Field(default=180, ge=1)
    component_delivery_boundary: bool = False
    stagnation_repeats: int = Field(default=3, ge=2)
    feedback_cooldown_requests: int = Field(default=3, ge=1)


class OntologyConfig(StrictModel):
    execution_policy: Literal["bounded","unbounded"] = "bounded"
    mode: Literal["dynamic", "template"] = "dynamic"
    templates: list[Path] = Field(default_factory=list)
    max_repairs: int | None = Field(default=5, ge=0)
    max_format_repairs: int | None = Field(default=1, ge=0)
    max_control_restarts: int | None = Field(default=3, ge=0, le=8)
    max_llm_requests: int | None = Field(default=160, ge=1)
    max_llm_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=9120, ge=1)
    output_subdir: str = "ontology"
    modeler: RoleConfig = Field(default_factory=RoleConfig)
    reviewer: RoleConfig = Field(default_factory=RoleConfig)
    save_as_template: bool = False
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    stage1: Stage1Config = Field(default_factory=Stage1Config)
    stage2: StageConfig = Field(default_factory=StageConfig)

    @model_validator(mode="before")
    @classmethod
    def migrate_roles(cls, raw):
        if not isinstance(raw, dict): return raw
        raw = dict(raw)
        if 'stage1' in raw or 'stage2' in raw:
            import warnings
            warnings.warn('Ontology stage1/stage2 configuration is deprecated: stage1.generator maps to modeler; stage2.reviewer (or stage1.reviewer) maps to reviewer; stage2.generator is retired.', DeprecationWarning, stacklevel=2)
            s1=raw.get('stage1') or {}; s2=raw.get('stage2') or {}
            if hasattr(s1,'model_dump'):s1=s1.model_dump()
            if hasattr(s2,'model_dump'):s2=s2.model_dump()
            raw.setdefault('modeler',s1.get('generator',{}))
            raw.setdefault('reviewer',s2.get('reviewer') or s1.get('reviewer',{}))
            raw.setdefault('save_as_template',s1.get('save_as_template',False))
        return raw

    @model_validator(mode="after")
    def check_output(self):
        if self.execution_policy=='unbounded':
            for name in ('timeout_seconds','max_llm_requests','max_llm_tokens','max_repairs','max_format_repairs','max_control_restarts'):setattr(self,name,None)
            for role in ('modeler','reviewer'):
                value=getattr(self,role)
                setattr(self,role,value.model_copy(update={**{name:None for name in ('timeout_seconds','max_requests','max_submission_failures','max_format_failures','max_identical_failures','max_narrative_characters','max_no_progress','max_recovery_no_progress')},'tool_choice':'auto'}))
            self.delivery=self.delivery.model_copy(update={'enabled':False,'component_delivery_boundary':False})
        elif any(getattr(self,k) is None for k in ('timeout_seconds','max_llm_requests','max_repairs','max_format_repairs','max_control_restarts')) or any(getattr(getattr(self,r),k) is None for r in ('modeler','reviewer') for k in ('timeout_seconds','max_requests','max_submission_failures','max_format_failures','max_identical_failures')):
            raise ValueError('Use execution_policy=unbounded for unlimited task/role limits')
        if Path(self.output_subdir).is_absolute() or ".." in Path(self.output_subdir).parts:
            raise ValueError("output_subdir must stay inside workspace")
        if self.mode == "dynamic" and self.templates:
            raise ValueError("templates require mode=template")
        if self.mode == "template" and self.save_as_template:
            raise ValueError("stage1.save_as_template requires mode=dynamic")
        return self


def load_config(path: str | Path | None = None) -> OntologyConfig:
    target = Path(path or DEFAULT_ONTOLOGY_CONFIG).expanduser().resolve()
    if target == DEFAULT_ONTOLOGY_CONFIG.resolve() and not target.exists():
        target = target.with_suffix(".yaml.example")
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("ontology config must be a mapping")
    result = OntologyConfig.model_validate(raw)
    result.templates = [(target.parent / p).resolve() if not p.is_absolute() else p for p in result.templates]
    return result
