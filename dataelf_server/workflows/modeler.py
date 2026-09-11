from __future__ import annotations

import json
from pathlib import Path

from dataelf.discovery.contracts import ArtifactRef, ModelingStageResult
from dataelf.discovery.run_control import check_cancelled
from dataelf_server.ontology.scope_v2_template import ScopeV2TemplateOntologyRunner


class ServerModeler:
    def run(self, job, context) -> ModelingStageResult:
        check_cancelled()
        workspace = Path(context.workspace_path)
        result = ScopeV2TemplateOntologyRunner().run(workspace)
        if result.status != "completed":
            return ModelingStageResult(status="failed", error_code="SERVER_MODELING_FAILED", error_message=result.error_message)
        artifacts = []
        root = workspace / "modeling" / "server"
        for index, path in enumerate(sorted(root.rglob("*"))):
            if not path.is_file():
                continue
            relative = path.relative_to(workspace).as_posix()
            media = {".json": "application/json", ".nq": "application/n-quads", ".nt": "application/n-triples", ".rdf": "application/rdf+xml"}.get(path.suffix)
            artifacts.append(ArtifactRef(
                artifact_id=f"server_modeling_{index}", kind="ontology_rdf" if path.suffix in {".nq", ".nt", ".rdf"} else "modeling_evidence",
                path=relative, role="evidence", producer_stage="domain_modeling", media_type=media,
            ))
        rdf = Path(result.nquads_path).relative_to(workspace).as_posix()
        scope_result = Path(context.domain_context["scope_v2_result_path"]).resolve().relative_to(workspace.resolve()).as_posix()
        inputs = workspace / "artifacts" / "server_inputs.json"
        inputs.write_text(json.dumps({"rdf_path": rdf, "scope_result_path": scope_result, "scope": "scope_v2"}, indent=2) + "\n", encoding="utf-8")
        artifacts.append(ArtifactRef(artifact_id="server_inputs", kind="modeling_input_index", path="artifacts/server_inputs.json", role="evidence", producer_stage="domain_modeling", media_type="application/json"))
        return ModelingStageResult(status="completed", artifacts=artifacts, context={"rdf_path": rdf}, metrics={"model_calls": 0}, env={"DATAELF_SERVER_RDF": str(workspace / rdf)})
