"""Snapshot readers, JSON pointers, profiling, and exact evidence replay."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import SourceInput


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def select(value: Any, pointer: str) -> list[tuple[str, Any]]:
    if pointer == "":
        return [("", value)]
    if not pointer.startswith("/"):
        raise ValueError(f"expected JSON pointer: {pointer}")
    states = [("", value)]
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        result = []
        for path, item in states:
            if token == "*" and isinstance(item, (list, dict)):
                pairs = enumerate(item) if isinstance(item, list) else item.items()
                result.extend((path + "/" + str(k).replace("~", "~0").replace("/", "~1"), v) for k, v in pairs)
            elif isinstance(item, dict) and token in item:
                result.append((path + "/" + raw, item[token]))
            elif isinstance(item, list) and token.isdigit() and int(token) < len(item):
                result.append((path + "/" + token, item[int(token)]))
        states = result
    return states


@dataclass(frozen=True)
class Record:
    source: str
    file: str
    pointer: str
    data: Any
    metadata: dict[str, Any]


class SourceCatalog:
    def __init__(self, workspace: Path, sources: list[SourceInput], *, allow_empty=False):
        self.workspace = workspace.resolve()
        self.sources = sources
        self.records: list[Record] = []
        self.hashes: dict[str, str] = {}
        for source in sources:
            path = source.path if source.path.is_absolute() else self.workspace / source.path
            path = path.resolve()
            relative = path.relative_to(self.workspace).as_posix()
            self.hashes[relative] = file_hash(path)
            fmt = source.format or path.suffix.removeprefix(".").lower()
            if fmt == "json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                pointers = source.record_pointers
                if pointers is None:
                    pointers = [source.records_path or ("/*" if isinstance(payload, list) else "")]
                selected = []
                for pointer in pointers:
                    matches = select(payload, pointer)
                    if source.record_pointers is not None and len(matches) != 1:
                        raise ValueError(f"record pointer does not resolve exactly once: {relative}#{pointer}")
                    selected.extend(matches)
            elif fmt in {"jsonl", "csv"}:
                if source.records_path:
                    raise ValueError("records_path is only valid for JSON; use mapping selectors for row fields")
                if fmt == "csv":
                    with path.open(encoding="utf-8-sig", newline="") as handle:
                        rows = list(csv.DictReader(handle))
                else:
                    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                selected = [(f"/{i}", row) for i, row in enumerate(rows)]
                if source.record_pointers is not None:
                    selected = [(p, v) for p, v in selected if p in source.record_pointers]
                    if len(selected) != len(source.record_pointers):
                        raise ValueError(f"invalid row selection: {relative}")
            else:
                raise ValueError(f"unsupported source format: {fmt}")
            self.records.extend(Record(source.source_id, relative, pointer, data,
                                       {**source.metadata, **source.record_metadata.get(pointer, {})})
                                for pointer, data in selected)
        if not self.records and not allow_empty:
            raise ValueError("ontology requires at least one source record")
        identities = [(r.source, r.file, r.pointer) for r in self.records]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate source record selection")
        selection = []
        for source in sources:
            value = source.model_dump(mode="json")
            path = source.path if source.path.is_absolute() else self.workspace / source.path
            value["path"] = path.resolve().relative_to(self.workspace).as_posix()
            selection.append(value)
        self.fingerprint = digest({"files": self.hashes, "selection": selection})

    def verify(self):
        for relative, expected in self.hashes.items():
            if file_hash(self.workspace / relative) != expected:
                raise ValueError(f"source changed during modeling: {relative}")

    def profile(self) -> dict[str, Any]:
        profiles = {}
        def preview(value, depth=0):
            if isinstance(value, str):
                return value[:256]
            if depth >= 3 and isinstance(value, (dict, list)):
                return {"preview_type": type(value).__name__, "size": len(value)}
            if isinstance(value, dict):
                return {k: preview(v, depth + 1) for k, v in list(value.items())[:10]}
            if isinstance(value, list):
                return [preview(v, depth + 1) for v in value[:3]]
            return value
        for source in sorted({r.source for r in self.records}):
            records = [r for r in self.records if r.source == source]
            fields: dict[str, list[Any]] = {}
            def walk(value, path=""):
                fields.setdefault(path, []).append(value)
                if isinstance(value, dict):
                    for key, child in value.items():
                        walk(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
                elif isinstance(value, list):
                    for child in value:
                        walk(child, path + "/*")
            for record in records:
                walk(record.data)
            profiles[source] = {"records": len(records), "fields": {
                path: {"present": len(values), "null": sum(v is None for v in values),
                       "types": sorted({type(v).__name__ for v in values}),
                       "distinct": len({digest(v) for v in values}), "examples": [preview(v) for v in values[:3]]}
                for path, values in fields.items()},
                "samples": [{"file": r.file, "pointer": r.pointer, "data": preview(r.data), "preview": True} for r in records[:3]]}
        return profiles
