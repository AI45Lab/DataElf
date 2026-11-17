"""One-way configuration migration; no legacy ontology execution lives here.

Only execution controls supported by the common runtime are translated. Old
source hints, prompts, Pi setup and output layouts no longer control modeling.
"""

from pathlib import Path
import warnings

import yaml

from dataelf.ontology.config import (
    DEFAULT_ONTOLOGY_CONFIG,
    OntologyConfig,
    RoleConfig,
    StageConfig,
    Stage1Config,
    load_config,
)
from dataelf.ontology.contracts import StrictModel
from pydantic import Field

LEGACY_DEFAULT = Path(__file__).resolve().parent / "modeling/ontology/config.yaml"
LEGACY_LOCAL = Path(__file__).with_name("config.legacy.local.yaml")
# Read-only fallback for configurations saved before this adapter moved.
PREVIOUS_LEGACY_LOCAL = DEFAULT_ONTOLOGY_CONFIG.with_name("config.legacy.local.yaml")


class _LegacyOptions(StrictModel):
    ontology_template: str | None = None
    raw_page_size: int = Field(default=50, ge=1, le=50)
    worker_timeout_seconds: int = Field(default=9120, ge=1)


def resolve_config_path(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    if target == LEGACY_DEFAULT and not target.exists():
        warnings.warn(
            "The old ontology config path moved to dataelf/ontology/config.yaml",
            DeprecationWarning,
            stacklevel=2,
        )
        for candidate in (LEGACY_LOCAL, PREVIOUS_LEGACY_LOCAL):
            if candidate.is_file():
                return candidate
        return DEFAULT_ONTOLOGY_CONFIG
    return target


def _mapping(value, label, allowed=None):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    if allowed is not None and (unknown := set(value) - set(allowed)):
        raise ValueError(f"Unknown {label} config keys: {', '.join(sorted(unknown))}")
    return value


def _role(raw, label):
    raw = _mapping(raw, label)
    # Historical request limits cannot become invalid silently during migration.
    for key in (
        "process_timeout_seconds",
        "request_timeout_seconds",
        "timeout_seconds",
    ):
        if key in raw and int(raw[key]) < 1:
            raise ValueError(f"{label}.{key} must be positive")
    if not 0 <= int(raw.get("request_max_retries", 3)) <= 10:
        raise ValueError(f"{label}.request_max_retries must be between 0 and 10")
    return RoleConfig(
        model=raw.get("name"),
        timeout_seconds=raw.get(
            "process_timeout_seconds", raw.get("timeout_seconds", 1800)
        ),
    )


def convert_legacy_config(raw: dict) -> OntologyConfig:
    """Translate the retired unified schema without importing retired parsers."""
    _mapping(
        raw,
        "ontology",
        {
            "ontology_template",
            "raw_page_size",
            "worker_timeout_seconds",
            "stage1",
            "stage2",
        },
    )
    options = _LegacyOptions.model_validate(
        {k: v for k, v in raw.items() if k not in {"stage1", "stage2"}}
    )
    stages = []
    for name, generator, allowed in (
        (
            "stage1",
            "generator",
            {
                "source",
                "ontology",
                "generator",
                "reviewer",
                "pi",
                "quality",
                "checkpoint",
                "artifacts",
            },
        ),
        (
            "stage2",
            "compiler",
            {
                "compiler",
                "reviewer",
                "output",
                "vocabulary",
                "quality",
                "total_stage_timeout_seconds",
            },
        ),
    ):
        stage = _mapping(raw.get(name), name, allowed)
        if not stage:
            raise ValueError(f"{name} must not be empty")
        for key, value in stage.items():
            if key != "total_stage_timeout_seconds":
                _mapping(value, f"{name}.{key}")
        quality = stage.get("quality", {})
        if not 1 <= int(quality.get("max_repair_rounds", 5)) <= 10:
            raise ValueError(
                f"{name}.quality.max_repair_rounds must be between 1 and 10"
            )
        if int(stage.get("total_stage_timeout_seconds", 1800)) < 1:
            raise ValueError(f"{name}.total_stage_timeout_seconds must be positive")
        stages.append(
            StageConfig(
                generator=_role(stage.get(generator, {}), f"{name}.{generator}"),
                reviewer=_role(stage.get("reviewer", {}), f"{name}.reviewer"),
            )
        )
    template = (options.ontology_template or "").strip()
    if template and template != "ai_index_search":
        raise ValueError(f"unknown ontology template: {template}")
    warnings.warn(
        "Legacy ontology config is deprecated: only template, model names, process timeouts, "
        "Stage1 repair budget and overall deadline are migrated. Source/semantic hints, old Pi "
        "setup, request options and output layouts are retired; use the common config and main Pi registry.",
        DeprecationWarning,
        stacklevel=2,
    )
    return OntologyConfig(
        mode="template" if template else "dynamic",
        templates=(
            [
                Path(__file__).resolve().parent
                / "templates/ai_index_search/template.json"
            ]
            if template
            else []
        ),
        timeout_seconds=options.worker_timeout_seconds,
        max_repairs=raw["stage1"].get("quality", {}).get("max_repair_rounds", 5),
        stage1=Stage1Config(**stages[0].model_dump()),
        stage2=stages[1],
    )


def load_modeling_config(path: str | Path) -> OntologyConfig:
    target = resolve_config_path(path)
    if not target.exists():
        return load_config(target)
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "ontology_template" in raw:
        return convert_legacy_config(raw)
    return load_config(target)


def collection_page_size(source, path):
    """Preserve historical collection sizing at the AI Index caller boundary."""
    if "page_size" not in source.model_fields_set:
        target = resolve_config_path(path)
        if target.is_file():
            raw = yaml.safe_load(target.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "raw_page_size" in raw:
                size = _LegacyOptions.model_validate(
                    {"raw_page_size": raw["raw_page_size"]}
                ).raw_page_size
                warnings.warn(
                    "raw_page_size moved to domains.ai_index.source.page_size",
                    DeprecationWarning,
                    stacklevel=2,
                )
                return size
    return source.page_size


import os
from dataelf.ontology.config import OntologySettings


def resolve_ontology_settings(self):
    """Resolve legacy AI settings only after the AI workflow is selected."""
    legacy = dict(self.domains.get("ai_index", {}).get("modeling", {}) or {})
    for field, env in (
        ("enabled", "DATAELF_AI_INDEX_MODELING_ENABLED"),
        ("ontology_config", "DATAELF_AI_INDEX_MODELING_ONTOLOGY_CONFIG"),
    ):
        if os.getenv(env) not in (None, ""):
            legacy[field] = os.environ[env]
    if not legacy:
        return self.ontology
    old = OntologySettings.model_validate(legacy)
    if "ontology" in self.model_fields_set:
        for key in legacy:
            left, right = getattr(old, key), getattr(self.ontology, key)
            if key == "ontology_config":
                left, right = Path(left).resolve(), Path(right).resolve()
            if left != right:
                raise ValueError(
                    f"conflicting ontology and domains.ai_index.modeling settings: {key}"
                )
        return self.ontology
    warnings.warn(
        "domains.ai_index.modeling is deprecated; use root ontology for the explicit research workflow",
        DeprecationWarning,
        stacklevel=2,
    )
    return old
