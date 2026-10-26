"""Role-limited tools; native calls only, never dispatch from assistant prose."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from dataelf.ontology.graph.store import write_json
from dataelf.discovery.artifacts import resolve_workspace_path
from .contracts import ReviewDecision, check_decision
from dataelf.ontology.tool_results import control_event
from dataelf.ontology.recovery_contract import review_summary
from .submissions import freeze, verify


def schemas(role, context=None):
    def obj(properties,required=()):return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}
    text={'type':'string','minLength':1};integer={'type':'integer','minimum':0}
    result={
 'analysis_inspect':obj({'resource':{'type':'string','enum':['status','files','contract','submission','previous_review']},'offset':integer,'page_offset':integer,'max_bytes':{'type':'integer','minimum':256,'maximum':16000},'limit':{'type':'integer','minimum':1,'maximum':100}},['resource']),
        'analysis_read':obj({'path':text,'offset':integer,'limit':{'type':'integer','minimum':1,'maximum':16000}},['path']),
    }
    if role=='analyst':
        result['analysis_submit']=obj({'idempotency_key':text,'response_path':text,
            'graph_refs':{'type':'array','items':obj({'graph_id':text,'revision_id':text},['graph_id','revision_id'])}},['idempotency_key'])
    else:
        result['analysis_verify']=obj({'code':text},['code'])
        result['analysis_review_submit']=ReviewDecision.model_json_schema()
        result['analysis_graph_query']=obj({'query':text,'graph_ref':obj({'graph_id':text,'revision_id':text},['graph_id','revision_id'])},['query'])
        result['analysis_graph_explore']=obj({'seeds':{'type':'array','items':text},'depth':{'type':'integer','minimum':1,'maximum':5},'direction':{'type':'string','enum':['in','out','both']}},['seeds'])
    from dataelf.ontology.capabilities import skill_review
    if context and skill_review(context):
        from dataelf.domains.ai_index.skill_tools import schemas as source_schemas
        from dataelf.ontology.collaboration_tools import schemas as review_schemas
        result.update(source_schemas(role));result.update(review_schemas(context))
        if role=='reviewer':
            result.pop('analysis_graph_query',None);result.pop('analysis_graph_explore',None)
            if context.get('graph_refs') or (context.get('submission') or {}).get('graph_refs'):
                from dataelf.ontology.graph.tools import ADAPTERS
                result.update({n:ADAPTERS[n].json_schema() for n in ['graph_query','graph_explore']})
                refs=context.get('graph_refs') or context['submission']['graph_refs']
                for n in ['graph_query','graph_explore']:result[n]['properties']['graph_ref']['enum']=refs
        result['analysis_recovery_read']=obj({'result_id':{'type':'string','pattern':'^[0-9a-f]{64}$'},'offset':integer,'max_bytes':{'type':'integer','minimum':256,'maximum':16000}},['result_id'])
        if 'analysis_review_submit' in result:
            from dataelf.ontology.review_records import reference_schema
            result['analysis_review_submit']=reference_schema(result['analysis_review_submit'])
            issue=result['analysis_review_submit'].get('$defs',{}).get('Issue')
            if issue:issue['required']=list(dict.fromkeys([*issue.get('required',[]),'nature']))
        if context.get('review_mode')=='local':result.pop('analysis_review_submit',None)
    elif 'analysis_review_submit' in result:
        result['analysis_review_submit'].get('$defs',{}).get('Issue',{}).get('properties',{}).pop('nature',None)
    return result


DESCRIPTIONS={
 'analysis_recovery_read':'Read one confirmed tool result from this role recovery by result_id. UTF-8 JSON bytes; this is cached tool-result pagination, not source API or file pagination.',
 'analysis_inspect':'Read the exact contract, saved artifact status, immutable submission, previous review, or paged file index. previous_review uses page_offset/max_bytes for UTF-8 JSON; files use record offset/limit. No workflow order is required.',
 'analysis_read':'Read UTF-8 artifact/evidence text by Unicode character offset (not bytes). next_request reads the same saved file; this is not source API pagination.',
 'analysis_submit':'Submit the current complete report files for validation and independent review. It freezes bytes, not prose. Invalid files return precise feedback and remain editable. New content needs a new idempotency key.',
 'analysis_verify':'Independently compute in isolated Python: SNAPSHOT is read-only /input, SCRATCH writable /scratch. No host files, network, shell or subprocesses. json/csv/math/statistics/decimal/datetime/re/collections/itertools and available numpy/pandas are preloaded; unavailable lazy imports are reported. Write reusable check results under SCRATCH. stdout and saved files become audit evidence.',
 'analysis_review_submit':'Submit the exact-bound independent verdict with evidenced checks and issue updates. approve needs no blockers; only fulfilled approval can complete the task.',
 'graph_query':'Read-only SELECT/ASK/CONSTRUCT on one explicit immutable GraphRef allowed by this submission. Uses sparql and shared GraphService pagination; no graph writes or external SERVICE.',
 'graph_explore':'Read-only neighborhood, paths and provenance on one explicit approved GraphRef. No writes.',
 'analysis_graph_query':'Query only this submission\'s approved immutable GraphRefs through the shared GraphService. SELECT/ASK/CONSTRUCT are supported; no updates or external SERVICE.',
 'analysis_graph_explore':'Explore the exact submission graph, read-only, through the shared GraphService.',
}


def compute(snapshot,scratch,code):
    from dataelf.discovery.run_control import current_run, check_cancelled
    scratch=Path(scratch);scratch.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dataelf-review-jail-') as directory:
        # Outer directory stays 0700 to protect evidence from other host users;
        # the jailed root itself must be traversable by the dropped identity.
        jail=Path(directory)/'root';jail.mkdir(mode=0o755);shutil.copytree(snapshot,jail/'input')
        for path in (jail/'input').rglob('*'):path.chmod(0o555 if path.is_dir() else 0o444)
        (jail/'input').chmod(0o555);(jail/'scratch').mkdir(mode=0o777);(jail/'scratch').chmod(0o777)
        env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(Path(__file__).resolve().parents[3]),'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1'}
        control=current_run()
        process=subprocess.Popen([sys.executable,'-m','dataelf.discovery.analysis_review.sandbox'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env,
            start_new_session=bool(control))
        if control:control.register(process)
        try:out,err=process.communicate(json.dumps({'jail':str(jail),'code':code}));check_cancelled()
        finally:
            if control:control.unregister(process)
        # Never expose a failed sandbox as a successful check.
        files=[]
        for path in (jail/'scratch').rglob('*'):
            if not path.is_file() or path.is_symlink():continue
            relative=path.relative_to(jail/'scratch');target=scratch/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target);files.append(str(relative))
        result={'exit_code':process.returncode,'stdout':out,'stderr':err,'files':files,
                'isolation':'chroot + unprivileged uid + seccomp; no unrestricted fallback'}
        write_json(scratch/'execution.json',result)
        return {**result,'stdout':out[:16000],'stderr':err[:8000],
                'truncated':len(out)>16000 or len(err)>8000,'full_result':'execution.json'}


def dispatch(context_path,name,args):
    context_path=Path(context_path);context=json.loads(context_path.read_text());role=context['role']
    from dataelf.ontology.capabilities import skill_review
    modern=skill_review(context)
    if modern and name in {'analysis_graph_query','analysis_graph_explore'}:
        # Explicit historical-name adapter; hidden from new model requests.
        refs=(context.get('submission') or {}).get('graph_refs',context.get('graph_refs',[]))
        args=dict(args)
        if 'graph_ref' not in args:
            if len(refs)!=1:raise ValueError('Select one explicit graph_ref; multiple versions are available')
            args['graph_ref']=refs[0]
        if name=='analysis_graph_query':args['sparql']=args.pop('query')
        return dispatch(context_path,'graph_query' if name=='analysis_graph_query' else 'graph_explore',args)
    if modern and name.startswith('ai_index_'):
        from dataelf.domains.ai_index.skill_tools import dispatch as source_dispatch
        return source_dispatch(context_path,name,args)
    if modern and name.startswith('review_'):
        from dataelf.ontology.collaboration_tools import dispatch as review_dispatch
        return review_dispatch(context_path,name,args)
    if name not in schemas(role,context):raise ValueError('Tool is not permitted for this role')
    import jsonschema
    jsonschema.Draft202012Validator(schemas(role,context)[name]).validate(args)
    workspace=Path(context['workspace']).resolve();submission=context.get('submission')
    root=(Path(context['review_snapshot']) if context.get('review_snapshot') else workspace/'submissions'/submission['submission_id']/'files') if role=='reviewer' else workspace
    scratch_root=Path(context.get('review_scratch') or context_path.parent/'scratch')
    if name=='analysis_recovery_read':
        from dataelf.ontology.recovery_contract import normalize_recovery
        from dataelf.ontology.resources import json_page
        allowed={r.get('result_id') for r in normalize_recovery(context.get('recovered_evidence')).get('confirmed_reads',[])}
        if args['result_id'] not in allowed:raise ValueError('Result is outside this role recovery')
        value=json.loads((workspace/'logs/recovery_results'/(args['result_id']+'.json')).read_text())
        from dataelf.ontology.sources import digest
        record=next(r for r in normalize_recovery(context.get('recovered_evidence')).get('confirmed_reads',[]) if r.get('result_id')==args['result_id'])
        if record.get('result_hash') and digest(value)!=record['result_hash']:raise ValueError('Saved recovery result hash changed')
        page=json_page(value,args.get('offset',0),args.get('max_bytes',8000))
        return {**page,'pagination_unit':'utf8_json_bytes','next_request':{'tool':name,'arguments':{**args,'offset':page['next_offset']}} if page['next_offset'] is not None else None}
    if name=='analysis_inspect':
        resource=args['resource'];offset=args.get('offset',0);limit=args.get('limit',30)
        if resource=='files':
            paths=sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file())
            result={'paths':paths[offset:offset+limit],'total':len(paths)}
            if offset+limit<len(paths):result['next_request']={'tool':name,'arguments':{**args,'offset':offset+limit,'limit':limit}}
            return result
        if resource=='status':return {'role':role,'submission_id':(submission or {}).get('submission_id'),
            'saved_outputs':{s['path']:((root if modern and role=='reviewer' else workspace)/s['path']).is_file() for s in context['contract']['artifacts']},
            'task_time_limit':None,'request_limit':None,'task_token_limit':None,'previous_review':review_summary(context.get('previous_review')) if modern else context.get('previous_review')}
        value=context.get(resource) if resource!='contract' else context['contract']
        if modern and resource=='previous_review':
            from dataelf.ontology.resources import json_page
            page=json_page(value,args.get('page_offset',0),args.get('max_bytes',8000))
            return {'resource':resource,**page,'pagination_unit':'utf8_json_bytes',
                'next_request':{'tool':name,'arguments':{**args,'page_offset':page['next_offset']}} if page['next_offset'] is not None else None}
        # The manifest index can be large; callers can read its saved file.
        if resource=='submission' and value:
            result={k:v for k,v in value.items() if k!='files'}|{'file_count':len(value['files'])}
            if modern and context.get('review_mode')=='local':return {**result,'snapshot_path':str(root),'review_request_id':context['review_request_id']}
            return {**result,'manifest_path':context['manifest_path']}
        return value
    if name=='analysis_read':
        is_scratch=role=='reviewer' and args['path'].startswith('scratch/')
        read_root=scratch_root if is_scratch else root
        relative=args['path'][len('scratch/'):] if is_scratch else args['path']
        path=resolve_workspace_path(read_root,relative)
        if modern and role=='reviewer' and not path.is_file() and not is_scratch:
            from dataelf.domains.ai_index.evidence import EvidenceCatalog
            from dataelf.ontology.collaboration import ReviewLedger
            path=ReviewLedger(workspace,'analysis').dependency(context,relative)
        text=path.read_text();offset=args.get('offset',0);limit=args.get('limit',12000)
        return {'path':args['path'],'text':text[offset:offset+limit],'characters':len(text),'pagination_unit':'unicode_characters','truncated':offset+limit<len(text),
                'next_request':{'tool':name,'arguments':{**args,'offset':offset+limit,'limit':limit}} if offset+limit<len(text) else None}
    if name=='analysis_submit':
        saved=freeze(context,**args)
        write_json(context_path.parent/'analysis_submission.json',saved)
        return {'control_event':control_event('delivery_submitted',saved['submission_hash']),'submitted':True,'submission_id':saved['submission_id'],'submission_hash':saved['submission_hash'],
                'status':'validated_pending_review','meaning':'Not yet approved or completed'}
    if name=='analysis_review_submit':
        if modern:
            from dataelf.ontology.collaboration import ReviewLedger
            args=ReviewLedger(workspace,'analysis').assemble(context,args)
        decision=check_decision(args,submission,context.get('previous_review'))
        verify(workspace,submission,live=True)
        if modern:
            from dataelf.ontology.collaboration import ReviewLedger
            # Publish the decision only after strict protocol validation. The
            # coordinator, not the model, advances reviewer-owned issue state.
            ledger=ReviewLedger(workspace,'analysis')
            previous=ledger.prior()
            check_decision(args,submission,previous)
            from dataelf.domains.ai_index.evidence import EvidenceCatalog
            write_json(context_path.parent/'analysis_review_evidence.json',EvidenceCatalog(workspace).reviewer_dependencies('analysis'))
        review_path=context_path.parent/'analysis_review.json'
        if review_path.exists() and json.loads(review_path.read_text())!=decision.model_dump(mode='json'):
            raise ValueError('Formal review is immutable; request a new bound review for a different verdict')
        write_json(review_path,decision.model_dump(mode='json'))
        return {'control_event':control_event('review_submitted',decision.submission_hash),'submitted':True,'verdict':decision.verdict}
    if name=='analysis_verify':
        import uuid
        scratch=scratch_root/uuid.uuid4().hex[:12]
        if modern:
            # Frozen author files stay unchanged. Supplemental registered sources
            # and this reviewer's previous scratch are a separate read-only view.
            from dataelf.domains.ai_index.evidence import EvidenceCatalog
            with tempfile.TemporaryDirectory(prefix='compute-view-',dir=context_path.parent) as folder:
                view=Path(folder)/'input';shutil.copytree(root,view)
                catalog=EvidenceCatalog(workspace)
                for record in catalog.records():
                    for relative in [record['response_file'],str((catalog.root/record['query_id']/'record.json').relative_to(workspace))]:
                        target=view/relative
                        if target.exists():continue
                        catalog.verify([record]);target.parent.mkdir(parents=True,exist_ok=True)
                        shutil.copyfile(resolve_workspace_path(workspace,relative),target)
                if scratch_root.exists():shutil.copytree(scratch_root,view/'reviewer_scratch')
                result=compute(view,scratch,args['code'])
            result['read_only_supplements']='Registered responses at their task paths; prior scratch under SNAPSHOT/reviewer_scratch. These do not modify the author snapshot.'
        else:result=compute(root,scratch,args['code'])
        result['evidence_path']=str(scratch.relative_to(workspace))
        result['read_path']='scratch/'+str(scratch.relative_to(scratch_root)/'execution.json')
        if result['truncated']:result['next_request']={'tool':'analysis_read','arguments':{'path':result['read_path'],'offset':0}}
        write_json(scratch/'code.json',{'code':args['code']});return result
    from dataelf.ontology.graph import GraphService
    refs=submission.get('graph_refs',[])
    if not refs:raise ValueError('Submission has no graph reference')
    if 'graph_ref' not in args and len(refs)!=1:raise ValueError('Select an explicit graph_ref')
    ref=args.get('graph_ref',refs[0])
    if ref not in refs:raise ValueError('Graph reference is outside this submission')
    local=modern and context.get('review_mode')=='local'
    graph_root=Path(context['graph_root']) if local else root/Path(context['graph_root']).relative_to(workspace)
    service=GraphService(graph_root,workspace if local else root)
    if name=='graph_query':return service.query(ref,**{k:v for k,v in args.items() if k!='graph_ref'})
    if name=='graph_explore':return service.explore(ref,**{k:v for k,v in args.items() if k!='graph_ref'})
    if name=='analysis_graph_query':return service.query(ref,args['query'])
    if name=='analysis_graph_explore':return service.explore(ref,**args)
    raise ValueError('Unknown tool')


if __name__=='__main__':
    try:
        payload=json.load(sys.stdin);result=dispatch(sys.argv[1],payload['tool'],payload['arguments']);print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error_type':type(exc).__name__,'error':str(exc)},ensure_ascii=False));raise SystemExit(1)
