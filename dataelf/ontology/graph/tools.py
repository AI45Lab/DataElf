"""Thin shared Pi bridge. All graph execution and publication lives in GraphService."""
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Literal
from pydantic import Field,TypeAdapter
from ..contracts import StrictModel,OntologyContract,SourceInput
from .contracts import GraphRef,ChangeSet
from .service import GraphService

class Workspace(StrictModel):
    action: Literal['create','open','discard']
    graph_id: str | None = None
    base: GraphRef | None = None
    workspace_id: str | None = None
    review_policy: Literal['required','deterministic'] | None = None
class Inspect(StrictModel):
    graph_ref: GraphRef
    resource: Literal['summary','contract','provenance','changes','validation','review','quads','review_context']='summary'
    offset: int=Field(default=0,ge=0,strict=True)
    limit: int=Field(default=100,ge=1,le=1000,strict=True)
class Query(StrictModel):
    graph_ref: GraphRef
    sparql: str
    cursor: str | None=None
    graphs: list[str] | None=None
    include_inference: bool=Field(default=False,strict=True)
    offset: int=Field(default=0,ge=0,le=100000,strict=True)
    limit: int=Field(default=100,ge=1,le=1000,strict=True)
    timeout: float=Field(default=10,gt=0,le=30)
class Explore(StrictModel):
    graph_ref: GraphRef
    seeds: list[str]=Field(min_length=1,max_length=20)
    graphs: list[str] | None=None
    mode: Literal['neighborhood','subgraph','path','provenance']='neighborhood'
    direction: Literal['in','out','both']='both'
    depth: int | None=Field(default=None,ge=0,le=6,strict=True)
    target: str | None=None
    predicates: list[str] | None=None
    limit: int=Field(default=200,ge=1,le=1000,strict=True)
class Materialize(StrictModel):
    graph_ref: GraphRef
    contract: OntologyContract
    sources: list[SourceInput]=Field(min_length=1)
    replay_changes: bool=Field(default=True,strict=True)
class Update(StrictModel):
    graph_ref: GraphRef
    changes: ChangeSet
class Diff(StrictModel):
    left: GraphRef
    right: GraphRef
    limit: int=Field(default=1000,ge=1,le=1000,strict=True)
class Validate(StrictModel):
    graph_ref: GraphRef
    scope: Literal['commit','candidate']='candidate'
class Commit(StrictModel):
    graph_ref: GraphRef
    expected_base: str | None=None
    idempotency_key: str | None=None

ADAPTERS={name:TypeAdapter(model) for name,model in [('graph_workspace',Workspace),('graph_inspect',Inspect),('graph_query',Query),('graph_explore',Explore),
    ('graph_materialize',Materialize),('graph_update',Update),('graph_diff',Diff),('graph_validate',Validate),('graph_commit',Commit)]}
READ_ONLY={'graph_inspect','graph_query','graph_explore','graph_diff','graph_validate'}
def tool_names(read_only=False):return sorted(READ_ONLY if read_only else ADAPTERS)


def analysis_context(workspace,graph_ref,root,run_id=None,workflow_context=None):
    directory=Path(workspace)/'prompts';directory.mkdir(parents=True,exist_ok=True)
    path=directory/'graph_context.json';schema_path=directory/'graph_tool_schemas.json'
    # Research maintenance explicitly requires independent semantic review by default.
    context={'role':'analysis','run_id':run_id,'request':{'workspace':str(Path(workspace).resolve())},'graph_ref':graph_ref,'graph_root':str(root)}
    if workflow_context:context.update(workflow_context)
    write_context(path,context)
    return path

def write_context(path,context):
    from ..sources import write_json
    write_json(path,context);from ..wire_schema import provider_schema
    write_json(Path(path).with_name('graph_tool_schemas.json'),{n:provider_schema(a.json_schema()) for n,a in ADAPTERS.items()})
    from ..capabilities import skill_review
    if skill_review(context):
        from dataelf.domains.ai_index.role_prompts import descriptions
        write_json(Path(path).with_name('tool_descriptions.json'),descriptions())


def dispatch(context_path,name,payload):
    context=json.loads(Path(context_path).read_text());role=context['role']
    if name not in ADAPTERS:raise ValueError('unknown graph tool')
    if role.endswith('reviewer') and name not in READ_ONLY:raise ValueError('reviewer has read-only graph capabilities')
    args=ADAPTERS[name].validate_python(payload).model_dump(mode='python')
    service=GraphService(context['graph_root'],context['request']['workspace'])
    from ..capabilities import skill_review
    if role=='analysis':
        from dataelf.config import DataElfConfig
        policy='required' if skill_review(context) else DataElfConfig.from_env().ontology.maintenance_review_policy
    else:policy='required'
    if role.endswith('reviewer'):
        from .contracts import GraphRef
        bound=GraphRef.model_validate(context['graph_ref']).model_dump(mode='json',exclude_none=True)
        allowed=[bound]
        parent=service.state(bound).get('base_revision')
        if parent:allowed.append({'graph_id':bound['graph_id'],'revision_id':parent})
        for field in ('graph_ref','left','right','base'):
            if args.get(field) and GraphRef.model_validate(args[field]).model_dump(mode='json',exclude_none=True) not in allowed:
                raise ValueError('Reviewer graph reference is outside the exact candidate and its declared base')
    permitted=context['graph_ref']['graph_id']
    for field in ('graph_ref','left','right','base'):
        if args.get(field) and args[field]['graph_id']!=permitted:raise ValueError('graph reference outside this task')
    if name=='graph_workspace':
        if args.get('review_policy')=='deterministic' and policy=='required':raise ValueError('agent cannot lower trusted review policy; independent review is required')
        args['review_policy']=args.get('review_policy') or policy
        if args.get('graph_id') not in (None,permitted):raise ValueError('workspace graph outside this task')
        args['graph_id']=permitted
        result=service.workspace(**args)
        if skill_review(context) and hasattr(result,'workspace_id'):
            state=service.state(result);state.setdefault('metadata',{})['workflow_policy']=context['workflow_policy']
            service.store.save(result,state)
    elif name not in READ_ONLY and policy=='required':
        service.require_review(args['graph_ref'])
        return dispatch_mutation(context_path,context,name,args,service)
    else:return dispatch_mutation(context_path,context,name,args,service)
    return result.model_dump(mode='json') if hasattr(result,'model_dump') else result


def dispatch_mutation(context_path,context,name,args,service):
    # Cache at read time, with both before/after bindings. Never attach a later
    # candidate hash to an earlier query result during recovery.
    if name in ('graph_query','graph_explore') and context.get('source_fingerprint'):
        from ..read_cache import save_read
        original={**args};ref=service.ref(args['graph_ref']).model_dump(mode='json',exclude_none=True)
        before=service.binding(service.state(ref))
        result=_dispatch_mutation(context_path,context,name,args,service)
        if before!=service.binding(service.state(ref)):
            return result
        return save_read(context_path,context,name,original,result,
            [{'graph_ref':ref,'candidate_hash':before}])
    return _dispatch_mutation(context_path,context,name,args,service)


def _dispatch_mutation(context_path,context,name,args,service):
    role=context['role']
    from ..capabilities import skill_review
    if skill_review(context) and role.endswith('reviewer') and context.get('candidate_hash'):
        ref=context.get('graph_ref')
        if ref and service.binding(service.state(ref))!=context['candidate_hash']:
            raise ValueError('Review is bound to a candidate that has changed')
    if name=='graph_inspect':result=service.inspect(args.pop('graph_ref'),**args)
    elif name=='graph_query':result=service.query(args.pop('graph_ref'),**args)
    elif name=='graph_explore':result=service.explore(args.pop('graph_ref'),**args)
    elif name=='graph_materialize':
        if role in ('modeler','stage1_generator'):
            from ..task_sources import effective_request
            expected=[s.model_dump(mode='json') for s in effective_request(context,args['contract']).sources]
            supplied=[SourceInput.model_validate(s).model_dump(mode='json') for s in args['sources']]
            if supplied!=expected:raise ValueError('modeling materialization must use the supplied frozen source selection')
        result=service.materialize(args.pop('graph_ref'),**args,agent_id=role,run_id=context.get('run_id'))
    elif name=='graph_update':
        args['changes'].update(agent_id=role,run_id=context.get('run_id'))
        extra={}
        if skill_review(context):
            from dataelf.domains.ai_index.evidence import EvidenceCatalog
            from ..sources import file_hash
            catalog=EvidenceCatalog(service.evidence_root);records=catalog.records();catalog.verify(records)
            registered={r['response_file']:r for r in records}
            current=service.state(args['graph_ref'])
            hashes={}
            for evidence in args['changes']['evidence']:
                relative=evidence.get('file')
                if not relative:continue
                if relative not in registered and relative not in current['source_hashes']:
                    raise ValueError('Register new maintenance evidence before using it')
                if relative in registered:
                    record=catalog.root/registered[relative]['query_id']/'record.json'
                    hashes[str(record.relative_to(service.evidence_root))]=file_hash(record)
            extra['additional_source_hashes']=hashes
        result=service.update(args['graph_ref'],args['changes'],**extra)
    elif name=='graph_diff':result=service.diff(**args)
    elif name=='graph_validate':result=service.validate(args['graph_ref'],args['scope'])
    else:
        result=service.commit(args['graph_ref'],expected_base=args['expected_base'],idempotency_key=args['idempotency_key'])
        if result.status=='pending_review' and role in ('modeler','stage1_generator'):
            if args['graph_ref']!=context['graph_ref']:raise ValueError('submit the active modeling candidate supplied for this run')
            from ..tools import submit_contract
            submitted=submit_contract(Path(context_path),context,service.state(args['graph_ref'])['contract'])
            return {**result.model_dump(mode='json'),**submitted}
        if result.status=='pending_review' and role=='analysis':
            if skill_review(context):
                from ..collaboration import ReviewLedger
                from ..sources import write_json,digest
                ledger=ReviewLedger(service.evidence_root,'graph')
                state=service.state(args['graph_ref'])
                original=context['original_spec']
                review_context={'role':'analyst','workspace':str(service.evidence_root),'graph_ref':context['graph_ref'],
                    'graph_root':str(service.root),'workflow_policy':context['workflow_policy']}
                changes=state.get('changes',[])
                purpose=(changes[-1].get('change',{}).get('reason','') if changes else '')
                request=ledger.request(review_context,object_kind='candidate',
                    question=original['objective']+'\n本次图谱维护目的：'+purpose,evidence=[],graph_ref=args['graph_ref'],
                    mode='formal',commit_args={k:args[k] for k in ['expected_base','idempotency_key']},
                    idempotency_key='graph-commit:'+digest({'key':args['idempotency_key'],'candidate':result.candidate_hash,'responses':ledger.responses()}))
                if request.get('feedback') and request['feedback'].get('verdict')=='revise':
                    return {**result.model_dump(mode='json'),'status':'rejected','review_feedback':request['feedback'],
                        'request_id':request['request_id'],'message':'Revise this candidate or save new counterevidence before requesting re-review.'}
                marker={'kind':'graph_review','family':'graph','request_id':request['request_id']}
                write_json(Path(context['analysis_context_path']).with_name('handoff.json'),marker)
                from ..tool_results import control_event
                return {**result.model_dump(mode='json'),'control_event':control_event('graph_review',request['request_id']),'handoff':marker}
            from ..runtime import PiOntologyAgent
            from ..config import load_config
            from ..contracts import OntologyRequest
            from ..sources import SourceCatalog
            state=service.state(args['graph_ref'])
            inputs=list(state['source_inputs'])
            represented={(service.evidence_root/Path(s['path'])).resolve() for s in inputs}
            # Maintenance can add frozen evidence independently of materializing
            # the original contract. The reviewer must be able to replay it.
            from ..sources import digest
            for file in state['source_hashes']:
                if (service.evidence_root/file).resolve() not in represented:
                    inputs.append({'source_id':'evidence_'+digest(file)[:16],'path':file})
            if not inputs:raise ValueError('semantic reviewer requires evidence snapshots; imported limited provenance must use an explicit deterministic policy')
            request=OntologyRequest(domain=(state.get('contract') or {}).get('domain','imported'),workspace=service.evidence_root,sources=inputs,
                objective='Review this candidate publication relative to its immutable parent '+str(state['base_revision'])+'. Verify the changes, their frozen evidence, provenance and all affected constraints. The published parent and its prior review are available through the same read-only graph service; historical change records describe earlier versions. Reject unsupported or evidence-contradicting current assertions.')
            folder=service.store.path(service.ref(args['graph_ref']))/'reviews'/uuid.uuid4().hex;folder.mkdir(parents=True)
            reviewer_context={'request':request.model_dump(mode='json'),'source_fingerprint':SourceCatalog(request.workspace,request.sources).fingerprint,
                'graph_root':str(service.root),'graph_ref':args['graph_ref'],'contract':state.get('contract'),
                'candidate_hash':result.candidate_hash,'validation':result.validation.model_dump(mode='json'),'feedback':[],
                'review_context':service.review_context(args['graph_ref']),
                'control_path':str(folder.parent/'control.json'),'max_llm_requests':160,'max_llm_tokens':None}
            from dataelf.config import DataElfConfig
            runtime_config=DataElfConfig.from_env()
            from ..config import RoleConfig
            saved=state.get('metadata',{}).get('reviewer_config')
            reviewer_config=RoleConfig.model_validate(saved) if saved else load_config(runtime_config.ontology.ontology_config).reviewer
            try:review=PiOntologyAgent(runtime_config)('reviewer',reviewer_context,folder,reviewer_config)
            except (ValueError,TimeoutError,subprocess.TimeoutExpired) as exc:
                from ..errors import OntologyError
                if isinstance(exc,OntologyError) and exc.feedback['target'] not in {'control','format','arguments'}:raise
                if not isinstance(exc,(OntologyError,TimeoutError,subprocess.TimeoutExpired)):raise
                diagnostics=exc.feedback if isinstance(exc,OntologyError) else {
                    'code':'ONTOLOGY_REVIEW_TIMEOUT','target':'control','issues':[{'message':'Independent review timed out; completed checks and candidate retained','recoverable':True}]}
                return result.model_copy(update={'graph_ref':service.ref(args['graph_ref']),
                    'review_diagnostics':{**diagnostics,'checkpoint_path':str(folder),'candidate_hash':result.candidate_hash}}).model_dump(mode='json')
            service.review(args['graph_ref'],result.candidate_hash,review)
            result=service.commit(args['graph_ref'],expected_base=args['expected_base'],idempotency_key=args['idempotency_key'])
        if result.status=='completed' and role=='analysis':
            record_analysis_publication(service.evidence_root,result,context['graph_ref'],service.root)
    return result.model_dump(mode='json') if hasattr(result,'model_dump') else result


def record_analysis_publication(workspace,result,previous,root):
    from ..sources import write_json
    workspace=Path(workspace);published=result.graph_ref.model_dump(mode='json')
    versions=workspace/'artifacts/analysis_graph_versions.json'
    used=json.loads(versions.read_text()).get('graph_refs',[]) if versions.exists() else [previous]
    if published not in used:used.append(published)
    write_json(versions,{'graph_refs':used})
    write_json(workspace/'artifacts/analysis_graph_ref.json',{'graph_ref':published,'graph_root':str(root),'rdf_path':str(Path(result.manifest_path).parent/'graph.nq')})
    inventory=workspace/'artifacts/server_inputs.json'
    if inventory.exists():
        value=json.loads(inventory.read_text());value.update(graph_ref=published,graph_root=str(root),rdf_path=str((Path(result.manifest_path).parent/'graph.nq').relative_to(workspace)))
        write_json(inventory,value)

if __name__=='__main__':
    try:print(json.dumps(dispatch(Path(sys.argv[1]),sys.argv[2],json.load(sys.stdin)),ensure_ascii=False,default=str))
    except Exception as exc:
        from pydantic import ValidationError
        if isinstance(exc,ValidationError):
            detail={'code':'GRAPH_ARGUMENT_INVALID','target':'arguments',
                    'issues':exc.errors(include_input=False,include_context=False,include_url=False)}
        else:detail={'code':'GRAPH_OPERATION_FAILED','target':'operation','message':str(exc)}
        print(json.dumps(detail),file=sys.stderr);sys.exit(1)
