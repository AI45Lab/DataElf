from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
from typing import Any

import yaml


@dataclass(frozen=True)
class SourceConfig:
    format: str = "ai_index_raw"
    raw_subdir: str = "raw/ai_index"
    json_glob: str = "*.json"
    tables_subdir: str = "tables"
    csv_glob: str = "*.csv"
    include_tables: tuple[str, ...] = ()
    exclude_tables: tuple[str, ...] = ()
    sample_rows: int = 5
    max_sample_rows: int = 50
    profile_max_rows: int | None = None
    redact_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class OntologyConfig:
    ontology_id: str = "dataelf_ai_index"
    namespace: str = "urn:dataelf:ontology:ai-index:"
    title: str = "DataElf AI Index Ontology"
    label_language: str = "en"
    domain_pack_path: Path = Path("dataelf/domains/ai_index/domain.yaml")
    competency_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "openai"
    name: str = "glm-5.2-1m"
    context_window: int = 200_000
    max_tokens: int = 32_768
    temperature: float = 0.1
    process_timeout_seconds: int = 7_200
    request_timeout_seconds: int = 900
    request_max_retries: int = 3
    api_key_env: str = "OPENAI_API_KEY"
    base_url_env: str = "OPENAI_BASE_URL"


@dataclass(frozen=True)
class PiConfig:
    repo: Path = Path(".")
    node: Path = Path("node")
    supported_version: str = "0.80.3"


@dataclass(frozen=True)
class QualityConfig:
    max_repair_rounds: int = 5
    manual_audit_required: bool = True
    require_all_tables_classified: bool = True
    require_all_columns_classified: bool = True
    require_domain_hints_resolved: bool = True
    blocking_severities: tuple[str, ...] = ("critical", "high")


@dataclass(frozen=True)
class CheckpointConfig:
    enabled: bool = True
    resume: str = "auto"
    retain_completed: bool = True


@dataclass(frozen=True)
class ArtifactsConfig:
    """Workspace-relative location for Stage 1 runtime artifacts."""

    subdir: str = "ontology/stage1"


@dataclass(frozen=True)
class Stage1Config:
    path: Path
    source: SourceConfig = field(default_factory=SourceConfig)
    ontology: OntologyConfig = field(default_factory=OntologyConfig)
    generator: ModelConfig = field(default_factory=ModelConfig)
    reviewer: ModelConfig = field(
        default_factory=lambda: ModelConfig(temperature=0.0, max_tokens=16_384)
    )
    pi: PiConfig = field(default_factory=PiConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    checkpoint: CheckpointConfig = field(default_factory=CheckpointConfig)
    artifacts: ArtifactsConfig = field(default_factory=ArtifactsConfig)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string array")
    return tuple(value)


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _resolve_executable(base: Path, value: str | Path) -> Path:
    text = str(value).strip()
    if not text:
        raise ValueError("pi.node must not be empty")
    if "/" not in text and "\\" not in text:
        discovered = shutil.which(text)
        if discovered:
            return Path(discovered).resolve()
    return _resolve(base, text)


def _model(raw: Any, defaults: ModelConfig, label: str) -> ModelConfig:
    value = _mapping(raw, label)
    return ModelConfig(
        provider=str(value.get("provider", defaults.provider)),
        name=str(value.get("name", defaults.name)),
        context_window=int(value.get("context_window", defaults.context_window)),
        max_tokens=int(value.get("max_tokens", defaults.max_tokens)),
        temperature=float(value.get("temperature", defaults.temperature)),
        process_timeout_seconds=int(
            value.get("process_timeout_seconds", value.get("timeout_seconds", defaults.process_timeout_seconds))
        ),
        request_timeout_seconds=int(value.get("request_timeout_seconds", defaults.request_timeout_seconds)),
        request_max_retries=int(value.get("request_max_retries", defaults.request_max_retries)),
        api_key_env=str(value.get("api_key_env", defaults.api_key_env)),
        base_url_env=str(value.get("base_url_env", defaults.base_url_env)),
    )


def load_config(path: str | Path) -> Stage1Config:
    config_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    root = _mapping(raw, "configuration")
    base = config_path.parent
    source = _mapping(root.get("source"), "source")
    ontology = _mapping(root.get("ontology"), "ontology")
    pi = _mapping(root.get("pi"), "pi")
    quality = _mapping(root.get("quality"), "quality")
    checkpoint = _mapping(root.get("checkpoint"), "checkpoint")
    artifacts = _mapping(root.get("artifacts"), "artifacts")
    questions = _tuple(ontology.get("competency_questions"), "ontology.competency_questions")
    if not questions:
        raise ValueError("ontology.competency_questions must not be empty")
    result = Stage1Config(
        path=config_path,
        source=SourceConfig(
            format=str(source.get("format", "ai_index_raw")),
            raw_subdir=str(source.get("raw_subdir", "raw/ai_index")),
            json_glob=str(source.get("json_glob", "*.json")),
            tables_subdir=str(source.get("tables_subdir", "tables")),
            csv_glob=str(source.get("csv_glob", "*.csv")),
            include_tables=_tuple(source.get("include_tables"), "source.include_tables"),
            exclude_tables=_tuple(source.get("exclude_tables"), "source.exclude_tables"),
            sample_rows=int(source.get("sample_rows", 5)),
            max_sample_rows=int(source.get("max_sample_rows", 50)),
            profile_max_rows=(
                None if source.get("profile_max_rows") in (None, "") else int(source["profile_max_rows"])
            ),
            redact_columns=_tuple(source.get("redact_columns"), "source.redact_columns"),
        ),
        ontology=OntologyConfig(
            ontology_id=str(ontology.get("id", "dataelf_ai_index")),
            namespace=str(ontology.get("namespace", "urn:dataelf:ontology:ai-index:")),
            title=str(ontology.get("title", "DataElf AI Index Ontology")),
            label_language=str(ontology.get("label_language", "en")),
            domain_pack_path=_resolve(base, ontology.get("domain_pack_path", "../../dataelf/domains/ai_index/domain.yaml")),
            competency_questions=questions,
        ),
        generator=_model(root.get("generator"), ModelConfig(), "generator"),
        reviewer=_model(
            root.get("reviewer"),
            ModelConfig(temperature=0.0, max_tokens=16_384),
            "reviewer",
        ),
        pi=PiConfig(
            repo=_resolve(base, pi.get("repo", "../..")),
            node=_resolve_executable(base, pi.get("node", "node")),
            supported_version=str(pi.get("supported_version", "0.80.3")),
        ),
        quality=QualityConfig(
            max_repair_rounds=int(quality.get("max_repair_rounds", 5)),
            manual_audit_required=bool(quality.get("manual_audit_required", True)),
            require_all_tables_classified=bool(quality.get("require_all_tables_classified", True)),
            require_all_columns_classified=bool(quality.get("require_all_columns_classified", True)),
            require_domain_hints_resolved=bool(quality.get("require_domain_hints_resolved", True)),
            blocking_severities=_tuple(quality.get("blocking_severities", ["critical", "high"]), "quality.blocking_severities"),
        ),
        checkpoint=CheckpointConfig(
            enabled=bool(checkpoint.get("enabled", True)),
            resume=str(checkpoint.get("resume", "auto")),
            retain_completed=bool(checkpoint.get("retain_completed", True)),
        ),
        artifacts=ArtifactsConfig(subdir=str(artifacts.get("subdir", "ontology/stage1"))),
    )
    if result.source.sample_rows < 0 or result.source.max_sample_rows < 1:
        raise ValueError("source sampling values are invalid")
    if result.source.sample_rows > result.source.max_sample_rows:
        raise ValueError("source.sample_rows exceeds source.max_sample_rows")
    if result.source.format not in {"ai_index_raw", "csv"}:
        raise ValueError("source.format must be ai_index_raw or csv")
    if not 1 <= result.quality.max_repair_rounds <= 10:
        raise ValueError("quality.max_repair_rounds must be between 1 and 10")
    for label, model in (("generator", result.generator), ("reviewer", result.reviewer)):
        if model.process_timeout_seconds < 1:
            raise ValueError(f"{label}.process_timeout_seconds must be positive")
        if model.request_timeout_seconds < 1:
            raise ValueError(f"{label}.request_timeout_seconds must be positive")
        if not 0 <= model.request_max_retries <= 10:
            raise ValueError(f"{label}.request_max_retries must be between 0 and 10")
    if not result.ontology.namespace.endswith((":", "#", "/")):
        raise ValueError("ontology.namespace must end with ':', '#', or '/'")
    artifact_path = Path(result.artifacts.subdir)
    if artifact_path.is_absolute() or ".." in artifact_path.parts or not artifact_path.parts:
        raise ValueError("artifacts.subdir must be a safe workspace-relative path")
    return result
