"""Exact successful reads, shared across ontology sessions, never review verdicts."""
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path

from .resources import json_page
from .sources import digest,file_hash


def directory(context_path, context):
    # The directory comes from trusted runtime context, never tool arguments.
    return Path(context.get('read_cache_directory') or Path(context_path).parent / 'read_cache')


def save_read(context_path, context, tool, arguments, result, graph_bindings=None):
    value = {k: v for k, v in result.items() if k not in
             {'available_tools', 'workflow', 'draft_hash', 'progress_key', 'cached_read'}}
    record = {'tool': tool, 'arguments': arguments, 'result': value,
              'source_fingerprint': context.get('source_fingerprint'),
              'graph_bindings': graph_bindings or []}
    from .capabilities import skill_review
    if skill_review(context):
        from .task_sources import task_catalog
        _,catalog=task_catalog(context)
        record['source_dependencies']=catalog.hashes
    result_id = digest(record)
    folder=directory(context_path,context);folder.mkdir(parents=True,exist_ok=True)
    path=folder/(result_id+'.json')
    # Independent temporary names also permit concurrent identical reads.
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=folder,delete=False) as temporary:
        try:
            json.dump(record,temporary,ensure_ascii=False,default=str)
            temporary.flush();os.fsync(temporary.fileno())
            os.replace(temporary.name,path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)
    return {**result, 'cached_read': {'result_id': result_id,
        'next_request': request(result_id),
        'meaning': 'Exact returned result; a read is not an approval. Original truncation remains authoritative.'}}


def request(result_id, offset=0, max_bytes=8000):
    return {'tool': 'ontology_evidence', 'arguments': {
        'operation': 'cached', 'result_id': result_id, 'offset': offset, 'max_bytes': max_bytes}}


def read(context_path, context, payload):
    path = directory(context_path, context) / (payload['result_id'] + '.json')
    if not path.exists():
        raise ValueError('cached result is not in this ontology run')
    record = json.loads(path.read_text())
    if digest(record) != payload['result_id']:
        raise ValueError('cached result integrity mismatch')
    from .capabilities import skill_review
    if skill_review(context) and 'source_dependencies' in record:
        root=Path(context['request']['workspace']).resolve()
        for relative,expected in record['source_dependencies'].items():
            path=(root/relative).resolve()
            if not path.is_relative_to(root) or file_hash(path)!=expected:
                raise ValueError('cached evidence dependency changed: '+relative)
    elif record['source_fingerprint'] != context.get('source_fingerprint'):
        raise ValueError('cached result belongs to a different source snapshot')
    if record['graph_bindings']:
        from .graph import GraphService
        service = GraphService(context['graph_root'], context['request']['workspace'])
        for binding in record['graph_bindings']:
            if binding['graph_ref']['graph_id'] != context['graph_ref']['graph_id']:
                raise ValueError('cached graph is outside this task')
            state=service.state(binding['graph_ref'])
            if service.binding(state) != binding['candidate_hash']:
                raise ValueError('cached result refers to a changed graph candidate')
            # Supplemental maintenance evidence need not be in the original
            # modeling SourceCatalog; it is nevertheless frozen by the graph.
            for file,expected in state.get('source_hashes',{}).items():
                source=(service.evidence_root/file).resolve()
                if not source.is_relative_to(service.evidence_root) or not source.is_file() or file_hash(source)!=expected:
                    raise ValueError('cached graph evidence snapshot changed or absent')
    page = json_page(record['result'], payload['offset'], payload['max_bytes'])
    return {'resource': 'cached_read', 'result_id': payload['result_id'],
        'original_tool': record['tool'], 'original_arguments': record['arguments'],
        'source_fingerprint': record['source_fingerprint'],
        'graph_bindings': record['graph_bindings'], **page,
        'next_request': request(payload['result_id'], page['next_offset'], payload['max_bytes'])
            if page['next_offset'] is not None else None,
        'meaning': 'Pages reconstruct the original returned result, including its truncation markers; not an approval.'}


def context_feedback(feedback):
    """Full diagnostics stay on disk; recovery state appears only once in context."""
    result = deepcopy(feedback)
    for entry in result:
        for issue in entry.get('issues', []):
            issue.pop('checkpoint_state', None)
    return result


def evidence_excerpt(context_path,context,result_id,max_bytes=1800):
    """Bounded already-returned evidence, retaining origin and truncation status."""
    # Reuse all snapshot, integrity and candidate checks before using the cache.
    verified=read(context_path,context,{'result_id':result_id,'offset':0,'max_bytes':512})
    record=json.loads((directory(context_path,context)/(result_id+'.json')).read_text())
    original=record['result'];value=original
    if original.get('encoding')=='json' and original.get('offset',0)==0 and not original.get('truncated'):
        try:value=json.loads(original['text'])
        except (ValueError,KeyError):pass
    observations,cut=returned_observations(value,max_bytes)
    return {'result_id':result_id,'original_tool':record['tool'],'original_arguments':record['arguments'],
        'source_fingerprint':record['source_fingerprint'],'graph_bindings':verified['graph_bindings'],
        'observations':observations,'excerpt_truncated':cut,'original_truncated':original.get('truncated'),
        'original_page':{k:original[k] for k in ('encoding','offset','next_offset','total_bytes') if k in original},
        'meaning':'Projection of previously returned evidence. result_pointer addresses the decoded returned value, not the source file. Omitted values are unknown here. No synthetic preview objects replace source values. Not a model decision or approval.',
        'next_request':request(result_id)}


def returned_observations(value,max_bytes):
    """Exact leaves and explicit string fragments, never fake objects at data paths."""
    from collections import deque
    nodes=[];pending=deque([('',value)]);bounded=False
    while pending and len(nodes)<5000:
        path,node=pending.popleft();nodes.append((path,node))
        if isinstance(node,dict):
            pending.extend((path+'/'+k.replace('~','~0').replace('/','~1'),v) for k,v in node.items())
        elif isinstance(node,list):pending.extend((path+'/'+str(i),v) for i,v in enumerate(node))
    bounded=bool(pending)
    # Show a few actually observed fields from record-shaped arrays before envelope
    # metadata. This is structural sampling, without any domain vocabulary.
    priority=[]
    for path,node in nodes:
        if isinstance(node,list) and node and isinstance(node[0],dict):
            priority.extend((path+'/0/'+k.replace('~','~0').replace('/','~1'),v)
                for k,v in list(node[0].items())[:4] if not isinstance(v,(dict,list)) or not v)
    leaves=[(p,v) for p,v in nodes if not isinstance(v,(dict,list)) or not v]
    result=[];seen=set();cut=bounded
    for path,node in priority+leaves:
        if path in seen:continue
        seen.add(path);row={'result_pointer':path}
        if isinstance(node,str) and len(node.encode())>160:
            row.update(value_type='string',text_excerpt=node.encode()[:160].decode('utf-8',errors='ignore'),
                value_characters=len(node),value_is_complete=False);cut=True
        else:row['value']=node
        candidate=result+[row]
        if len(json.dumps(candidate,ensure_ascii=False,separators=(',',':')).encode())>max_bytes:
            cut=True;continue
        result.append(row)
    if len(result)<len(leaves):cut=True
    return result,cut
