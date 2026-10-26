"""Recovery describes persisted artifacts, rather than treating prose as progress."""
import json
from pathlib import Path
from .sources import digest


def brief_trial(trial):
    if not trial:return None
    value={k:trial[k] for k in ('trial_id','scope','status','draft_hash','mapping_ids','candidate_hash',
        'content_fingerprint','deferred_checks','next_request') if k in trial}
    validation=trial.get('validation') or {}
    value['validation']={k:validation[k] for k in ('status','scope') if k in validation}
    return value


def artifact_snapshot(run_directory,service,reference):
    path=Path(run_directory)/'checkpoints/modeler/draft.json'
    draft=json.loads(path.read_text()) if path.exists() else {}
    state=service.state(reference)
    counts={k:len(draft.get(k) or {}) for k in ('classes','properties','mappings')}
    substantive=any(counts.values())
    from .graph.review_checkpoints import previous_checks
    checks=previous_checks(Path(run_directory)/'sessions',service.binding(state),
        next((json.loads(p.read_text()).get('source_fingerprint') for p in sorted((Path(run_directory)/'sessions').glob('*/context.json'))),None))
    trial_path=path.parent/'last_successful_trial.json'
    trial=json.loads(trial_path.read_text()) if trial_path.exists() else None
    latest_path=path.parent/'last_trial.json'
    latest=json.loads(latest_path.read_text()) if latest_path.exists() else None
    validation=service.validate(reference).model_dump(mode='json') if state.get('contract') else None
    valid_candidate=bool(validation and validation['status']=='valid')
    from .graph.store import content_hash
    from rdflib import Dataset
    business=Dataset()
    for s,p,o,g in service.dataset(reference).quads((None,None,None,None)):
        if str(g)!=state.get('graph_names',{}).get('schema'):business.graph(g).add((s,p,o))
    versions={**{section+':'+key:digest(value) for section in ('classes','properties') for key,value in (draft.get(section) or {}).items()},
        **{'mapping:'+m['id']:digest(m) for m in draft.get('mappings',[]) if m.get('id')}}
    result={'component_versions':versions,'draft_exists':path.exists(),'draft_has_components':substantive,'draft_components':counts,
        'draft_hash':digest(draft) if draft else None,
        'last_successful_trial':brief_trial(trial),
        'latest_trial':brief_trial(latest),
        'draft_validation':(latest['scope']+'_'+latest['status']) if latest and latest['draft_hash']==digest(draft) else 'unvalidated',
        'candidate_validation':validation['status'] if validation else 'not_materialized',
        'candidate_review':{'candidate_hash':state['review'].get('candidate_hash'),'verdict':state['review'].get('result',{}).get('verdict')} if state.get('review') else None,
        'candidate_quads':len(state.get('quads',[])),'candidate_has_contract':bool(state.get('contract')),
        'fingerprint':digest({'validated_candidate':{'rdf':content_hash(business),
                'checks':{k:state['contract'].get(k) for k in ('constraints','queries')}} if valid_candidate else None,
            'review':state.get('review'),'review_checks':checks,
            'successful_trial':trial.get('content_fingerprint') if trial else None})}
    return result


def compact_artifacts(context_path,context):
    """Handles and distinct statuses, never old prose or duplicated full contracts."""
    from .workflow_state import checkpoint_directory
    from .draft import draft_state
    directory=checkpoint_directory(context_path,context)
    draft=draft_state(context_path,context)
    def load(name):
        path=directory/name
        return json.loads(path.read_text()) if path.exists() else None
    latest=load('last_trial.json');success=load('last_successful_trial.json');complete=load('last_complete_trial.json')
    errors=[]
    from .control import control_path
    path=control_path(context_path,context)
    if path.exists():
        state=json.loads(path.read_text())
        errors=[{k:g.get(k) for k in ('code','rule','component','evidence','latest_path','trial_id','count')}
            for g in state.get('diagnostics',{}).get('groups',{}).values() if not g.get('resolved')][-3:]
    if not errors and latest and latest['status']=='invalid':
        trial=load('trials/'+latest['trial_id']+'/result.json') or {}
        errors=[{k:i[k] for k in ('path','message','evidence') if k in i} for i in trial.get('failure',{}).get('issues',[])[:3]]
    candidate={'graph_ref':context.get('graph_ref'),'validation':'not_checked_here','review':'not_checked_here'}
    if context.get('graph_ref'):
        from .graph import GraphService
        service=GraphService(context['graph_root'],context['request']['workspace']);state=service.state(context['graph_ref'])
        review=state.get('review')
        binding=service.binding(state)
        complete_valid=(complete and complete.get('candidate_hash')==binding) or (
            context.get('candidate_hash')==binding and (context.get('validation') or {}).get('status')=='valid')
        candidate.update(has_contract=bool(state.get('contract')),contract_hash=digest(state.get('contract')),review={
            'candidate_hash':review.get('candidate_hash'),'verdict':review.get('result',{}).get('verdict')} if review else None,
            validation='complete_validated' if complete_valid else 'not_confirmed_for_current_candidate')
    notes=load('workflow.json') or {}
    return {'objective':context['request'].get('objective','')[:800],
        'source_scope':[{'id':s['source_id'],'records_path':s.get('records_path','')} for s in context['request']['sources']],
        'saved_decisions':[{**n,'status':'saved_unvalidated'} for n in list(notes.get('component_notes',{}).values())[-8:]],
        'completion_conditions':['evidence-backed business coverage and justified omissions; metadata alone is insufficient',
            'complete executable contract and source coverage','deterministic validation','independent review','immutable publication'],
        'working_draft':{'hash':digest(draft),'components':{k:len(draft.get(k) or {}) for k in ('classes','properties','mappings')},'status':latest['scope']+'_'+latest['status'] if latest and latest['draft_hash']==digest(draft) else 'unvalidated',
            'read':{'tool':'ontology_draft','arguments':{'action':'read'}}},
        'latest_trial':brief_trial(latest),'last_successful_trial':brief_trial(success),'full_candidate':candidate,'unresolved_errors':errors}


def confirmed_reads(directory):
    """Record successful evidence accesses; never promote a tool query to a verdict."""
    path=Path(directory)/'events.jsonl';calls={};reads=[];result_bytes=0
    context_path=Path(directory)/'context.json'
    context=json.loads(context_path.read_text()) if context_path.exists() else None
    if not path.exists():return reads
    for line in path.read_text().splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        if not isinstance(event,dict):continue
        if event.get('type')=='message_end':
            content=event.get('message',{}).get('content',[])
            for c in content if isinstance(content,list) else []:
                if not isinstance(c,dict) or c.get('type')!='toolCall':continue
                args=c.get('arguments')
                if isinstance(args,str):
                    try:args=json.loads(args)
                    except ValueError:continue
                if isinstance(args,dict):calls[c['id']]={**c,'arguments':args}
        if event.get('type')=='tool_execution_end' and not event.get('isError'):
            call=calls.get(event.get('toolCallId'),{})
            if call.get('name') in ('ontology_evidence','graph_query','graph_explore'):
                args=call.get('arguments',{})
                entry={'tool':call['name'],'arguments':args,'saved_result':str(path),
                    'meaning':'successful read, not an approval or executed contract'}
                blocks=event.get('result',{}).get('content',[])
                try:value=json.loads(''.join(b.get('text','') for b in blocks if b.get('type')=='text'))
                except ValueError:value=None
                if isinstance(value,dict):
                    if value.get('cached_read'):
                        entry.update(value['cached_read'])
                        entry.pop('saved_result')
                        if context:
                            from .read_cache import evidence_excerpt
                            try:entry['evidence_excerpt']=evidence_excerpt(context_path,context,entry['result_id'],max_bytes=1000)
                            except ValueError:entry['excerpt_unavailable']='cache binding/integrity check failed'
                        reads.append(entry)
                        continue
                    if call['name']=='ontology_evidence' and args.get('operation')=='cached':
                        from .read_cache import request
                        entry.update(result_id=args['result_id'],next_request=request(args['result_id']))
                        entry.pop('saved_result')
                        if context:
                            from .read_cache import evidence_excerpt
                            try:entry['evidence_excerpt']=evidence_excerpt(context_path,context,entry['result_id'],max_bytes=1000)
                            except ValueError:entry['excerpt_unavailable']='cache binding/integrity check failed'
                        reads.append(entry)
                        continue
                    value={k:v for k,v in value.items() if k not in {'available_tools','workflow','draft_hash','progress_key'}}
                    if value.get('encoding')=='json' and not value.get('truncated') and value.get('offset',0)==0:
                        try:value['exact_value']=json.loads(value['text']);value.pop('text')
                        except (ValueError,KeyError):pass
                    size=len(json.dumps(value,ensure_ascii=False).encode())
                    if size<=2000 and result_bytes+size<=8000:
                        entry['saved_result_data']=value;result_bytes+=size
                reads.append(entry)
    unique={}
    for entry in reads:
        unique[entry.get('result_id') or digest({'tool':entry['tool'],'arguments':entry['arguments']})]=entry
    selected=list(unique.values())[-12:];remaining=8000
    for entry in selected:
        excerpt=entry.get('evidence_excerpt')
        if excerpt:
            size=len(json.dumps(excerpt,ensure_ascii=False).encode())
            if size>remaining:entry.pop('evidence_excerpt')
            else:remaining-=size
    return selected


def continuation_reads(previous,directory):
    """Bounded merged read excerpts; full original results remain in the cache."""
    values={v['result_id']:v for v in (previous or {}).get('confirmed_reads',[]) if v.get('result_id')}
    for v in confirmed_reads(directory):
        key=v.get('result_id')
        if key:values[key]=v
    out=[];remaining=8000
    for item in list(values.values())[-16:]:
        item=dict(item);excerpt=item.pop('evidence_excerpt',None)
        if excerpt:
            size=len(json.dumps(excerpt,ensure_ascii=False).encode())
            if size<=remaining:item['evidence_excerpt']=excerpt;remaining-=size
        out.append(item)
    return out
