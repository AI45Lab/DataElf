from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_ONTOLOGY_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
EXAMPLE_ONTOLOGY_CONFIG = DEFAULT_ONTOLOGY_CONFIG.with_suffix(".yaml.example")


def resolve_config_path(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    if target == DEFAULT_ONTOLOGY_CONFIG and not target.exists():
        return EXAMPLE_ONTOLOGY_CONFIG
    return target


def read_config(path: str | Path) -> tuple[Path, dict[str, Any]]:
    """Read the single ontology configuration; stage-relative files are unsupported."""
    target = resolve_config_path(path)
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("ontology configuration must be a mapping")
    allowed = {"ontology_template", "raw_page_size", "worker_timeout_seconds", "stage1", "stage2"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Unknown ontology config keys: {', '.join(sorted(map(str, unknown)))}")
    for stage in ("stage1", "stage2"):
        if not isinstance(raw.get(stage), dict) or not raw[stage]:
            raise ValueError(f"ontology configuration requires a non-empty {stage} mapping")
    return target, raw
