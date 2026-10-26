"""Parsed local SPARQL with bounded worker execution and typed result pages."""
import multiprocessing as mp
import queue
from itertools import islice
from rdflib import Dataset, URIRef
from rdflib.compare import to_canonical_graph
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate
from pyparsing import ParseResults
from rdflib.plugins.sparql.parserutils import CompValue
from .contracts import RDFTerm
from .store import decode_state, encode_dataset


def walk(value):
    if isinstance(value,CompValue):
        yield value
        for v in value.values():yield from walk(v)
    elif isinstance(value,(list,tuple,ParseResults)):
        for v in value:yield from walk(v)


def validate_query(query, *, construct=True):
    parsed=parseQuery(query)
    names={node.name for node in walk(parsed)}
    if names & {'ServiceGraphPattern','DatasetClause'}:raise ValueError('only local graph queries; SERVICE/FROM are unsupported')
    if parsed[1].name not in ({'SelectQuery','AskQuery','ConstructQuery'} if construct else {'SelectQuery','AskQuery'}):raise ValueError('unsupported query form')
    return parsed


def validate_update(text):
    parsed=parseUpdate(text)
    allowed={'InsertData','DeleteData','DeleteWhere','Modify'}
    requests=parsed.get('request',[])
    if not requests or any(r.name not in allowed for r in requests):raise ValueError('only local INSERT/DELETE operations are supported')
    names={n.name for n in walk(parsed)}
    if names & {'ServiceGraphPattern','UsingClause'}:raise ValueError('remote/federated update is unsupported')
    for r in requests:
        if 'using' in r or 'withClause' in r:raise ValueError('use explicit GRAPH targets, not USING/WITH')
    return parsed


def view(dataset,graphs):
    selected=Dataset(default_union=True)
    for g in graphs:
        for t in dataset.graph(URIRef(g)):selected.graph(URIRef(g)).add(t)
    return selected


def worker(q,quads,text,operation,offset,limit,snapshot_path=None,snapshot_metadata=None):
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(1024*1024*1024,1024*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU,(10,11))
        ds=decode_state({'quads':quads})
        if operation=='update':
            ds.update(text);q.put({'quads':encode_dataset(ds)});return
        result=ds.query(text)
        if result.type=='ASK':q.put({'type':'ASK','answer':bool(result.askAnswer)});return
        if result.type=='SELECT':
            variables=[str(v) for v in result.vars]
            page=list(islice(iter(result),offset,offset+limit+1))
            q.put({'type':'SELECT','variables':variables,'rows':[
                {var:RDFTerm.from_node(row[i]).model_dump(mode='json') if row[i] is not None else None for i,var in enumerate(variables)} for row in page[:limit]],
                'offset':offset,'next_offset':offset+limit if len(page)>limit else None,'truncated':len(page)>limit});return
        if len(result.graph)>100000:raise ValueError('CONSTRUCT exceeds the 100000-triple snapshot budget')
        ordered=sorted(to_canonical_graph(result.graph),key=lambda t:tuple(v.n3() for v in t))
        if snapshot_path:
            import json
            from .store import write_json
            snapshot={**snapshot_metadata,'triples':[[RDFTerm.from_node(v).model_dump(mode='json') for v in t] for t in ordered]}
            if len(json.dumps(snapshot).encode())>64*1024*1024:raise ValueError('CONSTRUCT exceeds the 64 MiB snapshot budget')
            write_json(snapshot_path,snapshot);q.put({'snapshot_ready':True});return
        page=ordered[offset:offset+limit+1]
        q.put({'type':'CONSTRUCT','triples':[[RDFTerm.from_node(v).model_dump(mode='json') for v in t] for t in page[:limit]],
               'offset':offset,'next_offset':offset+limit if len(page)>limit else None,'truncated':len(page)>limit,
               'graph_membership':'constructed graph; input named graph membership is not retained'})
    except BaseException as exc:q.put({'error':str(exc)})


def execute(dataset,text,operation='query',offset=0,limit=100,timeout=10,snapshot_path=None,snapshot_metadata=None):
    validate_update(text) if operation=='update' else validate_query(text)
    if not 0<=offset<=100000 or not 1<=limit<=1000 or not 0<timeout<=30:raise ValueError('query resource limits out of range')
    import subprocess,sys,json,os
    payload={'quads':encode_dataset(dataset),'text':text,'operation':operation,'offset':offset,'limit':limit,
             'snapshot_path':str(snapshot_path) if snapshot_path else None,'snapshot_metadata':snapshot_metadata}
    process=subprocess.Popen([sys.executable,'-m','dataelf.ontology.graph._query_worker'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        try:out,err=process.communicate(json.dumps(payload),timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill();process.communicate();raise TimeoutError('local SPARQL time/memory budget exceeded')
        if process.returncode:raise ValueError('bounded SPARQL worker failed: '+err[-2000:])
        result=json.loads(out)
        if 'error' in result:raise ValueError(result['error'])
        return result
    finally:
        if process.poll() is None:process.kill();process.communicate()


def construct_page(dataset,text,root,identity,metadata,offset,limit,timeout,cursor=None):
    import json,re
    from pathlib import Path
    if type(offset) is not int or type(limit) is not int or not 0<=offset<=100000 or not 1<=limit<=1000 or not 0<timeout<=30:
        raise ValueError('query resource limits out of range')
    current=identity
    if cursor:
        match=re.fullmatch(r'([0-9a-f]{64}):(\d{1,6})',cursor)
        if not match:raise ValueError('invalid query cursor')
        identity,position=match.groups();offset=int(position)
        if offset>100000:raise ValueError('invalid query cursor offset')
    path=Path(root)/'.query_results'/(identity+'.json')
    if not path.exists():
        if cursor or offset:raise ValueError('CONSTRUCT snapshot missing or changed; restart at offset 0 or use its cursor')
        execute(dataset,text,timeout=timeout,snapshot_path=path,snapshot_metadata={**metadata,'snapshot_id':identity})
    snapshot=json.loads(path.read_text())
    for key in ('graph_ref','query','include_inference'):
        if snapshot[key]!=metadata[key]:raise ValueError('cursor does not belong to this graph reference/query/scope')
    if metadata.get('explicit_graphs') is not None and snapshot['read_graphs']!=metadata['explicit_graphs']:
        raise ValueError('cursor graph scope differs')
    rows=snapshot['triples'];end=min(len(rows),offset+limit);more=end<len(rows)
    return {'type':'CONSTRUCT','triples':rows[offset:end],'offset':offset,'next_offset':end if more else None,
        'next_cursor':f'{identity}:{end}' if more else None,'snapshot_id':identity,
        'snapshot_candidate_hash':snapshot['candidate_hash'],'snapshot_is_current':identity==current,
        'read_graphs':snapshot['read_graphs'],'total_triples':len(rows),'truncated':more,
        'blank_node_scope':identity,'graph_membership':'constructed graph; input named graph membership is not retained'}
