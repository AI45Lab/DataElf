"""Appendable discovery catalog versus exact executable dependencies."""
from .capabilities import skill_review
from .contracts import OntologyRequest
from .sources import SourceCatalog


def effective_request(context, contract=None):
    request = context if isinstance(context, OntologyRequest) else OntologyRequest.model_validate(context['request'])
    if not skill_review(request): return request
    from dataelf.domains.ai_index.evidence import EvidenceCatalog
    catalog = EvidenceCatalog(request.workspace)
    sources = {s.source_id: s for s in request.sources}
    sources.update({s.source_id: s for s in catalog.sources()})
    if contract is not None:
        if isinstance(contract,dict):
            from .contracts import OntologyContract
            contract=OntologyContract.model_validate(contract)
        ids = {m.source for m in contract.mappings}
        missing = ids - set(sources)
        if missing: raise ValueError('Mappings reference unregistered sources: ' + str(sorted(missing)))
        sources = {ident: sources[ident] for ident in sorted(ids)}
        if not sources: raise ValueError('Complete graph requires evidence-bound executable mappings')
    return request.model_copy(update={'sources': list(sources.values())})


def task_catalog(context, contract=None):
    request = effective_request(context, contract)
    catalog=SourceCatalog(request.workspace,request.sources,allow_empty=skill_review(request) and contract is None)
    if skill_review(request):
        from .sources import file_hash
        for source in request.sources:
            relative=source.metadata.get('query_record_file')
            if relative:
                path=(request.workspace/relative).resolve();expected=source.metadata['query_record_hash']
                if not path.is_relative_to(request.workspace.resolve()) or file_hash(path)!=expected:raise ValueError('Source query metadata changed')
                catalog.hashes[relative]=expected
    return request,catalog
