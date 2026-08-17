from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "openai"
    name: str = "glm-5.2-1m"
    context_window: int = 200_000
    max_tokens: int = 16_384
    temperature: float = 0.0
    request_timeout_seconds: int = 600
    request_max_retries: int = 3
    api_key_env: str = "OPENAI_API_KEY"
    base_url_env: str = "OPENAI_BASE_URL"


@dataclass(frozen=True)
class OutputConfig:
    stable_suffix: str = ".rdf"
    rdfxml_name: str = "graph.rdf"
    ntriples_name: str = "graph.nt"
    nquads_name: str = "graph.nq"
    manifest_name: str = "manifest.json"
    validation_name: str = "validation.json"
    artifacts_subdir: str = "ontology/stage2"


@dataclass(frozen=True)
class QualityConfig:
    max_repair_rounds: int = 5
    require_all_documents: bool = True
    require_all_records: bool = True
    require_all_fragments: bool = True
    require_reviewer_approve: bool = True
    manual_audit_required: bool = True
    blocking_severities: tuple[str, ...] = ("critical", "high", "medium")


@dataclass(frozen=True)
class Stage2Config:
    path: Path
    stage1_config: Path
    compiler: ModelConfig = field(default_factory=ModelConfig)
    reviewer: ModelConfig = field(default_factory=lambda: ModelConfig(max_tokens=8_192))
    output: OutputConfig = field(default_factory=OutputConfig)
    provenance_namespace: str = "urn:dataelf:ontology:stage2:"
    quality: QualityConfig = field(default_factory=QualityConfig)
    total_stage_timeout_seconds: int = 1_800


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _tuple(value: Any, label: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string array")
    return tuple(value)


def _resolve(base: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _model(raw: Any, defaults: ModelConfig, label: str) -> ModelConfig:
    value = _mapping(raw, label)
    return ModelConfig(
        provider=str(value.get("provider", defaults.provider)),
        name=str(value.get("name", defaults.name)),
        context_window=int(value.get("context_window", defaults.context_window)),
        max_tokens=int(value.get("max_tokens", defaults.max_tokens)),
        temperature=float(value.get("temperature", defaults.temperature)),
        request_timeout_seconds=int(
            value.get("request_timeout_seconds", value.get("timeout_seconds", defaults.request_timeout_seconds))
        ),
        request_max_retries=int(value.get("request_max_retries", defaults.request_max_retries)),
        api_key_env=str(value.get("api_key_env", defaults.api_key_env)),
        base_url_env=str(value.get("base_url_env", defaults.base_url_env)),
    )


def load_config(path: str | Path) -> Stage2Config:
    target = Path(path).expanduser().resolve()
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    root = _mapping(raw, "configuration")
    output = _mapping(root.get("output"), "output")
    vocabulary = _mapping(root.get("vocabulary"), "vocabulary")
    quality = _mapping(root.get("quality"), "quality")
    result = Stage2Config(
        path=target,
        stage1_config=_resolve(target.parent, root.get("stage1_config", "../stage1/config.yaml")),
        compiler=_model(root.get("compiler"), ModelConfig(), "compiler"),
        reviewer=_model(root.get("reviewer"), ModelConfig(max_tokens=8_192), "reviewer"),
        output=OutputConfig(
            stable_suffix=str(output.get("stable_suffix", ".rdf")),
            rdfxml_name=str(output.get("rdfxml_name", "graph.rdf")),
            ntriples_name=str(output.get("ntriples_name", "graph.nt")),
            nquads_name=str(output.get("nquads_name", "graph.nq")),
            manifest_name=str(output.get("manifest_name", "manifest.json")),
            validation_name=str(output.get("validation_name", "validation.json")),
            artifacts_subdir=str(output.get("artifacts_subdir", "ontology/stage2")),
        ),
        provenance_namespace=str(vocabulary.get("provenance_namespace", "urn:dataelf:ontology:stage2:")),
        quality=QualityConfig(
            max_repair_rounds=int(quality.get("max_repair_rounds", 5)),
            require_all_documents=bool(quality.get("require_all_documents", True)),
            require_all_records=bool(quality.get("require_all_records", True)),
            require_all_fragments=bool(quality.get("require_all_fragments", True)),
            require_reviewer_approve=bool(quality.get("require_reviewer_approve", True)),
            manual_audit_required=bool(quality.get("manual_audit_required", True)),
            blocking_severities=_tuple(
                quality.get("blocking_severities"),
                "quality.blocking_severities",
                ("critical", "high", "medium"),
            ),
        ),
        total_stage_timeout_seconds=int(root.get("total_stage_timeout_seconds", 1_800)),
    )
    if not result.output.stable_suffix.startswith("."):
        raise ValueError("output.stable_suffix must begin with '.'")
    names = (
        result.output.rdfxml_name,
        result.output.ntriples_name,
        result.output.nquads_name,
        result.output.manifest_name,
        result.output.validation_name,
    )
    if any(Path(name).name != name for name in names):
        raise ValueError("Stage 2 output names must be file names")
    artifact_path = Path(result.output.artifacts_subdir)
    if artifact_path.is_absolute() or ".." in artifact_path.parts or not artifact_path.parts:
        raise ValueError("output.artifacts_subdir must be workspace-relative")
    if not result.provenance_namespace.endswith((":", "#", "/")):
        raise ValueError("vocabulary.provenance_namespace must end with ':', '#', or '/'")
    if not 1 <= result.quality.max_repair_rounds <= 10:
        raise ValueError("quality.max_repair_rounds must be between 1 and 10")
    for label, model in (("compiler", result.compiler), ("reviewer", result.reviewer)):
        if model.request_timeout_seconds < 1:
            raise ValueError(f"{label}.request_timeout_seconds must be positive")
        if not 0 <= model.request_max_retries <= 10:
            raise ValueError(f"{label}.request_max_retries must be between 0 and 10")
    if result.total_stage_timeout_seconds < 1:
        raise ValueError("total_stage_timeout_seconds must be positive")
    return result


__all__ = [
    "ModelConfig",
    "OutputConfig",
    "QualityConfig",
    "Stage2Config",
    "load_config",
]
