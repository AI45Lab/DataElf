"""Verify explicit published bundles, never implicit latest-run pointers."""
import json
from pathlib import Path

from .contracts import OntologyContract, ExtractionPlan, OntologyRunResult
from .sources import digest, file_hash


def validate_result(result: OntologyRunResult) -> str | None:
    try:
        if result.status != "completed":
            return result.error_message or "ontology did not complete"
        if not result.manifest_path:return 'ontology result lacks manifest'
        manifest=json.loads(Path(result.manifest_path).read_text())
        if manifest.get('schemaVersion')=='dataelf.ontology.graph.manifest.v2':
            from .graph import GraphService
            if not result.graph_ref or not result.bundle_path or not result.contract_path:return 'new graph result lacks explicit revision references'
            bundle=Path(result.bundle_path).resolve()
            service=GraphService(bundle.parents[2],bundle.parents[4]);state=service.state(result.graph_ref)
            if service.store.path(service.ref(result.graph_ref)).resolve()!=bundle:return 'bundle path differs from revision reference'
            if bundle.name!=result.revision_id or manifest.get('revision_id')!=result.revision_id:return 'revision mismatch'
            required={'graph.nq','graph.nt','graph.rdf','contract.json','state.json','validation.json','review.json','provenance.json','changes.json'}
            if not required<=set(manifest.get('artifacts',{})):return 'manifest omits required artifacts'
            paths=[(result.nquads_path,'graph.nq'),(result.ntriples_path,'graph.nt'),(result.rdfxml_path,'graph.rdf'),
                   (result.contract_path,'contract.json'),(result.validation_path,'validation.json'),(result.manifest_path,'manifest.json')]
            if any(not path or Path(path).resolve()!=bundle/name for path,name in paths):return 'result path does not identify the published artifact'
            if manifest.get('candidate_hash')!=service.binding(state):return 'candidate binding mismatch'
            if manifest.get('contract_hash')!=digest(state.get('contract')):return 'contract hash mismatch'
            if manifest.get('templateSaved') and (not result.template_path or Path(result.template_path).resolve()!=bundle/'template.json'):return 'template result path mismatch'
            if json.loads(Path(result.validation_path).read_text()).get('status')!='valid':return 'published validation is invalid'
            return None
        if manifest.get('schemaVersion')!='dataelf.ontology.manifest.v1' or manifest.get('status')!='completed':return 'unsupported or incomplete ontology manifest'
        if not all((result.stage1_bundle,result.stage2_bundle,result.nquads_path,result.ntriples_path,result.rdfxml_path,result.validation_path)):
            return 'ontology result is missing published artifact paths'
        first,second=Path(result.stage1_bundle).resolve(),Path(result.stage2_bundle).resolve()
        if first.parent!=second.parent:return 'stage bundles do not belong to the same publication'
        required = {"stage1/contract.json", "stage1/review.json", "stage1/validation.json", "stage2/plan.json",
                    "stage2/review.json", "stage2/validation.json", "stage2/graph.nq", "stage2/graph.nt", "stage2/graph.rdf", "stage2/projection_lineage.json"}
        hashes = manifest.get("artifacts", {})
        if manifest.get("templateSaved"):
            required.add("stage1/template.json")
            if not result.template_path or Path(result.template_path).resolve() != first / "template.json":
                return "template result path does not identify the published artifact"
        elif result.template_path:
            return "template result is not declared in manifest"
        if not required <= set(hashes):
            return "manifest omits required artifacts"
        for relative, expected in hashes.items():
            path = (first.parent / relative).resolve()
            if not path.is_relative_to(first.parent) or file_hash(path) != expected:
                return f"published artifact hash mismatch: {relative}"
        contract = OntologyContract.model_validate_json((first / "contract.json").read_text())
        if contract.domain != manifest.get("domain"):
            return "published contract domain differs from manifest"
        plan = ExtractionPlan.model_validate_json((second / "plan.json").read_text())
        if digest(contract.model_dump(mode="json")) != manifest["contractHash"] or plan.contract_hash != manifest["contractHash"]:
            return "published plan/contract hash mismatch"
        if json.loads(Path(result.validation_path).read_text()).get("status") != "valid":
            return "published graph validation is not valid"
        for path, name in ((result.nquads_path, "graph.nq"), (result.ntriples_path, "graph.nt"), (result.rdfxml_path, "graph.rdf")):
            if Path(path).resolve() != second / name:
                return "RDF result path does not identify the published artifact"
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"invalid published ontology: {exc}"
