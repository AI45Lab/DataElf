"""Persisted experiments are distinct from complete, reviewable candidates."""
import uuid

from .contracts import OntologyContract, OntologyRequest
from .draft import draft_state, save_draft
from .engine import materialize, require_valid_graph
from .errors import OntologyError, error_feedback
from .sources import SourceCatalog, digest, write_json
from .templates import require_domain
from .workflow_state import checkpoint_directory


def validation_fingerprint(contract, dataset, lineage, scope, source_fingerprint):
    from .graph.store import content_hash
    from .engine import iri
    from rdflib import Dataset
    business=Dataset()
    schema_graph=str(iri(contract.namespace,contract.graphs['schema']))
    for s,p,o,g in dataset.quads((None,None,None,None)):
        if str(g)!=schema_graph:business.graph(g).add((s,p,o))
    return digest({'validated_rdf':content_hash(business),'scope':scope,'source_fingerprint':source_fingerprint,
        'checks':{'constraints':[c.model_dump(mode='json') for c in contract.constraints],
            'queries':[q.model_dump(mode='json') for q in contract.queries]},
        'coverage':sorted((r['source'],r['file'],r['pointer'],r['recordHash']) for r in lineage['records'])})


def trial_summary(result):
    lineage = result.get('lineage', {})
    skips = lineage.get('skippedGroups', [])
    counts = {}
    for item in skips:
        key = item['contract_path']
        counts[key] = counts.get(key, 0) + 1
    return {k: result[k] for k in ('trial_id', 'scope', 'status', 'draft_hash', 'mapping_ids', 'content_fingerprint', 'candidate_hash',
            'deferred_checks', 'validation') if k in result} | {
        'skipped_group_count': len(skips), 'skipped_groups_by_condition': counts,
        'next_request': {'tool': 'ontology_inspect', 'arguments': {
            'resource': 'trial', 'trial_id': result['trial_id'], 'offset': 0}},
        'publication_eligible': False,
        'meaning': 'Execution experiment only. Complete submission, final validation and independent review are still required.'}


def execute_trial(context_path, context, payload):
    from .tools import prepare_candidate, verify_evidence
    from .task_sources import effective_request
    request = effective_request(context)
    previous = draft_state(context_path, context)
    if payload.get('base_hash') is not None and payload['base_hash'] != digest(previous):
        raise ValueError('stale draft base_hash; inspect the checkpoint before retrying')
    contract = OntologyContract.model_validate(payload['contract'] if payload.get('contract') is not None else previous)
    require_domain(contract.domain, request.domain)
    mapping_ids = payload['mapping_ids'] if payload['scope'] == 'component' else [m.id for m in contract.mappings]
    known = {m.id for m in contract.mappings}
    if set(mapping_ids) - known:
        raise ValueError('unknown trial mapping_ids: ' + str(sorted(set(mapping_ids) - known)))
    # Save the actual submitted document, never the focused projection. A later
    # execution failure leaves it editable while the last successful graph stays.
    draft_hash = save_draft(context_path, contract.model_dump(mode='json'))
    trial_id = uuid.uuid4().hex
    directory = checkpoint_directory(context_path, context) / 'trials' / trial_id
    directory.mkdir(parents=True)
    write_json(directory / 'contract.json', contract.model_dump(mode='json'))
    result = {'trial_id': trial_id, 'scope': payload['scope'], 'status': 'invalid',
        'draft_hash': draft_hash, 'mapping_ids': mapping_ids, 'source_fingerprint': context['source_fingerprint'],
        'deferred_checks': ['other mappings and source coverage', 'declared cardinality constraints',
            'declared business queries', 'cross-component completeness'] if payload['scope'] == 'component' else []}
    try:
        if payload['scope'] == 'component':
            selected = [m for m in contract.mappings if m.id in mapping_ids]
            focused = contract.model_copy(update={'mappings': selected, 'constraints': [], 'queries': []})
            sources = [s for s in request.sources if s.source_id in {m.source for m in selected}]
            catalog = SourceCatalog(request.workspace, sources)
            for mapping in selected:
                verify_evidence(mapping, catalog)
            ds, lineage, validation = materialize(focused, catalog)
            ds.serialize(destination=directory / 'graph.nq', format='nquads')
            result.update(lineage=lineage, validation={**validation, 'scope': 'component'})
            require_valid_graph(validation)
            executed_contract=focused
        else:
            prepared = prepare_candidate(context_path, context, contract.model_dump(mode='json'))
            result.update(prepared)
            if context.get('graph_ref'):
                from .graph import GraphService
                service = GraphService(context['graph_root'], request.workspace)
                state = service.state(context['graph_ref'])
                result['candidate_hash']=service.binding(state)
                result['lineage'] = state.get('projection_lineage', {})
                ds=service.dataset(context['graph_ref'])
                ds.serialize(destination=directory / 'graph.nq', format='nquads')
            else:
                ds, result['lineage'], _ = materialize(contract, SourceCatalog(request.workspace, request.sources))
            executed_contract=contract
        if request.workflow_policy != 'legacy':
            request=effective_request(context,executed_contract)
            result['source_fingerprint']=SourceCatalog(request.workspace,request.sources).fingerprint
        result['status'] = 'valid'
        result['content_fingerprint']=validation_fingerprint(executed_contract,ds,result['lineage'],result['scope'],result['source_fingerprint'])
    except (ValueError, TypeError) as exc:
        feedback = error_feedback(exc, 'contract')
        result['failure'] = feedback
        write_json(directory / 'result.json', result)
        write_json(checkpoint_directory(context_path, context) / 'last_trial.json', trial_summary(result))
        issues = feedback.get('issues') or [{'message': str(exc)}]
        # Keep the original component/evidence location, and attach a compact
        # recovery handle rather than echoing the entire contract again.
        raise OntologyError(feedback.get('code', 'ONTOLOGY_TRIAL_INVALID'), feedback['target'],
            [{**issue, 'trial_id': trial_id, 'draft_hash': draft_hash,
              'required_change': 'Inspect this trial and patch the saved component using draft_hash; then execute the draft again. No full-contract retransmission is needed.'}
             for issue in issues]) from exc
    write_json(directory / 'result.json', result)
    summary = trial_summary(result)
    write_json(checkpoint_directory(context_path, context) / 'last_trial.json', summary)
    write_json(checkpoint_directory(context_path, context) / 'last_successful_trial.json', summary)
    if result['scope']=='full':write_json(checkpoint_directory(context_path, context) / 'last_complete_trial.json', summary)
    return {**summary, **({'graph_ref': result['graph_ref']} if 'graph_ref' in result else {})}
