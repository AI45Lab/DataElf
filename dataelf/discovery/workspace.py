from __future__ import annotations

import json
from pathlib import Path

from dataelf.discovery.contracts import JobSpec


CORE_WORKSPACE_DIRS = ["raw", "tables", "scripts", "notes", "prompts", "logs", "reviews", "artifacts"]


def prepare_workspace(workspace_path: Path, spec: JobSpec) -> Path:
    workspace_path.mkdir(parents=True, exist_ok=True)
    for relative in CORE_WORKSPACE_DIRS:
        (workspace_path / relative).mkdir(parents=True, exist_ok=True)
    (workspace_path / "job_spec.json").write_text(
        json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return workspace_path


__all__ = ["CORE_WORKSPACE_DIRS", "prepare_workspace"]
