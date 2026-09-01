from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dataelf.discovery.contracts import ArtifactRef, OutputContract


class ArtifactContractError(ValueError):
    pass


def validate_outputs(workspace: Path, contract: OutputContract) -> tuple[list[ArtifactRef], list[str]]:
    workspace = workspace.resolve()
    artifacts: list[ArtifactRef] = []
    warnings: list[str] = []
    for spec in contract.artifacts:
        path = resolve_workspace_path(workspace, spec.path)
        if not path.is_file() or path.stat().st_size == 0:
            if spec.required:
                raise ArtifactContractError(f"Required output is missing or empty: {spec.path}")
            warnings.append(f"Optional output is missing or empty: {spec.path}")
            continue
        if spec.media_type == "application/json" or spec.json_root:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ArtifactContractError(f"Output is not valid JSON: {spec.path}: {exc}") from exc
            if spec.json_root and (not isinstance(payload, dict) or not isinstance(payload.get(spec.json_root), list)):
                raise ArtifactContractError(f"Output {spec.path} must contain a {spec.json_root!r} list")
        artifacts.append(ArtifactRef(
            artifact_id=spec.artifact_id, kind=spec.kind, path=spec.path, role="output",
            producer_stage="explorer", media_type=spec.media_type, checksum=_sha256(path),
            metadata={"contract_id": contract.contract_id, "contract_version": contract.version},
        ))
    return artifacts, warnings


def validate_stage_artifacts(workspace: Path, artifacts: list[ArtifactRef]) -> None:
    for artifact in artifacts:
        path = resolve_workspace_path(workspace, artifact.path)
        if not path.exists():
            raise ArtifactContractError(
                f"Stage {artifact.producer_stage!r} declared a missing artifact: {artifact.path}"
            )


def write_artifact_manifest(workspace: Path, artifacts: list[ArtifactRef]) -> Path:
    path = workspace / "artifact_manifest.json"
    payload = {"artifacts": [artifact.model_dump(mode="json") for artifact in artifacts]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def resolve_workspace_path(workspace: Path, relative_path: str) -> Path:
    path = (workspace / relative_path).resolve()
    if not path.is_relative_to(workspace.resolve()):
        raise ArtifactContractError(f"Artifact path escapes workspace: {relative_path}")
    return path


def relative_artifact_path(workspace: Path, path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(workspace.resolve()).as_posix()
    except ValueError as exc:
        raise ArtifactContractError(f"Artifact path must be inside workspace: {resolved}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


__all__ = ["ArtifactContractError", "relative_artifact_path", "resolve_workspace_path", "validate_outputs", "validate_stage_artifacts", "write_artifact_manifest"]
