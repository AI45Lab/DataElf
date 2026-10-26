"""Discovery adapter for domains which have already prepared source artifacts."""
from pathlib import Path

from dataelf.discovery.contracts import ArtifactRef, ModelingStageResult
from .contracts import OntologyRequest, SourceInput
from .config import load_config
from .pipeline import run_ontology
from .templates import load_templates, TemplateDomainError
from .stage_result import stage_accounting


class OntologyModeler:
    def __init__(self, config):
        self.config = config

    def run(self, job, context):
        workspace = Path(context.workspace_path)
        try:
            config = load_config(self.config.ontology.ontology_config)
            if config.mode == "template":
                load_templates(config.templates, domain=job.spec.domain)
        except TemplateDomainError as exc:
            return ModelingStageResult(status="failed", error_code="ONTOLOGY_TEMPLATE_DOMAIN_MISMATCH", error_message=str(exc))
        supplied = job.spec.inputs.get("ontology_sources")
        sources = [SourceInput.model_validate(item) for item in supplied] if supplied else [
            SourceInput(source_id=artifact.artifact_id, path=Path(artifact.path))
            for artifact in context.artifacts if artifact.kind == "source_data"
            and Path(artifact.path).suffix.lower() in {".json", ".jsonl", ".csv"}]
        if not sources:
            return ModelingStageResult(status="failed", error_code="ONTOLOGY_SOURCES_REQUIRED",
                error_message="This domain must prepare JSON/JSONL/CSV source snapshots before ontology modeling.")
        agent = None
        if config.mode == "dynamic":
            from .runtime import PiOntologyAgent
            agent = PiOntologyAgent(self.config)
        result = run_ontology(OntologyRequest(domain=job.spec.domain, workspace=workspace, sources=sources, objective=job.spec.objective), config=config, agent=agent)
        if result.status != "completed":
            return ModelingStageResult(status=result.status, error_code=result.error_code, error_message=result.error_message)
        artifacts = []
        for label, path, kind in (("rdf", result.nquads_path, "ontology_rdf"), ("contract", result.contract_path or str(Path(result.stage1_bundle) / "contract.json"), "modeling_contract"),
                                  ("manifest", result.manifest_path, "modeling_manifest"), ("validation", result.validation_path, "modeling_validation")):
            artifacts.append(ArtifactRef(artifact_id="ontology_" + label, kind=kind, path=Path(path).relative_to(workspace.resolve()).as_posix(),
                                         role="evidence", producer_stage="domain_modeling"))
        if result.template_path:
            artifacts.append(ArtifactRef(artifact_id="ontology_template", kind="ontology_template",
                path=Path(result.template_path).relative_to(workspace.resolve()).as_posix(), role="output",
                producer_stage="domain_modeling", media_type="application/json"))
        counters, details = stage_accounting(result.details.get("metrics", {}))
        return ModelingStageResult(status="completed", artifacts=artifacts, metrics=counters, graph_ref=result.graph_ref, context={"rdf_path": artifacts[0].path,"graph_ref":result.graph_ref,"graph_root":str(Path(result.bundle_path).parents[2]), **details})
