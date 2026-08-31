from __future__ import annotations

import csv
import datetime as dt
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from dataelf.domains.ai_index.modeling.ontology.common.artifacts import atomic_write_json, file_sha256, read_json_object, sha256_json
from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.config import Stage1Config


_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_TRUE_FALSE = {"true", "false"}


@dataclass(frozen=True)
class CSVTable:
    name: str
    path: Path
    relative_file: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    size_bytes: int
    sha256: str


def load_domain_pack(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read domain pack {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"domain pack must be a mapping: {path}")
    if not isinstance(value.get("entities", {}), dict) or not isinstance(value.get("relations", {}), dict):
        raise ValueError("domain pack entities and relations must be mappings")
    return value


def _discover_paths(source: Path, config: Stage1Config) -> list[Path]:
    if source.is_file():
        if source.suffix.lower() != ".csv":
            raise ValueError(f"source file is not CSV: {source}")
        paths = [source]
    elif source.is_dir():
        paths = sorted(path for path in source.glob(config.source.csv_glob) if path.is_file())
    else:
        raise ValueError(f"CSV source does not exist: {source}")
    include = set(config.source.include_tables)
    exclude = set(config.source.exclude_tables)
    selected = [
        path
        for path in paths
        if (not include or path.stem in include or path.name in include)
        and path.stem not in exclude
        and path.name not in exclude
    ]
    if not selected:
        raise ValueError(f"no CSV tables matched source {source}")
    names = [path.stem for path in selected]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise ValueError(f"CSV table names are ambiguous: {duplicates}")
    return selected


def _read_table(path: Path, root: Path) -> CSVTable:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"CSV has no header: {path}")
            columns = [str(value).strip() if value is not None else "" for value in reader.fieldnames]
            if not columns or any(not value for value in columns):
                raise ValueError(f"CSV contains a blank header: {path}")
            duplicates = sorted(name for name, count in Counter(columns).items() if count > 1)
            if duplicates:
                raise ValueError(f"CSV contains duplicate headers {duplicates}: {path}")
            rows: list[dict[str, str]] = []
            for row_number, raw in enumerate(reader, start=1):
                if None in raw:
                    raise ValueError(f"CSV row {row_number} has too many fields: {path}")
                row = {column: "" if raw.get(original) is None else str(raw[original]) for column, original in zip(columns, reader.fieldnames)}
                rows.append(row)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ValueError(f"cannot read CSV {path}: {exc}") from exc
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return CSVTable(
        name=path.stem,
        path=path,
        relative_file=relative,
        columns=tuple(columns),
        rows=tuple(rows),
        size_bytes=path.stat().st_size,
        sha256=file_sha256(path),
    )


def load_tables(source: Path, config: Stage1Config) -> list[CSVTable]:
    paths = _discover_paths(source, config)
    root = source if source.is_dir() else source.parent
    return [_read_table(path, root) for path in paths]


def source_fingerprint(source: Path, config: Stage1Config) -> str:
    paths = _discover_paths(source, config)
    root = source if source.is_dir() else source.parent
    files: list[dict[str, Any]] = []
    for path in paths:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name
        files.append({"file": relative, "sizeBytes": path.stat().st_size, "sha256": file_sha256(path)})
    return sha256_json(
        {
            "format": "csv-directory-v1",
            "selection": {
                "glob": config.source.csv_glob,
                "include": list(config.source.include_tables),
                "exclude": list(config.source.exclude_tables),
            },
            "files": files,
        }
    )


def _row_hash(row: dict[str, str]) -> str:
    return sha256_json(row)


def _locator(table: CSVTable, row_number: int, row: dict[str, str]) -> dict[str, Any]:
    return {
        "table": table.name,
        "relativeFile": table.relative_file,
        "dataRowNumber": row_number,
        "canonicalRowHash": _row_hash(row),
    }


def _fits_date(value: str) -> bool:
    try:
        dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _fits_datetime(value: str) -> bool:
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return "T" in value or " " in value
    except ValueError:
        return False


def _json_kind(value: str) -> str | None:
    if not value or value[0] not in "[{":
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, list):
        return "array"
    if isinstance(parsed, dict):
        return "object"
    return None


def _infer_type(nonblank: Sequence[str]) -> tuple[str, dict[str, int]]:
    fits = {
        "integer": sum(bool(_INTEGER.fullmatch(value)) for value in nonblank),
        "number": sum(bool(_NUMBER.fullmatch(value)) and math.isfinite(float(value)) for value in nonblank),
        "boolean": sum(value.strip().lower() in _TRUE_FALSE for value in nonblank),
        "date": sum(_fits_date(value.strip()) for value in nonblank),
        "datetime": sum(_fits_datetime(value.strip()) for value in nonblank),
        "jsonArray": sum(_json_kind(value.strip()) == "array" for value in nonblank),
        "jsonObject": sum(_json_kind(value.strip()) == "object" for value in nonblank),
    }
    if not nonblank:
        return "unknown", fits
    priority = ("boolean", "integer", "number", "date", "datetime", "jsonArray", "jsonObject")
    for kind in priority:
        if fits[kind] == len(nonblank):
            return kind, fits
    return "string", fits


def profile_columns(table: CSVTable, max_rows: int | None) -> dict[str, Any]:
    inspected = table.rows if max_rows is None else table.rows[:max_rows]
    columns: dict[str, Any] = {}
    for column in table.columns:
        values = [row[column] for row in inspected]
        nonblank = [value for value in values if value.strip()]
        inferred, fits = _infer_type(nonblank)
        counts = Counter(nonblank)
        columns[column] = {
            "inferredType": inferred,
            "inspectedRows": len(inspected),
            "nonBlankCount": len(nonblank),
            "blankCount": len(values) - len(nonblank),
            "distinctNonBlankCount": len(counts),
            "duplicateNonBlankCount": sum(count - 1 for count in counts.values() if count > 1),
            "maxLength": max((len(value) for value in nonblank), default=0),
            "fits": fits,
        }
    return {
        "table": table.name,
        "rowCount": len(table.rows),
        "profiledRowCount": len(inspected),
        "exact": len(inspected) == len(table.rows),
        "columns": columns,
    }


def profile_identity(table: CSVTable, columns: Sequence[str]) -> dict[str, Any]:
    keys = tuple(columns)
    missing = [column for column in keys if column not in table.columns]
    if not keys or missing:
        return {
            "table": table.name,
            "columns": list(keys),
            "valid": False,
            "error": "identity columns are missing" if missing else "identity columns are empty",
            "missingColumns": missing,
        }
    values: list[tuple[str, ...]] = []
    incomplete = 0
    for row in table.rows:
        value = tuple(row[column].strip() for column in keys)
        if any(not part for part in value):
            incomplete += 1
        else:
            values.append(value)
    groups = Counter(values)
    duplicate_groups = sum(count > 1 for count in groups.values())
    duplicate_rows = sum(count - 1 for count in groups.values() if count > 1)
    complete = len(table.rows) - incomplete
    unique = complete == len(table.rows) and duplicate_rows == 0
    return {
        "table": table.name,
        "columns": list(keys),
        "valid": True,
        "rowCount": len(table.rows),
        "completeRowCount": complete,
        "incompleteRowCount": incomplete,
        "distinctCompleteKeyCount": len(groups),
        "duplicateGroupCount": duplicate_groups,
        "duplicateRowCount": duplicate_rows,
        "maxMultiplicity": max(groups.values(), default=0),
        "isRowUnique": unique,
        "recommendedSemantics": "row_primary_key" if unique else "entity_merge_key",
    }


def profile_join(
    source: CSVTable,
    source_columns: Sequence[str],
    target: CSVTable,
    target_columns: Sequence[str],
) -> dict[str, Any]:
    left_columns = tuple(source_columns)
    right_columns = tuple(target_columns)
    missing_left = [column for column in left_columns if column not in source.columns]
    missing_right = [column for column in right_columns if column not in target.columns]
    if not left_columns or len(left_columns) != len(right_columns) or missing_left or missing_right:
        return {
            "sourceTable": source.name,
            "sourceColumns": list(left_columns),
            "targetTable": target.name,
            "targetColumns": list(right_columns),
            "valid": False,
            "missingSourceColumns": missing_left,
            "missingTargetColumns": missing_right,
            "error": "join columns are missing or arity differs",
        }
    left_values = [tuple(row[column].strip() for column in left_columns) for row in source.rows]
    right_values = [tuple(row[column].strip() for column in right_columns) for row in target.rows]
    left_nonblank = [value for value in left_values if all(value)]
    right_nonblank = [value for value in right_values if all(value)]
    left_counts = Counter(left_nonblank)
    right_counts = Counter(right_nonblank)
    matched_rows = sum(count for value, count in left_counts.items() if value in right_counts)
    matched_keys = sum(value in right_counts for value in left_counts)
    source_unique = all(count == 1 for count in left_counts.values())
    target_unique = all(count == 1 for count in right_counts.values())
    if source_unique and target_unique:
        cardinality = "one_to_one"
    elif target_unique:
        cardinality = "many_to_one"
    elif source_unique:
        cardinality = "one_to_many"
    else:
        cardinality = "many_to_many"
    return {
        "sourceTable": source.name,
        "sourceColumns": list(left_columns),
        "targetTable": target.name,
        "targetColumns": list(right_columns),
        "valid": True,
        "sourceRowCount": len(source.rows),
        "sourceNonBlankRowCount": len(left_nonblank),
        "sourceDistinctKeyCount": len(left_counts),
        "targetRowCount": len(target.rows),
        "targetNonBlankRowCount": len(right_nonblank),
        "targetDistinctKeyCount": len(right_counts),
        "matchedSourceRowCount": matched_rows,
        "unmatchedSourceRowCount": len(left_nonblank) - matched_rows,
        "matchedDistinctSourceKeyCount": matched_keys,
        "sourceCoverage": 1.0 if not left_nonblank else matched_rows / len(left_nonblank),
        "targetKeyUnique": target_unique,
        "sourceKeyUnique": source_unique,
        "observedCardinality": cardinality,
    }


class EvidenceRegistry:
    def __init__(self, source_fingerprint: str) -> None:
        self.source_fingerprint = source_fingerprint
        self.records: dict[str, dict[str, Any]] = {}

    def add(self, evidence_type: str, request: dict[str, Any], result: dict[str, Any]) -> str:
        identifier = "ev_" + sha256_json(
            {
                "sourceFingerprint": self.source_fingerprint,
                "type": evidence_type,
                "request": request,
                "result": result,
            }
        )[:20]
        self.records[identifier] = {
            "id": identifier,
            "type": evidence_type,
            "request": request,
            "result": result,
        }
        return identifier


def _table_summary(table: CSVTable) -> dict[str, Any]:
    return {
        "name": table.name,
        "file": table.relative_file,
        "columns": list(table.columns),
        "rowCount": len(table.rows),
        "empty": not table.rows,
        "sizeBytes": table.size_bytes,
        "sha256": table.sha256,
    }


def _sample(table: CSVTable, limit: int, redact: set[str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for row_number, row in enumerate(table.rows[:limit], start=1):
        values = {key: ("<redacted>" if key in redact and value else value) for key, value in row.items()}
        rows.append({"values": values, "locator": _locator(table, row_number, row)})
    return {"table": table.name, "limit": limit, "returned": len(rows), "rows": rows}


def _relation_parts(name: str) -> tuple[str, str, str | None]:
    parts = name.split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], None
    return name, name, None


def build_evidence_bundle(
    source: Path,
    config: Stage1Config,
    *,
    source_fingerprint_override: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    normalizer_fingerprint: str | None = None,
    source_index: dict[str, Any] | None = None,
    normalization_lineage: dict[str, Any] | None = None,
    source_replay_errors: list[str] | None = None,
    source_contract_fingerprint: str | None = None,
) -> dict[str, Any]:
    tables = load_tables(source, config)
    table_map = {table.name: table for table in tables}
    domain_pack = load_domain_pack(config.ontology.domain_pack_path)
    catalog_core = [_table_summary(table) for table in tables]
    source_fingerprint_value = source_fingerprint_override or sha256_json(
        {
            "format": "csv-directory-v1",
            "selection": {
                "glob": config.source.csv_glob,
                "include": list(config.source.include_tables),
                "exclude": list(config.source.exclude_tables),
            },
            "files": [
                {"file": item["file"], "sizeBytes": item["sizeBytes"], "sha256": item["sha256"]}
                for item in catalog_core
            ],
        }
    )
    registry = EvidenceRegistry(source_fingerprint_value)
    catalog_result = {
        "sourceType": "ai_index_raw" if source_metadata else "csv",
        "sourceFingerprint": source_fingerprint_value,
        "tableCount": len(tables),
        "nonEmptyTableCount": sum(bool(table.rows) for table in tables),
        "totalRowCount": sum(len(table.rows) for table in tables),
        "tables": catalog_core,
    }
    if source_metadata:
        catalog_result["rawFileCount"] = len(source_metadata.get("rawFiles", []))
        catalog_result["rawFiles"] = source_metadata.get("rawFiles", [])
        catalog_result["normalizerFingerprint"] = normalizer_fingerprint
    catalog_ref = registry.add("catalog", {}, catalog_result)
    samples: dict[str, str] = {}
    profiles: dict[str, str] = {}
    identities: dict[str, str] = {}
    joins: dict[str, str] = {}
    redact = set(config.source.redact_columns)
    for table in tables:
        samples[table.name] = registry.add(
            "sample_rows",
            {"table": table.name, "limit": config.source.sample_rows},
            _sample(table, config.source.sample_rows, redact),
        )
        profiles[table.name] = registry.add(
            "column_profile",
            {"table": table.name, "maxRows": config.source.profile_max_rows},
            profile_columns(table, config.source.profile_max_rows),
        )
        id_columns = [column for column in table.columns if column == "id" or column.endswith("_id")]
        candidates: list[tuple[str, ...]] = [(column,) for column in id_columns]
        if len(id_columns) > 1:
            candidates.append(tuple(id_columns))
        for columns in candidates:
            key = f"{table.name}|{','.join(columns)}"
            identities[key] = registry.add(
                "identity_profile",
                {"table": table.name, "columns": list(columns)},
                profile_identity(table, columns),
            )
    for source_table in tables:
        for target_table in tables:
            if source_table.name == target_table.name:
                continue
            shared = sorted(
                column
                for column in set(source_table.columns).intersection(target_table.columns)
                if column == "id" or column.endswith("_id")
            )
            for column in shared:
                key = f"{source_table.name}|{column}|{target_table.name}|{column}"
                joins[key] = registry.add(
                    "join_profile",
                    {
                        "sourceTable": source_table.name,
                        "sourceColumns": [column],
                        "targetTable": target_table.name,
                        "targetColumns": [column],
                    },
                    profile_join(source_table, [column], target_table, [column]),
                )
    entity_hints: dict[str, Any] = {}
    entities = domain_pack.get("entities", {})
    for class_name, raw_hint in entities.items():
        hint = dict(raw_hint) if isinstance(raw_hint, dict) else {}
        table_name = str(hint.get("table", ""))
        id_field = str(hint.get("id_field", ""))
        evidence_ref = identities.get(f"{table_name}|{id_field}")
        entity_hints[str(class_name)] = {
            "class": str(class_name),
            "table": table_name,
            "idField": id_field,
            "nameField": hint.get("name_field"),
            "tableExists": table_name in table_map,
            "columnsExist": bool(table_name in table_map and id_field in table_map[table_name].columns),
            "identityEvidenceRef": evidence_ref,
        }
    relation_hints: dict[str, Any] = {}
    for relation_name, raw_hint in domain_pack.get("relations", {}).items():
        hint = dict(raw_hint) if isinstance(raw_hint, dict) else {}
        source_class, predicate, target_class = _relation_parts(str(relation_name))
        relation_table_name = str(hint.get("table", ""))
        source_field = str(hint.get("source_field", ""))
        target_field = str(hint.get("target_field", ""))
        source_entity = entity_hints.get(source_class, {})
        target_entity = entity_hints.get(target_class, {}) if target_class else {}
        source_key = (
            f"{relation_table_name}|{source_field}|{source_entity.get('table')}|{source_entity.get('idField')}"
        )
        target_key = (
            f"{relation_table_name}|{target_field}|{target_entity.get('table')}|{target_entity.get('idField')}"
        )
        relation_table = table_map.get(relation_table_name)
        relation_hints[str(relation_name)] = {
            "name": str(relation_name),
            "sourceClass": source_class,
            "predicate": predicate,
            "targetClass": target_class,
            "table": relation_table_name,
            "sourceField": source_field,
            "targetField": target_field,
            "tableExists": relation_table is not None,
            "fieldsExist": bool(
                relation_table
                and source_field in relation_table.columns
                and target_field in relation_table.columns
            ),
            "sourceJoinEvidenceRef": joins.get(source_key),
            "targetJoinEvidenceRef": joins.get(target_key) if target_class else None,
            "targetIsScalar": target_class is None,
        }
    domain_result = {
        "domain": domain_pack.get("domain"),
        "notice": "These are declared hints, not unconditional facts; resolve with source evidence.",
        "entities": entity_hints,
        "relations": relation_hints,
    }
    domain_ref = registry.add(
        "domain_hints",
        {"path": config.ontology.domain_pack_path.name, "sha256": file_sha256(config.ontology.domain_pack_path)},
        domain_result,
    )
    source_index_ref: str | None = None
    lineage_ref: str | None = None
    if source_index is not None:
        source_index_ref = registry.add(
            "raw_source_index",
            {"sourceIndexSha256": source_index.get("sourceIndexSha256")},
            {
                "schemaVersion": source_index.get("schemaVersion"),
                "metrics": source_index.get("metrics", {}),
                "documents": source_index.get("documents", []),
                "pathProfiles": source_index.get("pathProfiles", {}),
                "relationComparisons": source_index.get("relationComparisons", {}),
                "replayErrors": list(source_replay_errors or []),
            },
        )
    if normalization_lineage is not None:
        lineage_ref = registry.add(
            "normalization_lineage",
            {"lineageSha256": normalization_lineage.get("lineageSha256")},
            normalization_lineage,
        )
    return {
        "formatVersion": 2,
        "sourceFingerprint": source_fingerprint_value,
        "sourceType": catalog_result["sourceType"],
        "normalizerFingerprint": normalizer_fingerprint,
        "sourceContractFingerprint": source_contract_fingerprint,
        "domainPackFingerprint": file_sha256(config.ontology.domain_pack_path),
        "catalogEvidenceRef": catalog_ref,
        "domainHintsEvidenceRef": domain_ref,
        "sourceIndexEvidenceRef": source_index_ref,
        "normalizationLineageEvidenceRef": lineage_ref,
        "catalog": catalog_result,
        "sourceIndex": source_index,
        "normalizationLineage": normalization_lineage,
        "sourceReplayErrors": list(source_replay_errors or []),
        "toolIndex": {
            "samples": samples,
            "profiles": profiles,
            "identities": identities,
            "joins": joins,
        },
        "evidence": registry.records,
    }


def prepare_source_cache(workspace: Path, config: Stage1Config) -> tuple[Path, dict[str, Any], bool]:
    if config.source.format == "ai_index_raw":
        from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.raw_source import (
            normalize_raw_source,
            normalizer_fingerprint as current_normalizer_fingerprint,
            raw_source_fingerprint,
        )

        raw_source = workspace / config.source.raw_subdir
        fingerprint = raw_source_fingerprint(raw_source, config)
        cache_root = workspace / config.artifacts.subdir / "source_cache" / fingerprint
        normalized = normalize_raw_source(raw_source, cache_root, config)
        source = normalized.tables_path
        source_metadata = normalized.metadata
        normalizer_fp: str | None = current_normalizer_fingerprint()
    else:
        source = workspace / config.source.tables_subdir
        fingerprint = source_fingerprint(source, config)
        cache_root = workspace / config.artifacts.subdir / "source_cache" / fingerprint
        source_metadata = None
        normalizer_fp = None
    domain_fingerprint = file_sha256(config.ontology.domain_pack_path)
    bundle_path = cache_root / "evidence.json"
    catalog_path = cache_root / "catalog.json"
    source_index_path = cache_root / "source_index.json"
    lineage_path = cache_root / "normalization_lineage.json"
    source_contract_fp: str | None = None
    if config.source.format == "ai_index_raw":
        from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1 import raw_semantics

        source_contract_fp = file_sha256(Path(raw_semantics.__file__).resolve())
    cache_artifacts_exist = bundle_path.exists() and catalog_path.exists() and (
        config.source.format != "ai_index_raw" or (source_index_path.exists() and lineage_path.exists())
    )
    if cache_artifacts_exist:
        cached = read_json_object(bundle_path)
        if (
            cached.get("formatVersion") == 2
            and cached.get("sourceFingerprint") == fingerprint
            and cached.get("domainPackFingerprint") == domain_fingerprint
            and cached.get("normalizerFingerprint") == normalizer_fp
            and cached.get("sourceContractFingerprint") == source_contract_fp
            and not cached.get("sourceReplayErrors")
        ):
            if config.source.format == "ai_index_raw":
                from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.raw_semantics import replay_source_index

                replay_errors = replay_source_index(workspace, read_json_object(source_index_path))
                if replay_errors:
                    raise ValueError("cached source index replay failed: " + "; ".join(replay_errors[:10]))
            return cache_root, cached, True
    source_index = None
    normalization_lineage = None
    replay_errors: list[str] = []
    if config.source.format == "ai_index_raw":
        from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.raw_semantics import (
            build_normalization_lineage,
            build_source_index,
            replay_source_index,
        )

        source_index = build_source_index(raw_source, fingerprint, config.source.raw_subdir)
        replay_errors = replay_source_index(workspace, source_index)
        if replay_errors:
            raise ValueError("source index replay failed: " + "; ".join(replay_errors[:10]))
        normalization_lineage = build_normalization_lineage(load_tables(source, config), source_index)
    fresh = build_evidence_bundle(
        source,
        config,
        source_fingerprint_override=fingerprint if source_metadata else None,
        source_metadata=source_metadata,
        normalizer_fingerprint=normalizer_fp,
        source_index=source_index,
        normalization_lineage=normalization_lineage,
        source_replay_errors=replay_errors,
        source_contract_fingerprint=source_contract_fp,
    )
    atomic_write_json(catalog_path, fresh["catalog"])
    if source_index is not None:
        atomic_write_json(source_index_path, source_index)
    if normalization_lineage is not None:
        atomic_write_json(lineage_path, normalization_lineage)
    atomic_write_json(bundle_path, fresh)
    return cache_root, fresh, False


def evidence_result(bundle: dict[str, Any], evidence_ref: str) -> dict[str, Any]:
    record = bundle.get("evidence", {}).get(evidence_ref)
    if not isinstance(record, dict) or not isinstance(record.get("result"), dict):
        raise KeyError(f"unknown evidence reference: {evidence_ref}")
    return record["result"]


def query_evidence(bundle: dict[str, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    index = bundle.get("toolIndex", {})
    if tool == "source_overview":
        return {
            "evidenceRef": bundle["catalogEvidenceRef"],
            "result": evidence_result(bundle, bundle["catalogEvidenceRef"]),
            "domainHintsEvidenceRef": bundle["domainHintsEvidenceRef"],
            "domainHints": evidence_result(bundle, bundle["domainHintsEvidenceRef"]),
            "sourceIndexEvidenceRef": bundle.get("sourceIndexEvidenceRef"),
            "sourceIndexSummary": evidence_result(bundle, bundle["sourceIndexEvidenceRef"])
            if bundle.get("sourceIndexEvidenceRef") else None,
            "normalizationLineageEvidenceRef": bundle.get("normalizationLineageEvidenceRef"),
        }
    if tool in {"describe_raw_endpoint", "profile_raw_path", "raw_coverage", "compare_relation_sources", "replay_source"}:
        source_index = bundle.get("sourceIndex")
        ref = bundle.get("sourceIndexEvidenceRef")
        if not isinstance(source_index, dict) or not isinstance(ref, str):
            raise KeyError("raw source index is unavailable")
        if tool == "describe_raw_endpoint":
            endpoint = str(arguments.get("endpoint", ""))
            documents = [item for item in source_index.get("documents", []) if item.get("endpoint") == endpoint]
            profiles = [item for item in source_index.get("pathProfiles", {}).values() if item.get("endpoint") == endpoint]
            result = {"endpoint": endpoint, "documents": documents, "pathProfileCount": len(profiles), "pathProfiles": profiles}
        elif tool == "profile_raw_path":
            from dataelf.domains.ai_index.modeling.ontology.stage1.ontology_stage1.raw_semantics import source_profile

            result = source_profile(
                source_index,
                endpoint=str(arguments.get("endpoint", "")),
                path_pattern=str(arguments.get("pathPattern", "")),
            )
        elif tool == "compare_relation_sources":
            name = str(arguments.get("relation", ""))
            comparisons = source_index.get("relationComparisons", {})
            result = comparisons.get(name) if name else comparisons
            if result is None:
                raise KeyError(f"unknown relation comparison: {name}")
        elif tool == "replay_source":
            result = {"status": "valid" if not bundle.get("sourceReplayErrors") else "invalid", "errors": bundle.get("sourceReplayErrors", []), "metrics": source_index.get("metrics", {})}
        else:
            result = {"metrics": source_index.get("metrics", {}), "documents": source_index.get("documents", [])}
        return {"evidenceRef": ref, "result": result}
    if tool == "trace_normalized_column":
        lineage = bundle.get("normalizationLineage")
        ref = bundle.get("normalizationLineageEvidenceRef")
        coordinate = str(arguments.get("coordinate", ""))
        if not isinstance(lineage, dict) or not isinstance(ref, str):
            raise KeyError("normalization lineage is unavailable")
        mapping = lineage.get("mappings", {}).get(coordinate)
        if not isinstance(mapping, dict):
            raise KeyError(f"unknown normalized column: {coordinate}")
        return {"evidenceRef": ref, "result": mapping}
    if tool in {"describe_table", "sample_rows", "profile_columns"}:
        table = str(arguments.get("table", ""))
        mapping = index["profiles"] if tool != "sample_rows" else index["samples"]
        ref = mapping.get(table)
        if not ref:
            raise KeyError(f"unknown table: {table}")
        result = evidence_result(bundle, ref)
        if tool == "describe_table":
            catalog_table = next(item for item in bundle["catalog"]["tables"] if item["name"] == table)
            result = {"catalog": catalog_table, "profile": result}
        return {"evidenceRef": ref, "result": result}
    if tool == "profile_identity":
        columns = arguments.get("columns", [])
        key = f"{arguments.get('table', '')}|{','.join(columns)}"
        ref = index["identities"].get(key)
    elif tool == "validate_join":
        key = "|".join(
            [
                str(arguments.get("sourceTable", "")),
                ",".join(arguments.get("sourceColumns", [])),
                str(arguments.get("targetTable", "")),
                ",".join(arguments.get("targetColumns", [])),
            ]
        )
        ref = index["joins"].get(key)
    elif tool == "relationship_candidates":
        ref = bundle["domainHintsEvidenceRef"]
    else:
        raise KeyError(f"unsupported evidence tool: {tool}")
    if not ref:
        raise KeyError(f"evidence was not precomputed for {tool}: {arguments}")
    return {"evidenceRef": ref, "result": evidence_result(bundle, ref)}
