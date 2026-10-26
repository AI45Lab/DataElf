"""Host-owned reviewer calls for persisted AI Index local/maintenance handoffs."""
import json
from pathlib import Path

from dataelf.discovery.contracts import AgentResources
from dataelf.discovery.run_control import check_cancelled
from dataelf.ontology.collaboration import ReviewLedger
from dataelf.ontology.graph.store import write_json
from dataelf.ontology.errors import OntologyError


def prepare_input(directory, task):
    from .tools import schemas, DESCRIPTIONS
    from dataelf.domains.ai_index.role_prompts import compose, input_manifest, descriptions
    from dataelf.ontology.wire_schema import provider_schema
    tools=schemas(task['role'],task)
    if not task.get('graph_refs'):
        tools={n:s for n,s in tools.items() if not n.startswith('analysis_graph_')}
    desc=descriptions()
    registry={n:{'description':desc.get(n,DESCRIPTIONS.get(n,'')),'schema':provider_schema(s)} for n,s in tools.items()}
    write_json(directory/'tools.json',registry)
    name='analysis_reviewer' if task['role']=='reviewer' else 'analysis_author'
    system,prompt=compose(name,task,registry)
    (directory/'system.md').write_text(system)
    (directory/'prompt.md').write_text(prompt)
    write_json(directory/'composed_input.json',input_manifest(name,system,prompt,registry,task))
    return registry


def run_local_analysis(job, context, contract, config, request, reviewer=None):
    from .coordinator import _actor, evidence_excerpt
    workspace=Path(context.workspace_path);ledger=ReviewLedger(workspace,'analysis')
    root=ledger.root/'requests'/request['request_id'];attempt=len(list((root/'sessions').glob('*')))
    ledger.verify(request);ledger.stage(request['request_id'],'reviewing')
    artifacts=[];recovery={}
    while True:
        check_cancelled();attempt+=1;directory=root/'sessions'/f'{attempt:04d}_reviewer';directory.mkdir(parents=True)
        task={'workspace':str(workspace),'spec':job.spec.model_dump(mode='json'),'workflow_policy':job.spec.workflow_policy,
              'role':'reviewer','review_family':'analysis','review_mode':'local',
              'review_request_id':request['request_id'],'review_object_hash':request['object_hash'],
              'review_snapshot':request['snapshot_path'],'review_scratch':str(root/'scratch'),
              'local_question':request['question'],'contract':contract.model_dump(mode='json'),
              'graph_root':context.graph_root,'graph_refs':[context.graph_ref] if context.graph_ref else [],
              'previous_review':ledger.prior(),'collaboration':ledger.summary(),'recovered_evidence':recovery,
              'max_output_tokens':config.domain_config('ai_index').get('analysis',{}).get('reviewer_max_output_tokens'),
              'submission':{'submission_id':request['request_id'],'submission_hash':request['object_hash'],
                            'files':request['files'],'graph_refs':[context.graph_ref] if context.graph_ref else []}}
        path=directory/'context.json';write_json(path,task);tools=prepare_input(directory,task)
        actor_context=context.model_copy(update={'prompt_path':str(directory/'prompt.md'),
            'agent_resources':AgentResources(skills=context.agent_resources.skills),
            'env':{**context.env,'DATAELF_ANALYSIS_CONTEXT':str(path),'DATAELF_ANALYSIS_ROLE':'reviewer',
                'DATAELF_WORKFLOW_POLICY':job.spec.workflow_policy,'DATAELF_ANALYSIS_TOOLS':','.join(tools)}})
        actor=_actor(config,'reviewer',attempt,reviewer)
        if hasattr(actor,'log_prefix'):actor.log_prefix=f'local_review_{request["request_id"][-8:]}_{attempt:04d}'
        result=actor.run(job,actor_context);check_cancelled();artifacts.extend(result.artifacts)
        write_json(directory/'process_result.json',result.model_dump(mode='json'))
        if ledger.load(request['request_id']).get('feedback'):
            ledger.stage(request['request_id'],'author_scheduled');return artifacts
        events=workspace/'logs'/f'{getattr(actor,"log_prefix","pi")}_events.jsonl'
        from dataelf.ontology.recovery_contract import merge_recovery
        recovery=merge_recovery(recovery,evidence_excerpt(events));write_json(directory/'usage_and_recovery.json',recovery)
        if recovery.get('last_response_only_tool_markup'):
            raise RuntimeError('PI_NATIVE_TOOL_CHANNEL_INCOMPATIBLE: local reviewer returned text tags only')
        if result.status=='failed' and result.error_code not in {'PI_OUTPUT_TRUNCATED','PI_MODEL_ERROR','PI_EVENT_PARSE_ERROR'} and not (directory/'analysis_transport.json').exists():
            raise RuntimeError(result.error_message or result.error_code)


def run_graph_review(job, context, config, request, reviewer=None):
    from dataelf.ontology.contracts import OntologyRequest, Review
    from dataelf.ontology.config import OntologyConfig, load_config
    from dataelf.ontology.graph import GraphService
    from dataelf.ontology.sources import SourceCatalog
    from dataelf.ontology.runtime import PiOntologyAgent
    from dataelf.ontology.review_loop import check_review
    from dataelf.domains.ai_index.evidence import EvidenceCatalog
    workspace=Path(context.workspace_path);ledger=ReviewLedger(workspace,'graph')
    root=ledger.root/'requests'/request['request_id'];attempt=len(list((root/'sessions').glob('*')))
    ledger.verify(request);ledger.stage(request['request_id'],'reviewing')
    service=GraphService(context.graph_root,workspace) if context.graph_root else None
    ref=request.get('graph_ref');state=service.state(ref) if ref else None
    if state and service.binding(state)!=request['candidate_hash']:raise ValueError('Review candidate changed after handoff')
    from dataelf.ontology.resume import rebound_sources
    sources=rebound_sources(state,workspace) if state else EvidenceCatalog(workspace).sources()
    original=OntologyRequest(domain='ai_index',workspace=workspace,sources=sources,
        workflow_policy=job.spec.workflow_policy,objective=job.spec.objective,
        task_context={'parameters':job.spec.parameters,'constraints':job.spec.constraints,'maintenance_purpose':request['question']})
    try:base=load_config(config.ontology_for_domain('ai_index').ontology_config)
    except FileNotFoundError:base=OntologyConfig()
    policy=OntologyConfig.model_validate({**base.model_dump(mode='python'),'execution_policy':'unbounded','save_as_template':False,'templates':[]})
    actor=reviewer or PiOntologyAgent(config,runtime_env=context.env,agent_resources=context.agent_resources)
    feedback=None
    while True:
        check_cancelled();attempt+=1;directory=root/'sessions'/f'{attempt:04d}_reviewer';directory.mkdir(parents=True)
        record=state.get('contract') if state else json.loads((Path(request['snapshot_path'])/'review_object.json').read_text())
        task={'request':original.model_dump(mode='json'),'role':'reviewer','workflow_policy':job.spec.workflow_policy,
            'review_family':'graph','review_mode':request['mode'],'review_request_id':request['request_id'],
            'review_object_hash':request['object_hash'],'review_snapshot':request['snapshot_path'],
            'review_scratch':str(root/'scratch'),'local_question':request['question'],
            'source_fingerprint':SourceCatalog(workspace,original.sources,allow_empty=True).fingerprint,
            'graph_root':request.get('snapshot_graph_root') or context.graph_root,'graph_ref':ref,'candidate_hash':request['candidate_hash'],
            'contract':record,'previous_review':ledger.prior(),'collaboration':ledger.summary(),
            'feedback':feedback,
            'validation':service.validate(ref).model_dump(mode='json') if ref else None,
            'review_context':service.review_context(ref) if ref else None,
            'control_path':str(root/'control.json'),'execution_policy':'unbounded','max_llm_requests':None,'max_llm_tokens':None}
        try:result=request['feedback'] if request.get('feedback') and request['mode']=='formal' else actor('reviewer',task,directory,policy.reviewer)
        except OntologyError as exc:
            if exc.feedback['target'] not in {'control','format','arguments'} or any(i.get('recoverable') is False for i in exc.feedback.get('issues',[])):raise
            write_json(directory/'continuation.json',exc.feedback);continue
        if request['mode']=='local':
            if not ledger.load(request['request_id']).get('feedback'):raise ValueError('Local reviewer did not save feedback')
            ledger.stage(request['request_id'],'author_scheduled');return None
        if request.get('feedback') and request['mode']=='formal':decision=Review.model_validate(result)
        else:
            try:decision=check_review(Review.model_validate(result),ledger.prior(),strict=True)
            except OntologyError as exc:
                if exc.feedback['target'] not in {'format','arguments'}:raise
                feedback=exc.feedback;write_json(directory/'continuation.json',feedback);continue
            ledger.record_review(decision.model_dump(mode='json'),request_id=request['request_id'])
        if decision.verdict=='unusable':raise ValueError('Graph review blocked: '+decision.summary)
        if decision.verdict=='revise':
            ledger.stage(request['request_id'],'author_scheduled');return None
        frozen=ledger.load(request['request_id'])
        service.review(ref,request['candidate_hash'],decision.model_dump(mode='json'),
            evidence_hashes=frozen['reviewer_evidence'])
        args=request['commit_args'];published=service.commit(ref,expected_base=args['expected_base'],idempotency_key=args.get('idempotency_key'))
        if published.status!='completed':raise ValueError('Graph commit conflict or incomplete publication: '+published.model_dump_json())
        from dataelf.ontology.graph.tools import record_analysis_publication
        record_analysis_publication(workspace,published,context.graph_ref,context.graph_root)
        ledger.published(request['request_id'],published)
        ledger.stage(request['request_id'],'author_scheduled')
        return published
