from __future__ import annotations

import json
from pathlib import Path

from dataelf.discovery.artifacts import relative_artifact_path
from dataelf.discovery.contracts import ArtifactRef, DiscoveryContext, DiscoveryJob, ModelingStageResult
from dataelf.domains.ai_index.config import AIIndexDomainConfig
from dataelf.domains.ai_index.modeling.acquisition import AIIndexRawCollector
from dataelf.domains.ai_index.modeling.contracts import (
    AI_INDEX_MODELING_RAW_ACQUISITION_FAILED,
    AI_INDEX_MODELING_RAW_EMPTY,
    AI_INDEX_MODELING_RDF_INVALID,
    AI_INDEX_MODELING_STAGE1_INCOMPLETE,
    AI_INDEX_MODELING_STAGE2_FAILED,
    OntologyRunResult,
    RawAcquisitionResult,
)
from dataelf.domains.ai_index.modeling.ontology_runner import validate_ontology_result
from dataelf.domains.ai_index.modeling.state import AIIndexModelingStateStore
from dataelf.domains.ai_index.modeling.subprocess_runner import run_ontology_subprocess


EXPECTED_ENDPOINTS = {
    "/openapi/paper/search",
    "/openapi/institutions/search",
    "/openapi/scholar/search",
}


class AIIndexModeler:
    def __init__(self, domain_config: AIIndexDomainConfig, runtime_env: dict[str, str]):
        self.domain_config = domain_config
        self.runtime_env = runtime_env
        self.config = domain_config.modeling
        self.collector = AIIndexRawCollector(
            mode=domain_config.source.mode,
            base_url=domain_config.source.base_url,
            api_key=domain_config.source.api_key,
            fixtures_dir=domain_config.source.fixtures_dir,
            page_size=self.config.raw_page_size,
        )

    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> ModelingStageResult:
        workspace = Path(context.workspace_path).resolve()
        state = AIIndexModelingStateStore(workspace)
        state.transition("collecting_raw", jobId=job.job_id, stage="acquisition")
        try:
            acquisition = self.collector.collect(job, workspace)
        except Exception as exc:
            return self._fail(
                state,
                AI_INDEX_MODELING_RAW_ACQUISITION_FAILED,
                f"AI Index raw acquisition failed: {exc}",
                stage="acquisition",
            )
        invalid_reason = _invalid_acquisition_reason(acquisition)
        if invalid_reason:
            return self._fail(state, AI_INDEX_MODELING_RAW_EMPTY, invalid_reason, stage="acquisition")
        item_count = sum(record.item_count for record in acquisition.records)
        state.transition(
            "raw_acquired",
            stage="acquisition",
            artifactPaths={"rawDirectory": str((workspace / "raw" / "ai_index").resolve())},
            metrics={"rawFileCount": len(acquisition.raw_files), "rawItemCount": item_count},
        )
        state.transition(
            "ontology_running",
            stage="stage1",
            metrics={"mode": "template" if self.config.ontology_template else "dynamic"},
        )
        try:
            ontology = run_ontology_subprocess(workspace, self.config, self.runtime_env)
        except KeyboardInterrupt:
            stage = _worker_stage(workspace)
            state.transition(
                "paused_interrupted",
                stage=stage,
                error={"code": _interrupted_code(stage), "message": "AI Index modeling was interrupted."},
            )
            raise
        if ontology.status != "completed":
            return self._fail_from_ontology(state, ontology)
        invalid_reason = validate_ontology_result(ontology)
        if invalid_reason:
            return self._fail(
                state,
                AI_INDEX_MODELING_RDF_INVALID,
                invalid_reason,
                stage="rdf",
                ontology=ontology,
            )
        artifacts = _modeling_artifacts(workspace, ontology)
        primary = next(artifact for artifact in artifacts if artifact.artifact_id == "ai_index_rdf_nquads")
        state.transition(
            "completed",
            stage="rdf",
            runIds={"stage1": ontology.stage1_run_id, "stage2": ontology.stage2_run_id},
            artifactPaths={
                "primary": str((workspace / primary.path).resolve()),
                "manifest": ontology.manifest_path,
                "validation": ontology.validation_path,
            },
            metrics={"artifactCount": len(artifacts)},
        )
        return ModelingStageResult(
            status="completed",
            artifacts=artifacts,
            metrics={
                "stage1Mode": "template" if self.config.ontology_template else "dynamic",
                "stage1AttemptCount": 0 if self.config.ontology_template else 1,
                **({"stage1ModelCalls": 0} if self.config.ontology_template else {}),
            },
        )

    @staticmethod
    def _fail(
        state: AIIndexModelingStateStore,
        code: str,
        message: str,
        *,
        stage: str,
        ontology: OntologyRunResult | None = None,
    ) -> ModelingStageResult:
        updates: dict[str, object] = {"stage": stage, "error": {"code": code, "message": message}}
        if ontology is not None:
            updates["runIds"] = {"stage1": ontology.stage1_run_id, "stage2": ontology.stage2_run_id}
            updates["artifactPaths"] = _compact_ontology_paths(ontology)
        state.transition("failed", **updates)
        return ModelingStageResult(status="failed", error_code=code, error_message=message)

    def _fail_from_ontology(
        self,
        state: AIIndexModelingStateStore,
        ontology: OntologyRunResult,
    ) -> ModelingStageResult:
        status = "incomplete" if ontology.status == "incomplete" else "failed"
        code = ontology.error_code or AI_INDEX_MODELING_RDF_INVALID
        message = ontology.error_message or "AI Index ontology modeling did not complete."
        state.transition(
            status,
            stage=ontology.stage,
            runIds={"stage1": ontology.stage1_run_id, "stage2": ontology.stage2_run_id},
            artifactPaths=_compact_ontology_paths(ontology),
            error={"code": code, "message": message},
        )
        return ModelingStageResult(status=status, error_code=code, error_message=message)


def _invalid_acquisition_reason(acquisition: RawAcquisitionResult) -> str | None:
    acquired = {
        record.endpoint
        for record in acquisition.records
        if record.raw_file and Path(record.raw_file).is_file() and Path(record.raw_file).stat().st_size > 0
    }
    item_count = sum(record.item_count for record in acquisition.records)
    if not acquisition.raw_files or acquired != EXPECTED_ENDPOINTS or item_count <= 0:
        return "AI Index modeling requires three non-empty search response files and at least one record."
    return None


def _modeling_artifacts(workspace: Path, ontology: OntologyRunResult) -> list[ArtifactRef]:
    assert ontology.nquads_path and ontology.stage1_bundle
    run_id = ontology.stage2_run_id or ontology.stage1_run_id or "ai_index_modeling"
    values = {
        "ai_index_rdf_nquads": ("ontology_rdf", ontology.nquads_path, "application/n-quads"),
        "ai_index_rdf_ntriples": ("ontology_rdf", ontology.ntriples_path, "application/n-triples"),
        "ai_index_rdfxml": ("ontology_rdf", ontology.rdfxml_path, "application/rdf+xml"),
        "ai_index_modeling_manifest": ("modeling_manifest", ontology.manifest_path, "application/json"),
        "ai_index_modeling_validation": ("modeling_validation", ontology.validation_path, "application/json"),
        "ai_index_stage1_bundle": ("modeling_bundle", ontology.stage1_bundle, None),
        "ai_index_stage2_bundle": ("modeling_bundle", ontology.stage2_bundle, None),
    }
    return [
        ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            path=relative_artifact_path(workspace, path),
            role="evidence",
            producer_stage="domain_modeling",
            media_type=media_type,
            provenance={"domain": "ai_index", "run_id": run_id},
        )
        for artifact_id, (kind, path, media_type) in values.items()
        if path
    ]


def _compact_ontology_paths(ontology: OntologyRunResult) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "stage1Bundle": ontology.stage1_bundle,
            "stage2Bundle": ontology.stage2_bundle,
            "nquads": ontology.nquads_path,
            "manifest": ontology.manifest_path,
            "validation": ontology.validation_path,
        }.items()
        if value
    }


def _worker_stage(workspace: Path) -> str:
    try:
        payload = json.loads(
            (workspace / "modeling" / "ai_index" / "worker_progress.json").read_text(encoding="utf-8")
        )
        if payload.get("stage") in {"stage1", "stage2", "rdf"}:
            return str(payload["stage"])
    except (OSError, json.JSONDecodeError):
        pass
    return "stage1"


def _interrupted_code(stage: str) -> str:
    if stage == "stage1":
        return AI_INDEX_MODELING_STAGE1_INCOMPLETE
    return AI_INDEX_MODELING_STAGE2_FAILED


__all__ = ["AIIndexModeler"]
