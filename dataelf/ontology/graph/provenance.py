"""Fact supports and premise dependencies, kept separate from business quads."""
from ..sources import digest, file_hash, select
from .contracts import Quad


def fact_id(quad):return digest(quad.model_dump(mode='json'))


def valid_facts(state,dataset=None,root=None):
    """Least fixed point of live assertions with independent valid support.

    Invalidated derivations are not silently re-certified when a premise returns.
    A new support activity explicitly revalidates them. Cycles alone are not proof.
    """
    if dataset is None:
        from .store import decode_state
        dataset=decode_state(state)
    live={fact_id(Quad.from_nodes(*q)) for q in dataset.quads((None,None,None,None))}
    activities={a['id'] for a in state['provenance']['activities']}
    files={}
    if root is not None:
        for file,expected in state['source_hashes'].items():
            p=(root/file).resolve()
            files[file]=p.is_relative_to(root) and p.is_file() and file_hash(p)==expected
    def usable(item,s):
        if s.get('valid') is False or (s.get('valid') is None and item.get('stale') and s.get('fact_ids')):return False
        if s.get('activity') not in activities:return False
        if s.get('file') and root is not None and not files.get(s['file'],False):return False
        return True
    valid=set()
    while True:
        found={fid for fid in live-valid if any(usable(state['provenance']['facts'].get(fid,{}),s) and
            (all(p in valid for p in s['fact_ids']) if s.get('fact_ids') else bool(s.get('file') or s.get('source_id')=='contract'))
            for s in state['provenance']['facts'].get(fid,{}).get('supports',[]))}
        if not found:break
        valid.update(found)
    return live,valid


def refresh_dependencies(state,dataset):
    live,valid=valid_facts(state,dataset)
    for fid,item in state['provenance']['facts'].items():
        for s in item['supports']:
            invalid=[p for p in s.get('fact_ids',[]) if p not in valid]
            if invalid:s['valid']=False;s['invalidated_by']=invalid
        item['stale']=fid in live and bool(item['supports']) and fid not in valid


def check_evidence(evidence,root,state):
    import json
    from pathlib import Path
    from ..sources import SourceCatalog
    from ..contracts import SourceInput
    hashes={}
    for e in evidence:
        if e.file:
            path=(root/e.file).resolve()
            if not path.is_relative_to(root) or not path.is_file():raise ValueError('evidence file outside workspace or absent')
            catalog=SourceCatalog(root,[SourceInput(source_id=e.source_id or e.file,path=path)])
            catalog.verify();hashes[e.file]=catalog.hashes[path.relative_to(root).as_posix()]
            if not any(e.pointer==r.pointer or (e.pointer.startswith(r.pointer+'/') and select(r.data,e.pointer[len(r.pointer):])) for r in catalog.records):
                raise ValueError('unresolved evidence pointer: '+e.file+'#'+e.pointer)
        if e.fact_ids:
            _,valid=valid_facts(state,root=root)
            if any(fid not in valid for fid in e.fact_ids):raise ValueError('premise fact is absent, stale or unsupported')
    return hashes


def support(state,quad,evidence,activity,inference=False,dataset=None):
    fid=fact_id(quad)
    item=state['provenance']['facts'].setdefault(fid,{'quad':quad.model_dump(mode='json'),'supports':[],'inference':inference,'stale':False})
    for e in evidence:
        if e.fact_ids:
            _,valid=valid_facts(state,dataset)
            if any(p not in valid for p in e.fact_ids):raise ValueError('premise fact is absent, stale or unsupported')
        raw=e.model_dump(mode='json');raw['source_id']=e.source_id or e.file or 'inference'
        raw['activity']=activity;raw['valid']=True
        if raw not in item['supports']:item['supports'].append(raw)
    return fid


def retract(state,quad,source_id=None):
    fid=fact_id(quad);item=state['provenance']['facts'].get(fid)
    if item:
        item['supports']=[s for s in item['supports'] if source_id is not None and s['source_id']!=source_id]
        if item['supports']:return False
    return True
