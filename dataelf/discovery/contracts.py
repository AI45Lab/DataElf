from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from dataelf.schemas import now_utc


class JobSpec(BaseModel):
    domain: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    objective: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    requested_outputs: list[str] = Field(default_factory=list)
    modeling_strategy: str | None = None
    explorer: Literal["pi"] = "pi"


class ArtifactRef(BaseModel):
    artifact_id: str
    kind: str
    path: str
    role: Literal["input", "evidence", "output", "log"]
    producer_stage: str
    media_type: str | None = None
    schema_id: str | None = None
    schema_version: str | None = None
    checksum: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StageResult(BaseModel):
    status: Literal["completed", "incomplete", "failed", "skipped"]
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, str | int | float | bool] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class ModelingStageResult(StageResult):
    @model_validator(mode="after")
    def validate_completed_artifacts(self) -> "ModelingStageResult":
        if self.status == "completed" and not self.artifacts:
            raise ValueError("completed modeling must return at least one artifact")
        return self


class OutputArtifactSpec(BaseModel):
    artifact_id: str
    path: str
    kind: str
    media_type: str | None = None
    required: bool = True
    json_root: str | None = None


class OutputContract(BaseModel):
    contract_id: str
    version: str = "1"
    artifacts: list[OutputArtifactSpec]

    @model_validator(mode="after")
    def validate_unique_outputs(self) -> "OutputContract":
        artifact_ids = [item.artifact_id for item in self.artifacts]
        paths = [item.path for item in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("output artifact ids must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("output artifact paths must be unique")
        return self


class ExplorerRunResult(BaseModel):
    status: Literal["completed", "failed"]
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


class ReviewResult(BaseModel):
    review_id: str
    job_id: str
    status: Literal["pass", "pass_with_warnings", "failed", "skipped"]
    warnings: list[str] = Field(default_factory=list)
    recommended_revision: bool = False
    metrics: dict[str, str | int | float | bool] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_utc)


class DomainManifest(BaseModel):
    domain: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str
    display_name: str
    plugin: str
    capabilities: list[str] = Field(default_factory=list)
    workspace_dirs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_workspace_dirs(self) -> "DomainManifest":
        for relative in self.workspace_dirs:
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"domain workspace directory must be relative and contained: {relative}")
        return self


class DiscoveryJob(BaseModel):
    job_id: str
    spec: JobSpec
    status: Literal["created", "running", "completed", "failed"] = "created"
    workspace_path: str
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    error_code: str | None = None
    error_message: str | None = None


class DiscoveryContext(BaseModel):
    workspace_path: str
    spec: JobSpec
    manifest: DomainManifest
    model: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    domain_context: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    prompt_path: str | None = None


class DomainModeler(Protocol):
    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> ModelingStageResult: ...


class DomainPlugin(Protocol):
    manifest: DomainManifest

    def normalize_spec(self, spec: JobSpec) -> JobSpec: ...

    def prepare(self, spec: JobSpec, workspace_path: str, config: Any) -> StageResult: ...

    def create_modeler(self, spec: JobSpec, config: Any) -> DomainModeler | None: ...

    def build_prompt(self, job: DiscoveryJob, context: DiscoveryContext) -> str: ...

    def output_contract(self, spec: JobSpec) -> OutputContract: ...

    def review(self, job: DiscoveryJob, workspace_path: str) -> ReviewResult: ...

    def result_ids(self, workspace_path: str) -> list[str]: ...


class InsightsExplorer(Protocol):
    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> ExplorerRunResult: ...


__all__ = [
    "ArtifactRef", "DiscoveryContext", "DiscoveryJob", "DomainManifest", "DomainModeler",
    "DomainPlugin", "ExplorerRunResult", "InsightsExplorer", "JobSpec", "ModelingStageResult",
    "OutputArtifactSpec", "OutputContract", "ReviewResult", "StageResult",
]
