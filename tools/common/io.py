import json
import os
from pathlib import Path
from typing import Any


def load_json(path: str | Path) -> list | dict:
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(
    data: list | dict,
    path: str | Path,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)


def append_jsonl(records: list[dict[str, Any]], path: str | Path, flush: bool = True) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if flush:
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass


def repair_trailing_incomplete_jsonl(path: str | Path) -> bool:
    path = Path(path)
    if not path.exists():
        return False
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return False
    last_nl = data.rfind(b"\n")
    new_data = data[: last_nl + 1] if last_nl != -1 else b""
    path.write_bytes(new_data)
    return True


def normalize_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def load_jsonl_id_set(path: str | Path, id_key: str = "id") -> set[str]:
    path = Path(path)
    ids: set[str] = set()
    if not path.exists():
        return ids
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and id_key in obj:
                ids.add(normalize_id(obj.get(id_key)))
    return ids
