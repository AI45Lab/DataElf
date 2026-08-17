from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


AI_INDEX_MODELING_RAW_ACQUISITION_FAILED = "AI_INDEX_MODELING_RAW_ACQUISITION_FAILED"
AI_INDEX_MODELING_RAW_EMPTY = "AI_INDEX_MODELING_RAW_EMPTY"
AI_INDEX_MODELING_STAGE1_INCOMPLETE = "AI_INDEX_MODELING_STAGE1_INCOMPLETE"
AI_INDEX_MODELING_STAGE1_FAILED = "AI_INDEX_MODELING_STAGE1_FAILED"
AI_INDEX_MODELING_STAGE2_INCOMPATIBLE = "AI_INDEX_MODELING_STAGE2_INCOMPATIBLE"
AI_INDEX_MODELING_STAGE2_FAILED = "AI_INDEX_MODELING_STAGE2_FAILED"
AI_INDEX_MODELING_RDF_INVALID = "AI_INDEX_MODELING_RDF_INVALID"
AI_INDEX_MODELING_SUBPROCESS_FAILED = "AI_INDEX_MODELING_SUBPROCESS_FAILED"
AI_INDEX_MODELING_SUBPROCESS_TIMEOUT = "AI_INDEX_MODELING_SUBPROCESS_TIMEOUT"


class RawAcquisitionRecord(BaseModel):
    endpoint: str
    request: dict[str, Any] = Field(default_factory=dict)
    raw_file: str
    item_count: int = 0


class RawAcquisitionResult(BaseModel):
    raw_files: list[str] = Field(default_factory=list)
    records: list[RawAcquisitionRecord] = Field(default_factory=list)


class OntologyRunResult(BaseModel):
    status: Literal["completed", "incomplete", "failed"]
    stage: Literal["stage1", "stage2", "rdf"]
    stage1_run_id: str | None = None
    stage2_run_id: str | None = None
    stage1_bundle: str | None = None
    stage2_bundle: str | None = None
    nquads_path: str | None = None
    rdfxml_path: str | None = None
    ntriples_path: str | None = None
    manifest_path: str | None = None
    validation_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    def rdf_paths(self) -> tuple[Path | None, Path | None, Path | None]:
        nquads = Path(self.nquads_path) if self.nquads_path else None
        rdfxml = Path(self.rdfxml_path) if self.rdfxml_path else None
        ntriples = Path(self.ntriples_path) if self.ntriples_path else None
        return nquads, rdfxml, ntriples


class OntologyRunner(Protocol):
    def run(self, workspace_path: Path) -> OntologyRunResult:
        ...


__all__ = [
    "OntologyRunResult",
    "OntologyRunner",
    "AI_INDEX_MODELING_RAW_ACQUISITION_FAILED",
    "AI_INDEX_MODELING_RAW_EMPTY",
    "AI_INDEX_MODELING_RDF_INVALID",
    "AI_INDEX_MODELING_STAGE1_FAILED",
    "AI_INDEX_MODELING_STAGE1_INCOMPLETE",
    "AI_INDEX_MODELING_STAGE2_FAILED",
    "AI_INDEX_MODELING_STAGE2_INCOMPATIBLE",
    "AI_INDEX_MODELING_SUBPROCESS_FAILED",
    "AI_INDEX_MODELING_SUBPROCESS_TIMEOUT",
    "RawAcquisitionRecord",
    "RawAcquisitionResult",
]
