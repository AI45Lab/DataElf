"""One autonomous modeler, one independent reviewer, one shared commit boundary."""
from __future__ import annotations
import json
import subprocess
import time
import uuid
from pathlib import Path
from dataelf.discovery.run_control import check_cancelled,RunCancelled
from .config import OntologyConfig,load_config
from .contracts import OntologyContract,OntologyRequest,OntologyRunResult,Review,ExtractionPlan
from .sources import SourceCatalog,digest,file_hash,write_json
from .templates import bind_template,export_template,load_templates,require_domain,TemplateDomainError
from .graph import GraphService
from .errors import OntologyError,error_feedback
from .collaboration import ReviewHandoff


def _recovered_result(committed,run_id):
    bundle=Path(committed.manifest_path).parent
    manifest=json.loads((bundle/'manifest.json').read_text());metrics=manifest.get('metrics',{})
    return OntologyRunResult(status='completed',stage='rdf',graph_id=run_id,revision_id=committed.revision_id,
        bundle_path=str(bundle),contract_path=str(bundle/'contract.json'),
        graph_ref=committed.graph_ref.model_dump(mode='json'),stage1_run_id=run_id,stage1_bundle=str(bundle),stage2_bundle=str(bundle),
        nquads_path=str(bundle/'graph.nq'),ntriples_path=str(bundle/'graph.nt'),rdfxml_path=str(bundle/'graph.rdf'),
        manifest_path=str(bundle/'manifest.json'),validation_path=str(bundle/'validation.json'),
        details={'stage1Mode':'dynamic','modelCalls':metrics.get('modelCalls',0),'metrics':metrics,
            'task_outcome':'completed','artifact_status':'published','published':True,
            'legacyBundleMeaning':'both aliases point to one immutable graph revision'})


def run_ontology(request:OntologyRequest,*,config:OntologyConfig|None=None,agent=None,contract=None,progress=None):
    config=config or load_config(request.config);workspace=request.workspace.resolve()
    from .capabilities import skill_review
    if skill_review(request):
        if request.template or contract is not None:raise ValueError('Skill workflow uses dynamic modeling, not templates')
        config=OntologyConfig.model_validate({**config.model_dump(mode='python'),'execution_policy':'unbounded','mode':'dynamic','templates':[],
            'save_as_template':False,'modeler':{**config.modeler.model_dump(mode='python'),'author_interface':'split','tool_choice':'auto'}})
    request=request.model_copy(update={'workspace':workspace,'documents':[(workspace/p).resolve() for p in request.documents],
        'template':(workspace/request.template).resolve() if request.template else None})
    if request.template:config=config.model_copy(update={'mode':'template'})
    if contract is not None and config.mode!='template':raise ValueError('a fixed contract requires template mode')
    root=(workspace/config.output_subdir).resolve()
    if not root.is_relative_to(workspace):raise ValueError('ontology output must stay inside workspace')
    active_path=root/'skill_task.json';task_key=digest({'domain':request.domain,'objective':request.objective,'context':request.task_context})
    active=json.loads(active_path.read_text()) if skill_review(request) and active_path.exists() else None
    if active and active['task_key']!=task_key:raise ValueError('Workspace already belongs to a different ontology task')
    if active and active.get('status')=='cancelled':raise RunCancelled('Saved task was cancelled; it is not automatically resumed')
    run_id=active['run_id'] if active else uuid.uuid4().hex
    draft=root/'runs'/run_id;draft.mkdir(parents=True,exist_ok=bool(active))
    service=GraphService(root/'graphs',workspace)
    candidate=service.ref(active['candidate']) if active else service.workspace('create',graph_id=run_id,review_policy='required' if config.mode=='dynamic' else 'deterministic')
    if skill_review(request):
        write_json(active_path,{'task_key':task_key,'run_id':run_id,'candidate':candidate.model_dump(mode='json'),'status':'running'})
    if skill_review(request):
        state=service.state(candidate);state.setdefault('metadata',{})['workflow_policy']=request.workflow_policy
        service.store.save(candidate,state)
    unbounded=config.execution_policy=='unbounded'
    stage='modeling';deadline=None if unbounded else time.monotonic()+config.timeout_seconds;calls=0;repairs=0;format_repairs=0;restarts=0;feedback=[];modeled=False
    recovery_state=None;unchanged_recoveries=0;session_snapshot=None;last_directory=None;last_role=None;last_session_deadline=None
    closing_deadlines={};closing_resumed=set();first_save_resumed=set();review_history=[];previous_review=None
    host_state=draft/'host_state.json'
    if active and host_state.exists():
        saved=json.loads(host_state.read_text());modeled=saved['modeled']
        contract=OntologyContract.model_validate(saved['contract']) if saved.get('contract') else None
        feedback=saved.get('feedback',[]);review_history=saved.get('review_history',[]);previous_review=saved.get('previous_review')
    if active:
        calls=max([int(p.name.split('_')[0]) for p in (draft/'sessions').glob('*')]+[0])
        sessions=sorted((draft/'sessions').glob('*/context.json'))
        if sessions:
            from .control import record_interruption
            record_interruption(sessions[-1],json.loads(sessions[-1].read_text()))
        if service.store.head(run_id):
            recovered=service.commit(candidate,expected_base=None,idempotency_key=run_id)
            if recovered.status!='completed':raise ValueError('Saved publication could not be safely recovered')
            if service.validate(recovered.graph_ref).status!='valid':raise ValueError('Saved publication evidence failed integrity checks')
            result=_recovered_result(recovered,run_id)
            write_json(draft/'result.json',result.model_dump(mode='json'))
            write_json(active_path,{'task_key':task_key,'run_id':run_id,'candidate':candidate.model_dump(mode='json'),'status':'completed'})
            return result
    def persist():
        if skill_review(request):write_json(host_state,{'modeled':modeled,'contract':contract.model_dump(mode='json') if contract else None,
            'feedback':feedback,'review_history':review_history,'previous_review':previous_review})
    progress=progress or (lambda stage:None)
    def check():
        check_cancelled()
        if deadline is not None and time.monotonic()>=deadline:raise OntologyError('ONTOLOGY_GLOBAL_BUDGET_EXHAUSTED','control',[
            {'message':'Ontology global deadline exhausted','recoverable':False,'reason':'global_deadline'}])
    try:
        paths=[request.template] if request.template else config.templates
        templates={str(p):file_hash(p) for p in paths}
        if config.mode=='template':
            if contract is None:
                template=load_templates(paths,domain=request.domain);contract=template.contract
                request=request.model_copy(update={'sources':bind_template(template,request.sources)})
            else:
                for p in paths:require_domain(json.loads(p.read_text()).get('domain'),request.domain)
            require_domain(contract.domain,request.domain)
        catalog=SourceCatalog(workspace,request.sources,allow_empty=skill_review(request))
        documents={}
        for p in request.documents:
            if not p.is_relative_to(workspace):raise ValueError('documentation must be inside workspace')
            documents[str(p)]=file_hash(p)
        write_json(draft/'request.json',request.model_dump(mode='json'));write_json(draft/'profiles.json',catalog.profile())
        if config.mode=='dynamic' and agent is None:
            from .runtime import PiOntologyAgent
            agent=PiOntologyAgent()
        def verify():
            check();catalog.verify()
            for p,hash in {**templates,**documents}.items():
                if file_hash(Path(p))!=hash:raise ValueError('template or documentation changed')
        def invoke(role,**extra):
            nonlocal calls,session_snapshot,last_directory,unchanged_recoveries,last_role,last_session_deadline
            from .recovery import artifact_snapshot
            session_snapshot=artifact_snapshot(draft,service,candidate)
            verify()
            from .delivery import allocate_session
            from .control import metrics
            role_config,window=allocate_session(config,role,deadline,metrics(draft/'control.json'),closing_deadlines.get(role))
            calls+=1
            from .read_cache import context_feedback
            context={'request':request.model_dump(mode='json'),'source_fingerprint':catalog.fingerprint,'feedback':context_feedback(feedback[-8:] if unbounded else feedback),
                'control_path':str(draft/'control.json'),'checkpoint_directory':str(draft/'checkpoints/modeler'),
                'read_cache_directory':str(draft/'read_cache'),
                'deadline_monotonic':deadline,'execution_policy':config.execution_policy,**window,
                'graph_root':str(service.root),'graph_ref':candidate.model_dump(mode='json'),'run_id':run_id,
                'max_llm_requests':config.max_llm_requests,'max_llm_tokens':config.max_llm_tokens,
                'previous_contract':contract.model_dump(mode='json') if contract else None,
                'previous_draft':json.loads((draft/'checkpoints/modeler/draft.json').read_text()) if (draft/'checkpoints/modeler/draft.json').exists() else None,**extra}
            context['recovery_state']=recovery_state
            if skill_review(request):
                from .collaboration import ReviewLedger
                context.update(workflow_policy=request.workflow_policy,review_family='graph',review_mode='formal',
                    collaboration=ReviewLedger(workspace,'graph').summary())
                context.update(extra)
                context['previous_review']=ReviewLedger(workspace,'graph').prior()
            if role=='reviewer' and previous_review and not skill_review(request):context['previous_review']=previous_review
            directory=draft/'sessions'/f'{calls:03d}_{role}';directory.mkdir(parents=True)
            last_directory=directory
            last_role=role
            last_session_deadline=context['session_deadline_monotonic']
            try:result=agent(role,context,directory,role_config)
            except (TimeoutError,subprocess.TimeoutExpired) as exc:
                if deadline is not None and time.monotonic()>=deadline:
                    raise OntologyError('ONTOLOGY_GLOBAL_BUDGET_EXHAUSTED','control',[
                        {'message':'Ontology global deadline exhausted during the session','recoverable':False,'reason':'global_deadline'}]) from exc
                raise OntologyError('ONTOLOGY_TIMEOUT','control',[{
                    'message':'Ontology session timed out; checkpoints and trial artifacts retained',
                    'recoverable':True,'reason':'session_timeout','role':role,
                    'timeout_seconds':role_config.timeout_seconds,'session_directory':str(directory)}]) from exc
            finally:
                if role=='modeler' and (directory/'draft.json').exists():
                    write_json(draft/'checkpoints/modeler/draft.json',json.loads((directory/'draft.json').read_text()))
            verify();write_json(directory/'result.json',result)
            unchanged_recoveries=0
            return result
        while True:
            verify();progress(stage)
            try:
                if skill_review(request):
                    from .collaboration import ReviewLedger
                    ledger=ReviewLedger(workspace,'graph')
                    pending_local=[r for r in ledger.requests() if r['mode']=='local' and r['status']!='delivered']
                    if pending_local:
                        local=pending_local[0];ledger.verify(local)
                        if local['status'] not in {'feedback_saved','author_scheduled'}:
                            ledger.stage(local['request_id'],'reviewing')
                            invoke('reviewer',review_mode='local',review_request_id=local['request_id'],
                                review_object_hash=local['object_hash'],review_snapshot=local['snapshot_path'],
                                local_question=local['question'],graph_ref=local.get('graph_ref'),
                                graph_root=local.get('snapshot_graph_root') or str(service.root),
                                candidate_hash=local.get('candidate_hash'))
                            local=ledger.load(local['request_id'])
                            if not local.get('feedback'):raise ValueError('Local reviewer returned without persisted feedback')
                        entry={'code':'ONTOLOGY_LOCAL_FEEDBACK','target':'contract','request_id':local['request_id'],
                               'issues':local['feedback'].get('issues',[]),'answer':local['feedback']['answer']}
                        if not any(f.get('request_id')==local['request_id'] for f in feedback):feedback.append(entry)
                        write_json(draft/'feedback.json',feedback)
                        persist()
                        ledger.stage(local['request_id'],'delivered')
                if config.mode=='dynamic' and not modeled:
                    stage='modeling';contract=OntologyContract.model_validate(invoke('modeler'));modeled=True
                    if unbounded:write_json(draft/'checkpoints/modeler/draft.json',contract.model_dump(mode='json'))
                    persist()
                if skill_review(request):
                    from .task_sources import effective_request
                    request=effective_request(request,contract)
                    catalog=SourceCatalog(workspace,request.sources)
                require_domain(contract.domain,request.domain)
                if config.mode=='dynamic':
                    from .tools import verify_evidence
                    for mapping in contract.mappings:verify_evidence(mapping,catalog)
                state=service.state(candidate)
                if state.get('contract')!=contract.model_dump(mode='json'):
                    service.materialize(candidate,contract,request.sources,run_id=run_id,agent_id='modeler' if config.mode=='dynamic' else 'deterministic_template')
                validation=service.validate(candidate)
                if validation.status!='valid':raise OntologyError('ONTOLOGY_GRAPH_INVALID','contract',validation.issues)
                if config.mode=='dynamic':
                    state=service.state(candidate);state['review']=None;service.store.save(candidate,state)
                pending=service.commit(candidate,expected_base=None) if config.mode=='dynamic' else None
                if config.mode=='dynamic':
                    stage='review';progress(stage)
                    graph_path=draft/'candidate.nq';service.dataset(candidate).serialize(destination=graph_path,format='nquads')
                    review=Review.model_validate(invoke('reviewer',contract=contract.model_dump(mode='json'),graph_path=str(graph_path),
                        candidate_hash=pending.candidate_hash,validation=validation.model_dump(mode='json'),lineage=service.state(candidate).get('projection_lineage'),
                        review_context=service.review_context(candidate)))
                    if unbounded:
                        from .review_loop import check_review
                        row={'round':len(review_history)+1,'candidate_hash':pending.candidate_hash,'source_fingerprint':catalog.fingerprint,'result':review.model_dump(mode='json')}
                        review_history.append(row)
                        if skill_review(request):previous_review=ledger.prior()
                        try:review=check_review(review,previous_review,strict=skill_review(request))
                        except OntologyError as error:
                            row['feedback_error']=error.feedback;write_json(draft/'review_history.json',review_history);raise
                        row['result']=review.model_dump(mode='json');write_json(draft/'review_history.json',review_history)
                        previous_review=review.model_dump(mode='json')
                        if skill_review(request):ledger.record_review(previous_review)
                    if review.verdict=='unusable':raise RuntimeError('reviewer rejected unusable candidate: '+review.summary)
                    if review.verdict=='revise':
                        if not review.issues:raise RuntimeError('review revision requires actionable issues')
                        error=OntologyError('ONTOLOGY_REVIEW_REVISE','contract',[i.model_dump(mode='json') for i in review.issues])
                        if unbounded:error.feedback.update(candidate_hash=pending.candidate_hash,review_round=len(review_history),issue_updates=[u.model_dump(mode='json') for u in review.issue_updates],review_summary=review.summary)
                        raise error
                    if skill_review(request):
                        from dataelf.domains.ai_index.evidence import EvidenceCatalog
                        service.review(candidate,pending.candidate_hash,review.model_dump(mode='json'),
                            evidence_hashes=EvidenceCatalog(workspace).reviewer_dependencies('graph'))
                    else:service.review(candidate,pending.candidate_hash,review.model_dump(mode='json'))
                if config.save_as_template:
                    template=export_template(contract,ExtractionPlan(contract_hash=digest(contract.model_dump(mode='json'))),request.sources)
                    from .engine import materialize
                    replay,_,v=materialize(template.contract,SourceCatalog(workspace,bind_template(template,request.sources)))
                    from .graph.store import content_hash
                    if v['status']!='valid' or content_hash(replay)!=content_hash(service.dataset(candidate)):
                        raise OntologyError('ONTOLOGY_TEMPLATE_NOT_REPLAYABLE','contract',[{'message':'Supplemental candidate changes cannot be reproduced by the exported contract; incorporate reusable corrections into the contract.'}])
                    state=service.state(candidate);state['template']=template.model_dump(mode='json');service.store.save(candidate,state)
                verify()
                from .control import metrics
                state=service.state(candidate);lineage=state.get('projection_lineage',{})
                run_metrics={**metrics(draft/'control.json'),'agentSessions':calls,'modelCalls':calls,'repairs':repairs,
                    'formatRepairs':format_repairs,'controlRestarts':restarts,'reviewRounds':len(review_history),'recordCount':len(lineage.get('records',[])),'quadCount':len(state['quads'])}
                pi_config=getattr(getattr(getattr(agent,'config',None),'explorer',None),'pi',None)
                reviewer_config=config.reviewer.model_copy(update={
                    'model':config.reviewer.model or (pi_config.model if pi_config else None)})
                from .config import RoleConfig
                maintenance_reviewer=RoleConfig(model=reviewer_config.model,max_output_tokens=reviewer_config.max_output_tokens,stream_usage=reviewer_config.stream_usage) if unbounded else reviewer_config
                state['metadata']={'domain':request.domain,'mode':config.mode,'templates':templates,'documents':documents,
                    'workflow_policy':request.workflow_policy,
                    'sourceFingerprint':catalog.fingerprint,'templateSaved':config.save_as_template,'metrics':run_metrics,
                    'reviewer_config':maintenance_reviewer.model_dump(mode='json'),'run_reviewer_config':reviewer_config.model_dump(mode='json'),'execution_policy':config.execution_policy}
                service.store.save(candidate,state)
                committed=service.commit(candidate,expected_base=None,idempotency_key=run_id)
                if committed.status!='completed':raise RuntimeError('commit did not publish: '+committed.model_dump_json())
                bundle=Path(committed.manifest_path).parent
                if skill_review(request):
                    result=_recovered_result(committed,run_id)
                    write_json(draft/'result.json',result.model_dump(mode='json'))
                    write_json(active_path,{'task_key':task_key,'run_id':run_id,'candidate':candidate.model_dump(mode='json'),'status':'completed'})
                    return result
                return OntologyRunResult(status='completed',stage='rdf',graph_id=run_id,revision_id=committed.revision_id,
                    bundle_path=str(bundle),contract_path=str(bundle/'contract.json'),graph_ref=committed.graph_ref.model_dump(mode='json'),
                    stage1_run_id=run_id,stage2_run_id=None,stage1_bundle=str(bundle),stage2_bundle=str(bundle),
                    nquads_path=str(bundle/'graph.nq'),ntriples_path=str(bundle/'graph.nt'),rdfxml_path=str(bundle/'graph.rdf'),
                    manifest_path=str(bundle/'manifest.json'),validation_path=str(bundle/'validation.json'),
                    template_path=str(bundle/'template.json') if config.save_as_template else None,
                    details={'stage1Mode':config.mode,'modelCalls':calls,'metrics':run_metrics,
                        'task_outcome':'completed','artifact_status':'published','published':True,
                        'legacyBundleMeaning':'both aliases point to one immutable graph revision'})
            except ReviewHandoff:
                if not skill_review(request):raise
                persist()
                continue
            except (ValueError,TypeError) as exc:
                session_timeout=isinstance(exc,OntologyError) and exc.feedback['code']=='ONTOLOGY_TIMEOUT'
                issue=error_feedback(exc,'contract');feedback.append(issue);write_json(draft/'feedback.json',feedback)
                if config.mode=='template':raise
                if issue.get('code')=='ONTOLOGY_REVIEW_BLOCKED' or (unbounded and issue.get('code') in {'ONTOLOGY_TOOL_TRANSPORT_INVALID','ONTOLOGY_TOOL_CHOICE_UNSUPPORTED'}):raise
                if issue['target']=='control':
                    if not any(i.get('recoverable') for i in issue.get('issues',[])) or (not unbounded and config.max_control_restarts is not None and restarts>=config.max_control_restarts):raise exc
                    if not unbounded and last_role in first_save_resumed:
                        raise OntologyError(issue['code'],'control',[{**i,'recoverable':False,
                            'reason':'first_save_attempt_exhausted','message':i.get('message','')+'; bounded first-save recovery ended without submission'} for i in issue.get('issues',[])]) from exc
                    if not unbounded and last_role in closing_resumed:raise exc
                    from .recovery import artifact_snapshot,confirmed_reads
                    current=artifact_snapshot(draft,service,candidate)
                    changed=bool(session_snapshot and current['fingerprint']!=session_snapshot['fingerprint'])
                    unchanged_recoveries=0 if changed else unchanged_recoveries+1
                    from .recovery import continuation_reads
                    inherited_reads=continuation_reads(recovery_state,last_directory) if unbounded else confirmed_reads(last_directory)
                    recovery_state={'schema_version':'dataelf.recovery.v1',**current,'artifact_changed_in_last_session':changed,
                        'consecutive_unchanged_sessions':unchanged_recoveries,'confirmed_reads':inherited_reads,
                        'saved_components_changed':current.get('component_versions')!=(session_snapshot or {}).get('component_versions'),
                        'last_stop_reason':issue.get('code')}
                    if not unbounded and issue.get('code')=='ONTOLOGY_CLOSING_INTERRUPT':
                        closing_deadlines[last_role]=last_session_deadline
                        closing_resumed.add(last_role)
                        recovery_state['closing_recovery']=True
                    if (not unbounded and last_role=='modeler' and not current['draft_has_components'] and
                            config.delivery.enabled and issue.get('code')!='ONTOLOGY_CLOSING_INTERRUPT' and not session_timeout):
                        first_save_resumed.add(last_role)
                        closing_deadlines[last_role]=min(deadline,last_session_deadline,time.monotonic()+config.delivery.first_save_recovery_seconds)
                        recovery_state['first_save_attempt']=True
                    from .control import budget_status
                    previous_context=json.loads((last_directory/'context.json').read_text()) if (last_directory/'context.json').exists() else {}
                    if previous_context:
                        from .recovery import compact_artifacts
                        recovery_state['artifacts']=compact_artifacts(last_directory/'context.json',previous_context)
                        recovery_state['budget']=budget_status(last_directory/'context.json',previous_context)
                    write_json(draft/'recovery.json',recovery_state)
                    if session_timeout and not unbounded:
                        from .control import metrics
                        usage=metrics(draft/'control.json')
                        limits=config.modeler if last_role=='modeler' else config.reviewer
                        role_usage=usage.get('roleMetrics',{}).get(last_role,{})
                        remaining=(time.monotonic()<deadline and usage['llmRequests']<config.max_llm_requests
                            and role_usage.get('requests',0)<limits.max_requests
                            and (config.max_llm_tokens is None or (not usage['usageUnknownRequests'] and usage['knownTokens']<config.max_llm_tokens)))
                        if not changed or not remaining:
                            issue['issues'][0].update(recoverable=False,
                                reason='session_timeout_no_artifact_progress' if not changed else 'session_timeout_budget_exhausted',
                                checkpoint_state=recovery_state)
                            write_json(draft/'feedback.json',feedback)
                            raise OntologyError(issue['code'],'control',issue['issues']) from exc
                    if not unbounded and unchanged_recoveries>=2 and issue.get('code')!='ONTOLOGY_CLOSING_INTERRUPT':
                        raise OntologyError(issue['code'],'control',[{**i,'recoverable':False,
                            'message':i.get('message','')+'; two interrupted sessions produced no artifact changes',
                            'reason':'recovery_no_artifact_progress','checkpoint_state':recovery_state} for i in issue.get('issues',[])]) from exc
                    instruction=('Saved executable components exist; continue from the exact draft hash and candidate, retaining successful evidence reads.'
                        if current['draft_has_components'] or current['candidate_quads'] else
                        'The draft is empty and the candidate contains no facts. There is no executable work to resume. Reuse the recorded evidence reads; save a small evidence-backed contract component or trial it instead of repeating the full design in prose.')
                    for i in issue.get('issues',[]):i['required_change']=instruction;i['checkpoint_state']=recovery_state
                    write_json(draft/'feedback.json',feedback)
                    restarts+=1
                elif issue['target'] in {'format','arguments'}:
                    if not unbounded and config.max_format_repairs is not None and format_repairs>=config.max_format_repairs:raise
                    format_repairs+=1
                else:
                    if not unbounded and config.max_repairs is not None and repairs>=config.max_repairs:raise ValueError("ontology semantic repair budget exhausted: "+str(exc)) from exc
                    repairs+=1;modeled=False
                if unbounded and issue['target']!='control':
                    from .recovery import artifact_snapshot,continuation_reads,compact_artifacts
                    inherited_reads=continuation_reads(recovery_state,last_directory)
                    recovery_state={'schema_version':'dataelf.recovery.v1',**artifact_snapshot(draft,service,candidate),'confirmed_reads':inherited_reads,
                        'previous_review':previous_review,'decision_status':'Saved modeling decisions remain unvalidated; cached observations retain their source/candidate bindings.'}
                    if (last_directory/'context.json').exists():
                        ctx=json.loads((last_directory/'context.json').read_text())
                        recovery_state['artifacts']=compact_artifacts(last_directory/'context.json',ctx)
                    write_json(draft/'recovery.json',recovery_state)
                # Transport/format recovery during review keeps the validated
                # model and candidate. Only semantic issues require remodeling.
                stage='review' if modeled else 'modeling'
                persist()
    except (RunCancelled,KeyboardInterrupt):
        if skill_review(request):write_json(active_path,{'task_key':task_key,'run_id':run_id,'candidate':candidate.model_dump(mode='json'),'status':'cancelled'})
        write_json(draft/'failure.json',{'stage':stage,'status':'cancelled','task_outcome':'cancelled',
            'published':False,'graph_ref':candidate.model_dump(mode='json'),'checkpoint_directory':str(draft/'checkpoints/modeler')});raise
    except Exception as exc:
        write_json(draft/'failure.json',{'stage':stage,'error':str(exc),'graph_ref':candidate.model_dump(mode='json')})
        timeout=isinstance(exc,(TimeoutError,subprocess.TimeoutExpired)) or (isinstance(exc,OntologyError) and exc.feedback['code']=='ONTOLOGY_TIMEOUT')
        code='ONTOLOGY_TIMEOUT' if timeout else exc.feedback['code'] if isinstance(exc,OntologyError) else 'ONTOLOGY_TEMPLATE_DOMAIN_MISMATCH' if isinstance(exc,TemplateDomainError) else 'ONTOLOGY_'+stage.upper()+'_FAILED'
        from .control import metrics
        from .recovery import artifact_snapshot
        retained=artifact_snapshot(draft,service,candidate)
        partial=retained['draft_has_components'] or retained.get('last_successful_trial') or retained['candidate_has_contract']
        exhausted=isinstance(exc,OntologyError) and exc.feedback['target']=='control' and (
            'BUDGET_EXHAUSTED' in exc.feedback['code'] or exc.feedback['code'] in {'ONTOLOGY_CLOSING_INTERRUPT','ONTOLOGY_USAGE_UNAVAILABLE'})
        return OntologyRunResult(status='incomplete' if timeout or exhausted else 'failed',stage=stage,graph_id=run_id,graph_ref=candidate.model_dump(mode='json'),
            error_code=code,error_message=str(exc),details={**metrics(draft/'control.json'),'modelCalls':calls,'agentSessions':calls,'controlRestarts':restarts,'reviewRounds':len(review_history),
                'checkpoint_directory':str(draft/'checkpoints/modeler'),'candidate_workspace':str(service.store.path(candidate)),
                'task_outcome':'partial' if partial else 'failed','artifact_state':retained,'published':False})
