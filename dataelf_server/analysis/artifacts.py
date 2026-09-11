"""Explicit references to the artifacts prepared for one server attempt."""
from __future__ import annotations

import json
from pathlib import Path

from dataelf.discovery.artifacts import resolve_workspace_path


def rdf_path(workspace: Path) -> Path:
    inventory = json.loads((workspace / "artifacts" / "server_inputs.json").read_text(encoding="utf-8"))
    relative = inventory["rdf_path"]
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("RDF reference must be workspace-relative")
    path = resolve_workspace_path(workspace, relative)
    if not path.is_file():
        raise ValueError("Prepared RDF artifact is missing")
    return path
