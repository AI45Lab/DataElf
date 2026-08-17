from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataelf.domains.ai_index import table_builder

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json, file_sha256, sha256_json
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config


RAW_NORMALIZER_VERSION = "dataelf-ai-index-raw-normalizer/1"


@dataclass(frozen=True)
class RawNormalization:
    source_fingerprint: str
    normalizer_fingerprint: str
    tables_path: Path
    metadata: dict[str, Any]


def discover_raw_paths(source: Path, config: Stage1Config) -> list[Path]:
    if not source.is_dir():
        raise ValueError(f"AI Index raw source does not exist: {source}")
    paths = sorted(path for path in source.glob(config.source.json_glob) if path.is_file())
    if not paths:
        raise ValueError(f"no AI Index raw JSON matched {source / config.source.json_glob}")
    return paths


def raw_source_fingerprint(source: Path, config: Stage1Config) -> str:
    files = [
        {
            "file": path.relative_to(source).as_posix(),
            "sizeBytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }
        for path in discover_raw_paths(source, config)
    ]
    return sha256_json(
        {
            "format": "dataelf-ai-index-raw-v1",
            "selection": {"glob": config.source.json_glob},
            "files": files,
        }
    )


def normalizer_fingerprint() -> str:
    return sha256_json(
        {
            "version": RAW_NORMALIZER_VERSION,
            "tableBuilderSha256": file_sha256(Path(table_builder.__file__).resolve()),
            "implementationSha256": file_sha256(Path(__file__).resolve()),
        }
    )


def _load_raw(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read AI Index raw response {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"AI Index raw response must be an object: {path}")
    if value.get("source") != "ai_index" or not isinstance(value.get("endpoint"), str):
        raise ValueError(f"unsupported raw response envelope: {path}")
    if not isinstance(value.get("data"), (dict, list)):
        raise ValueError(f"AI Index raw response has no data object/list: {path}")
    return value


def _normalize_response(response: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    endpoint = response["endpoint"]
    if "paper/search" in endpoint:
        return table_builder.normalize_papers_response(response)
    if "institutions/search" in endpoint:
        return table_builder.normalize_institutions_response(response)
    if "scholar/search" in endpoint:
        return table_builder.normalize_scholars_response(response)
    if "funding-profile" in endpoint:
        institution_id = str(response.get("request", {}).get("institution_id", ""))
        return table_builder.normalize_funding_response(response, institution_id)
    raise ValueError(f"unsupported AI Index endpoint in raw source: {endpoint}")


def normalize_raw_source(source: Path, cache_root: Path, config: Stage1Config) -> RawNormalization:
    paths = discover_raw_paths(source, config)
    source_fp = raw_source_fingerprint(source, config)
    normalizer_fp = normalizer_fingerprint()
    target = cache_root / "normalized"
    manifest_path = target / "normalization.json"
    if manifest_path.is_file() and (target / "tables").is_dir():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            isinstance(manifest, dict)
            and manifest.get("sourceFingerprint") == source_fp
            and manifest.get("normalizerFingerprint") == normalizer_fp
        ):
            return RawNormalization(source_fp, normalizer_fp, target / "tables", manifest)

    temporary = cache_root / f".normalized-{os.getpid()}.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    (temporary / "tables").mkdir()
    table_builder.ensure_table_schemas(temporary)
    combined: dict[str, list[dict[str, Any]]] = {}
    raw_files: list[dict[str, Any]] = []
    for path in paths:
        response = _load_raw(path)
        relative = path.relative_to(source).as_posix()
        response = dict(response)
        response["raw_uri"] = (Path(config.source.raw_subdir) / relative).as_posix()
        tables = _normalize_response(response)
        for name, rows in tables.items():
            combined.setdefault(name, []).extend(rows)
        data = response.get("data")
        item_count = len(data.get("list", [])) if isinstance(data, dict) and isinstance(data.get("list"), list) else 1
        raw_files.append(
            {
                "file": relative,
                "workspaceRelativeFile": response["raw_uri"],
                "endpoint": response["endpoint"],
                "mode": response.get("mode"),
                "itemCount": item_count,
                "sizeBytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    table_builder.write_tables(temporary, combined, append=False)
    table_counts = {
        path.stem: len(table_builder.read_table(temporary, path.stem))
        for path in sorted((temporary / "tables").glob("*.csv"))
    }
    manifest = {
        "schemaVersion": "dataelf-raw-normalization.v1",
        "sourceType": "ai_index_raw",
        "sourceFingerprint": source_fp,
        "normalizerFingerprint": normalizer_fp,
        "rawFiles": raw_files,
        "tableCounts": table_counts,
        "totalRowCount": sum(table_counts.values()),
    }
    atomic_write_json(temporary / "normalization.json", manifest)
    if target.exists():
        shutil.rmtree(target)
    os.replace(temporary, target)
    return RawNormalization(source_fp, normalizer_fp, target / "tables", manifest)


__all__ = [
    "RAW_NORMALIZER_VERSION",
    "RawNormalization",
    "discover_raw_paths",
    "normalize_raw_source",
    "normalizer_fingerprint",
    "raw_source_fingerprint",
]
