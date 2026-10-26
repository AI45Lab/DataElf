from __future__ import annotations

from pathlib import Path

from dataelf.discovery.artifacts import relative_artifact_path
from dataelf.discovery.contracts import ArtifactRef, DiscoveryContext, DiscoveryJob, ModelingStageResult
from dataelf.domains.ai_index.config import AIIndexDomainConfig
from dataelf.domains.ai_index.acquisition import AIIndexRawCollector, RawAcquisitionResult
from dataelf.domains.ai_index.modeling_state import AIIndexModelingStateStore
from dataelf.ontology.legacy_ai_index import load_modeling_config, collection_page_size
from dataelf.ontology.artifacts import validate_result
from dataelf.ontology.stage_result import stage_accounting
from dataelf.ontology import OntologyRequest, OntologyRunResult, SourceInput, run_ontology


EXPECTED_ENDPOINTS = {
    "/openapi/paper/search",
    "/openapi/institutions/search",
    "/openapi/scholar/search",
}


class AIIndexModeler:
    def __init__(self, domain_config: AIIndexDomainConfig, runtime_env: dict[str, str], runtime_config=None):
        self.domain_config = domain_config
        self.runtime_env = runtime_env
        self.config = domain_config.modeling
        self.ontology_config = load_modeling_config(self.config.ontology_config)
        if self.ontology_config.save_as_template:
            import warnings
            warnings.warn('AI Index CLI no longer exports templates; versioned contracts remain in graph revisions', DeprecationWarning, stacklevel=2)
            self.ontology_config=self.ontology_config.model_copy(update={'save_as_template':False})
        self.runtime_config = runtime_config
        self.collector = AIIndexRawCollector(
            mode=domain_config.source.mode,
            base_url=domain_config.source.base_url,
            api_key=domain_config.source.api_key,
            fixtures_dir=domain_config.source.fixtures_dir,
            page_size=collection_page_size(domain_config.source, self.config.ontology_config),
        )

    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> ModelingStageResult:
        workspace = Path(context.workspace_path).resolve()
        if job.spec.inputs.get("resume_graph_manifest"):
            from dataelf.ontology.resume import resume_published_graph
            return resume_published_graph(workspace, job.spec.inputs["resume_graph_manifest"],workflow_policy=job.spec.workflow_policy)
        from dataelf.ontology.capabilities import skill_review
        if skill_review(job.spec):
            return self._run_skill(job,context,workspace)
        state = AIIndexModelingStateStore(workspace)
        state.transition("collecting_raw", jobId=job.job_id, stage="acquisition")
        try:
            acquisition = self.collector.collect(job, workspace)
        except Exception as exc:
            return self._fail(
                state,
                "AI_INDEX_MODELING_RAW_ACQUISITION_FAILED",
                f"AI Index raw acquisition failed: {exc}",
                stage="acquisition",
            )
        invalid_reason = _invalid_acquisition_reason(acquisition)
        if invalid_reason:
            return self._fail(state, "AI_INDEX_MODELING_RAW_EMPTY", invalid_reason, stage="acquisition")
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
            metrics={"mode": self.ontology_config.mode},
        )
        try:
            agent = None
            if self.ontology_config.mode == "dynamic":
                from dataelf.ontology.runtime import PiOntologyAgent
                agent = PiOntologyAgent(self.runtime_config)
            sources = [SourceInput(source_id=record.endpoint, path=Path(record.raw_file))
                       for record in acquisition.records]
            ontology = run_ontology(OntologyRequest(domain=job.spec.domain, workspace=workspace, sources=sources, objective=job.spec.objective),
                                   config=self.ontology_config,
                                   agent=agent,
                                   progress=lambda stage: state.transition("ontology_running", stage=stage))
        except KeyboardInterrupt:
            stage = str(state.payload.get("stage", "stage1"))
            state.transition(
                "paused_interrupted",
                stage=stage,
                error={"code": "ONTOLOGY_INTERRUPTED", "message": "AI Index modeling was interrupted."},
            )
            raise
        if ontology.status != "completed":
            return self._fail_from_ontology(state, ontology)
        invalid_reason = validate_result(ontology)
        if invalid_reason:
            return self._fail(
                state,
                "AI_INDEX_MODELING_RDF_INVALID",
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
        counters, details = stage_accounting(ontology.details.get("metrics", {}))
        return ModelingStageResult(
            status="completed",
            artifacts=artifacts,
            graph_ref=ontology.graph_ref,
            context={"graph_ref":ontology.graph_ref,"graph_root":str(Path(ontology.bundle_path).parents[2]), **details},
            metrics={
                "stage1Mode": self.ontology_config.mode,
                **counters,
            },
        )

    @staticmethod
    def _skill_request(job,workspace):
        return OntologyRequest(domain=job.spec.domain,workspace=workspace,sources=[],
            objective=job.spec.objective,workflow_policy=job.spec.workflow_policy,
            task_context={'parameters':job.spec.parameters,'constraints':job.spec.constraints})

    def _run_skill(self,job,context,workspace):
        from dataelf.ontology.runtime import PiOntologyAgent
        state=AIIndexModelingStateStore(workspace)
        state.transition('ontology_running',jobId=job.job_id,stage='modeling')
        ontology=run_ontology(self._skill_request(job,workspace),config=self.ontology_config,
            agent=PiOntologyAgent(self.runtime_config,runtime_env=context.env,agent_resources=context.agent_resources),
            progress=lambda stage:state.transition('ontology_running',stage=stage))
        if ontology.status!='completed':return self._fail_from_ontology(state,ontology)
        invalid=validate_result(ontology)
        if invalid:return self._fail(state,'AI_INDEX_MODELING_RDF_INVALID',invalid,stage='rdf',ontology=ontology)
        artifacts=_modeling_artifacts(workspace,ontology)
        counters,details=stage_accounting(ontology.details.get('metrics',{}))
        state.transition('completed',stage='rdf',artifactPaths=_compact_ontology_paths(ontology),metrics=counters)
        return ModelingStageResult(status='completed',artifacts=artifacts,graph_ref=ontology.graph_ref,
            context={'graph_ref':ontology.graph_ref,'graph_root':str(Path(ontology.bundle_path).parents[2]),**details},
            metrics={'stage1Mode':'dynamic',**counters})

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
        code = ontology.error_code or "AI_INDEX_MODELING_RDF_INVALID"
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
        "ai_index_modeling_contract": ("modeling_contract", ontology.contract_path, "application/json"),
        "ai_index_stage1_bundle": ("modeling_bundle", ontology.stage1_bundle, None),
        "ai_index_stage2_bundle": ("modeling_bundle", ontology.stage2_bundle, None),
        "ai_index_ontology_template": ("ontology_template", ontology.template_path, "application/json"),
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



__all__ = ["AIIndexModeler"]
