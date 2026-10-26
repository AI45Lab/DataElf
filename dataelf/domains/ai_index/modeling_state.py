from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_SCHEMA_VERSION = "dataelf-ai-index-modeling-run.v1"
_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|bearer|password|secret|token)", re.I)
_ALLOWED_UPDATES = {"jobId", "stage", "runIds", "artifactPaths", "error", "metrics"}


class AIIndexModelingStateStore:
    def __init__(self, workspace_path: Path):
        self.path = workspace_path / "modeling" / "ai_index" / "state.json"
        self.payload: dict[str, Any] = {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "status": "initialized",
            "history": [],
        }

    def transition(self, status: str, **updates: Any) -> None:
        unknown = set(updates) - _ALLOWED_UPDATES
        if unknown:
            raise ValueError(f"modeling state only accepts lightweight fields; unknown={sorted(unknown)}")
        now = datetime.now(timezone.utc).isoformat()
        self.payload.update(_redact(updates))
        self.payload["status"] = status
        self.payload["updatedAt"] = now
        self.payload.setdefault("history", []).append({"at": now, "status": status})
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(self.payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("<redacted>" if _SECRET_KEY.search(str(key)) else _redact(child)) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, str):
        return re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    return value


__all__ = ["AIIndexModelingStateStore", "STATE_SCHEMA_VERSION"]
