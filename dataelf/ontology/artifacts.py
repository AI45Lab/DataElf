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
        return 'unsupported ontology manifest; expected a published graph revision'
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"invalid published ontology: {exc}"
