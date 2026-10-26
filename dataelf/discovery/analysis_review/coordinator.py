"""Unbounded AI Index analyst/reviewer loop over existing workspace artifacts."""
import hashlib
import json
from pathlib import Path
import time

from dataelf.discovery.contracts import AgentResources, ExplorerRunResult, ReviewResult
from dataelf.discovery.run_control import check_cancelled, current_run, RunCancelled
from dataelf.discovery.pi_cli_explorer import PiCliInsightsExplorer
from dataelf.schemas import new_id
from dataelf.ontology.graph.store import write_json
from dataelf.ontology.sources import file_hash
from dataelf.ontology.graph.contracts import GraphRef
from dataelf.ontology.wire_schema import provider_schema
from .contracts import POLICY_VERSION, check_decision
from .submissions import install, verify
from .tools import schemas, DESCRIPTIONS


def evidence_excerpt(path):
    """Completed tool results only, bounded; prose is never promoted to facts."""
    if not path.exists():return {}
    observations=[];calls={};requests=0;responses=0;known=0;unknown=0;markup_only=False
    for event_line,line in enumerate(path.open(),1):
        try:event=json.loads(line)
        except ValueError:continue
        if event.get('type')=='message_start' and event.get('message',{}).get('role')=='assistant':requests+=1
        message=event.get('message',{})
        if event.get('type')=='message_end':
            for call in message.get('content',[]) if isinstance(message.get('content'),list) else []:
                if call.get('type')=='toolCall':calls[call['id']]=call
        if event.get('type')=='message_end' and message.get('role')=='assistant':
            responses+=1;usage=message.get('usage') or {}
            content=message.get('content',[])
            blocks=content if isinstance(content,list) else [{'type':'text','text':content}]
            text=''.join(b.get('text','') for b in blocks if b.get('type')=='text').strip()
            markup_only=(message.get('stopReason')=='stop' and text.startswith('<tool_call>')
                         and text.endswith('</tool_call>') and not any(b.get('type')=='toolCall' for b in blocks))
            total=usage.get('totalTokens')
            if isinstance(total,(int,float)) and total>0:known+=total
            else:unknown+=1
        if event.get('type')=='tool_execution_end':
            from dataelf.ontology.sources import digest
            call=calls.get(event.get('toolCallId'),{})
            result_hash=digest(event.get('result'))
            ident=digest({'log':str(path),'call':event.get('toolCallId'),'line':event_line,'result':result_hash})
            saved=path.parent/'recovery_results'/(ident+'.json')
            write_json(saved,event.get('result'))
            body=json.dumps(event.get('result'),ensure_ascii=False)
            observations.append({'tool':event.get('toolName'),'tool_call_id':event.get('toolCallId'),
                'arguments':call.get('arguments'), 'result_id':ident,'result_hash':result_hash,
                'evidence_excerpt':{'representation':'serialized_tool_result_fragment','text':body[:1200],
                    'excerpt_truncated':len(body)>1200,'original_result_truncated':(event.get('result',{}).get('details') or {}).get('truncated')},
                'next_request':{'tool':'analysis_recovery_read','arguments':{'result_id':ident}},
                'is_error':event.get('isError'),'complete_result_log':str(path)})
            observations=observations[-6:]
    return {'schema_version':'dataelf.recovery.v1','confirmed_reads':observations,'requests':requests,'responses':responses,'known_tokens':known,
            'usage_unknown_responses':unknown,'interrupted_usage_unknown':max(0,requests-responses),
            'last_response_only_tool_markup':markup_only,
            'semantics':'Tool result excerpts, possibly truncated. Full originals remain in log. No assistant design is a verified fact.'}


def _actor(config,role,index,explorer):
    if explorer is not None and not isinstance(explorer,PiCliInsightsExplorer):return explorer
    pi=config.explorer.pi;section=config.domain_config('ai_index').get('analysis',{})
    return PiCliInsightsExplorer(pi_binary=pi.binary,model=section.get(role+'_model') or pi.model,
        mode=pi.mode,cwd=pi.cwd,timeout_seconds=None,extra_args=pi.extra_args,log_mode=pi.log_mode,
        log_prefix='pi' if role=='analyst' and index==1 else f'analysis_{role}_{index:04d}',compact_stream_events=True,detect_model_errors=True)


def run_analysis(job,context,contract,config,*,analyst=None,reviewer=None):
    from dataelf.discovery.execution_policy import ai_index_research
    if not ai_index_research(job.spec):
        raise ValueError('Independent research review requires an explicit registered AI Index workflow policy')
    check_cancelled()
    workspace=Path(context.workspace_path).resolve();root=workspace/'reviews/analysis';root.mkdir(parents=True,exist_ok=True)
    original=Path(context.prompt_path).read_text();previous=None;submission=None;feedback=None;history=[];index=0
    started=time.monotonic();role='analyst';recovery={};artifacts=[]
    # Original evidence and published graphs are protected across revisions.
    protected={str(p.relative_to(workspace)):file_hash(p) for p in (workspace/'raw').rglob('*') if p.is_file()}
    from dataelf.ontology.capabilities import skill_review
    modern=skill_review(job.spec)
    if modern:
        from dataelf.domains.ai_index.evidence import SKILL_ROOT
        context=context.model_copy(update={'agent_resources':context.agent_resources.model_copy(update={
            'skills':list(dict.fromkeys([*context.agent_resources.skills,SKILL_ROOT/'SKILL.md']))})})
    journal=root/'state.json'
    if journal.exists():
        saved=json.loads(journal.read_text());index=saved.get('session',0);previous=saved.get('previous_review');history=saved.get('history',[])
        if saved.get('workflow_policy', job.spec.workflow_policy) != job.spec.workflow_policy:
            raise ValueError('Saved workflow selection does not match the requested analysis recovery')
        protected=saved.get('protected_hashes',protected);submission=saved.get('submission');role=saved.get('role','analyst');feedback=saved.get('feedback')
        if submission:verify(workspace,submission,live=False)
        restored=root/'sessions'/f'{index:04d}_{role}'/'usage_and_recovery.json'
        if restored.exists():recovery=json.loads(restored.read_text())
        if modern:
            if saved.get('phase')=='cancelled':raise RunCancelled('Cancelled analysis is not automatically resumed')
            directory=root/'sessions'/f'{index:04d}_{role}'
            # Reconcile completed native handoffs before creating any new model
            # session. A crash after submit/approval must not redo author work.
            if role=='analyst' and (directory/'analysis_submission.json').exists():
                incoming=json.loads((directory/'analysis_submission.json').read_text())
                if incoming['spec']!=job.spec.model_dump(mode='json') or incoming['contract']!=contract.model_dump(mode='json') or incoming.get('previous_review')!=previous:
                    raise ValueError('Saved submission does not match the original task and review')
                verify(workspace,incoming,live=True);submission=incoming
                protected.update({k:v for k,v in submission['files'].items() if k.startswith('raw/')})
                role='reviewer';feedback=None
            elif role=='reviewer' and (directory/'analysis_review.json').exists():
                decision=check_decision(json.loads((directory/'analysis_review.json').read_text()),submission,previous)
                if not any(h['session']==index for h in history):
                    history.append({'submission_id':submission['submission_id'],'submission_hash':submission['submission_hash'],
                        'decision':decision.model_dump(mode='json'),'session':index})
                from dataelf.ontology.collaboration import ReviewLedger
                ReviewLedger(workspace,'analysis').record_review(decision.model_dump(mode='json'))
                previous=decision.model_dump(mode='json')
                if decision.verdict=='approve' and decision.task_assessment=='fulfilled':
                    frozen=json.loads((directory/'analysis_review_evidence.json').read_text())
                    outputs=install(workspace,submission,previous,reviewer_evidence=frozen)
                    saved.update(phase='completed',role='analyst',previous_review=previous,history=history,submission=submission)
                    write_json(journal,saved)
                    review=ReviewResult(review_id=new_id('review'),job_id=job.job_id,
                        status='pass_with_warnings' if decision.issues else 'pass',warnings=[i.message for i in decision.issues],
                        metrics={'independent':True,'review_rounds':len(history)})
                    return ExplorerRunResult(status='completed',artifacts=outputs),review
                if decision.verdict=='blocked':
                    return ExplorerRunResult(status='failed',error_code='ANALYSIS_INCOMPLETE',error_message=decision.summary),None
                role='analyst';feedback={'review':previous}
            if saved.get('phase')=='completed':
                approved=json.loads((root/'approved.json').read_text())
                outputs=install(workspace,submission,approved['decision'],reviewer_evidence=approved['reviewer_evidence'])
                return ExplorerRunResult(status='completed',artifacts=outputs),ReviewResult(review_id=new_id('review'),job_id=job.job_id,
                    status='pass_with_warnings' if approved['decision']['issues'] else 'pass',warnings=[i['message'] for i in approved['decision']['issues']],
                    metrics={'independent':True,'review_rounds':len(history)})
    while True:
        if modern:
            from dataelf.ontology.collaboration import ReviewLedger
            from .collaboration_runtime import run_local_analysis,run_graph_review
            for family in ['graph','analysis']:
                ledger=ReviewLedger(workspace,family)
                for request in ledger.requests():
                    if request['status']=='delivered':continue
                    needs_publication=family=='graph' and request['mode']=='formal' and (request.get('feedback') or {}).get('verdict')=='approve' and not request.get('publication')
                    if not request.get('feedback') or needs_publication:
                        if family=='graph':run_graph_review(job,context,config,request)
                        else:artifacts.extend(run_local_analysis(job,context,contract,config,request,reviewer))
                    request=ledger.load(request['request_id'])
                    if request.get('feedback'):
                        feedback={'local_or_graph_review':request['feedback'],'request_id':request['request_id'],'review_family':family}
                        ledger.stage(request['request_id'],'author_scheduled')
            ledger=ReviewLedger(workspace,'analysis')
            prior=ledger.prior()
            if prior['issues'] or previous:previous={**(previous or {}),'issues':prior['issues']}
            versions=workspace/'artifacts/analysis_graph_ref.json'
            if context.graph_ref and versions.exists():
                new=json.loads(versions.read_text())
                context=context.model_copy(update={'graph_ref':new['graph_ref']})
        check_cancelled();index+=1;directory=root/'sessions'/f'{index:04d}_{role}';directory.mkdir(parents=True,exist_ok=False)
        state={'phase':'reviewing' if role=='reviewer' else 'revising' if previous or feedback else 'analyzing',
               'workflow_policy':job.spec.workflow_policy,
               'role':role,'session':index,'previous_review':previous,'history':history,'submission':submission,
               'protected_hashes':protected,'feedback':feedback,'elapsed_seconds':time.monotonic()-started}
        write_json(journal,state)
        if current_run():current_run().emit('analysis_'+state['phase'])
        section=config.domain_config('ai_index').get('analysis',{})
        task={'role':role,'workspace':str(workspace),'spec':job.spec.model_dump(mode='json'),
              'contract':contract.model_dump(mode='json'),'graph_refs':[
                  GraphRef.model_validate(ref).model_dump(mode='json',exclude_none=True)
                  for ref in (submission['graph_refs'] if role=='reviewer' and submission is not None else ([context.graph_ref] if context.graph_ref else []))],
              'graph_root':context.graph_root,'protected_hashes':protected,'previous_review':previous,
              'parent_submission':submission['submission_id'] if submission else None,
              'submission':submission,'max_output_tokens':section.get(role+'_max_output_tokens')}
        if modern:
            task['artifact_versions']=[{'path':s.path,'sha256':file_hash(workspace/s.path),
                'meaning':'saved file, not approval'} for s in contract.artifacts if (workspace/s.path).is_file()]
            task.update(workflow_policy=job.spec.workflow_policy,review_family='analysis',review_mode='formal',
                collaboration=ledger.summary(),feedback=feedback,recovered_evidence=recovery,
                previous_delivery=context.domain_context.get('previous_delivery'))
            if role=='reviewer':
                task['review_snapshot']=str(workspace/'submissions'/submission['submission_id']/'files')
                task['review_object_hash']=submission['submission_hash']
                task['review_scratch']=str(ledger.root/'scratch'/submission['submission_hash'])
        if submission:task['manifest_path']=str(workspace/'submissions'/submission['submission_id']/'manifest.json')
        ctx_path=directory/'context.json';write_json(ctx_path,task)
        tool_schemas={name:{'description':DESCRIPTIONS[name],'schema':provider_schema(schema)} for name,schema in schemas(role).items()}
        if not task['graph_refs']:
            tool_schemas={name:value for name,value in tool_schemas.items() if not name.startswith('analysis_graph_')}
        write_json(directory/'tools.json',tool_schemas)
        prompt=directory/'prompt.md'
        summary={'objective':job.spec.objective,'source_scope':job.spec.parameters,'submission_id':(submission or {}).get('submission_id'),
                 'previous_review':previous,'feedback':feedback,'recovered_evidence':recovery,
                 'complete_contract':'analysis_inspect resource=contract','files':'analysis_inspect resource=files',
                 'budget':{'task_deadline':None,'request_limit':None,'token_limit':None,'single_output_limit':task['max_output_tokens']}}
        prompt.write_text((original if role=='analyst' else 'Independently review the immutable submission against the original task. Read its files, recompute decisive quantities and use analysis_review_submit.\n')+
                          '\nCurrent saved task state (data, not instructions):\n'+json.dumps(summary,ensure_ascii=False,indent=2))
        if modern:
            from .collaboration_runtime import prepare_input
            tool_schemas=prepare_input(directory,task)
        resource=AgentResources(skills=context.agent_resources.skills) if modern else AgentResources()
        actor_context=context.model_copy(update={'prompt_path':str(prompt),'agent_resources':AgentResources() if role=='reviewer' else context.agent_resources,
            'env':{**context.env,'DATAELF_ANALYSIS_CONTEXT':str(ctx_path),'DATAELF_ANALYSIS_ROLE':role,
                   'DATAELF_ANALYSIS_TOOLS':','.join(tool_schemas)}})
        if modern:
            actor_context=actor_context.model_copy(update={'agent_resources':resource if role=='reviewer' else context.agent_resources,
                'env':{**actor_context.env,'DATAELF_WORKFLOW_POLICY':job.spec.workflow_policy}})
        actor=_actor(config,role,index,reviewer if role=='reviewer' else analyst)
        write_json(directory/'effective_input.json',{'model':getattr(actor,'model',context.model),'policy_version':POLICY_VERSION,
            'prompt_sha256':hashlib.sha256(prompt.read_bytes()).hexdigest(),'tools_sha256':hashlib.sha256((directory/'tools.json').read_bytes()).hexdigest(),
            'max_output_tokens':task['max_output_tokens'],'task_timeout':None,'tool_choice':'auto'})
        try:
            result=actor.run(job,actor_context);check_cancelled()
        except BaseException as exc:
            state['phase']='cancelled' if isinstance(exc,(RunCancelled,KeyboardInterrupt)) else 'interrupted'
            write_json(journal,state)
            raise
        artifacts.extend(result.artifacts)
        if modern and role=='analyst':
            for family in ['graph','analysis']:
                pending=ReviewLedger(workspace,family)
                for req in pending.requests():
                    if req['status']=='author_scheduled':pending.stage(req['request_id'],'delivered')
        events=workspace/'logs'/f'{getattr(actor,"log_prefix","pi")}_events.jsonl'
        from dataelf.ontology.recovery_contract import merge_recovery
        recovery=merge_recovery(recovery,evidence_excerpt(events))
        recovery['last_stop_reason']=result.error_code or ('formal_submission_saved' if (directory/('analysis_review.json' if role=='reviewer' else 'analysis_submission.json')).exists() else 'no_formal_submission')
        if modern:
            recovery['artifact_versions_before']=task.get('artifact_versions',[])
            recovery['artifact_versions_after']=[{'path':s.path,'sha256':file_hash(workspace/s.path)} for s in contract.artifacts if (workspace/s.path).is_file()]
            recovery['review_record_ids']=[c['check_id'] for c in ledger.checks()]
        write_json(directory/'usage_and_recovery.json',recovery)
        write_json(directory/'process_result.json',result.model_dump(mode='json'))
        if modern and (directory/'handoff.json').exists():
            marker=json.loads((directory/'handoff.json').read_text())
            if marker['kind'] in {'local_review','graph_review'}:
                role='analyst';feedback={'handoff':marker};continue
        submitted=directory/('analysis_review.json' if role=='reviewer' else 'analysis_submission.json')
        if submitted.exists():
            if role=='analyst':
                incoming=json.loads(submitted.read_text())
                try:
                    if incoming['spec']!=job.spec.model_dump(mode='json') or incoming['contract']!=contract.model_dump(mode='json') or incoming['policy_version']!=POLICY_VERSION:
                        raise ValueError('Submission cannot change the original task, contract or policy')
                    if incoming.get('previous_review')!=previous:raise ValueError('Submission cannot discard or replace the previous review')
                    if any(incoming['files'].get(rel)!=expected for rel,expected in protected.items()):
                        raise ValueError('Submission cannot replace already frozen source evidence')
                    verify(workspace,incoming,live=True)
                except ValueError as exc:
                    feedback={'submission_invalid':str(exc)};continue
                submission=incoming
                for rel,value in submission['files'].items():
                    if rel.startswith('raw/'):protected[rel]=value
                role='reviewer';feedback=None;continue
            try:
                decision=check_decision(json.loads(submitted.read_text()),submission,previous)
            except ValueError as exc:feedback={'review_protocol_error':str(exc)};continue
            history.append({'submission_id':submission['submission_id'],'submission_hash':submission['submission_hash'],
                            'decision':decision.model_dump(mode='json'),'session':index})
            write_json(root/'history.json',history)
            previous=decision.model_dump(mode='json')
            if modern:ledger.record_review(previous)
            if decision.verdict=='approve' and decision.task_assessment=='fulfilled':
                extra={}
                if modern:
                    extra['reviewer_evidence']=json.loads((directory/'analysis_review_evidence.json').read_text())
                try:outputs=install(workspace,submission,previous,**extra)
                except ValueError as exc:
                    role='analyst';feedback={'approval_invalidated':str(exc)};continue
                state.update(phase='completed',role='analyst',previous_review=previous,history=history,submission=submission)
                write_json(journal,state)
                review=ReviewResult(review_id=new_id('review'),job_id=job.job_id,
                    status='pass_with_warnings' if decision.issues else 'pass',
                    warnings=[i.message for i in decision.issues],metrics={'independent':True,'review_rounds':len(history)})
                return ExplorerRunResult(status='completed',artifacts=artifacts+outputs),review
            if decision.verdict=='blocked' or decision.task_assessment!='fulfilled' and decision.verdict=='approve':
                write_json(root/'blocked.json',previous)
                return ExplorerRunResult(status='failed',artifacts=artifacts,error_code='ANALYSIS_INCOMPLETE',
                    error_message=json.dumps({'task_assessment':decision.task_assessment,'summary':decision.summary,'blocker':decision.blocker},ensure_ascii=False)),None
            role='analyst';feedback={'review':previous};continue
        if result.error_code=='RUN_CANCELLED':
            raise RunCancelled('AI Index analysis cancelled')
        if recovery.get('last_response_only_tool_markup'):
            return ExplorerRunResult(status='failed',artifacts=artifacts,error_code='PI_NATIVE_TOOL_CHANNEL_INCOMPATIBLE',
                error_message='Pi received only tool-call markup in ordinary text, with no native toolCall. Nothing was executed; inspect the captured raw response and adapter events.'),None
        transport=directory/'analysis_transport.json'
        if transport.exists():
            detail=json.loads(transport.read_text())
            # The native-call guard deliberately aborts Pi before partial
            # arguments execute. That owned abort is not a worker crash.
            # Preserve a real provider fault instead of masking it as format.
            from dataelf.ontology.errors import classify_model_error, OntologyError
            errors=[i['error_message'] for i in detail.get('issues',[]) if i.get('error_message')]
            if errors and not isinstance(classify_model_error(errors,role),OntologyError):
                return ExplorerRunResult(status='failed',artifacts=artifacts,error_code='PI_MODEL_ERROR',
                    error_message='; '.join(errors)),None
            feedback={'reason':detail.get('code'),'native_transport':detail,
                'message':'Incomplete native arguments were not executed. Continue from the saved files; issue a complete native tool call.'}
            continue
        if result.status=='failed' and result.error_code not in {'PI_OUTPUT_TRUNCATED','PI_MODEL_ERROR','PI_EVENT_PARSE_ERROR'}:
            return result,None
        if result.error_code=='PI_MODEL_ERROR':
            from dataelf.ontology.errors import classify_model_error, OntologyError
            classified=classify_model_error([result.error_message or ''],role)
            if not isinstance(classified,OntologyError):return result,None
        feedback={'reason':result.error_code or 'NO_FORMAL_SUBMISSION',
            'message':'Continue the same goal and saved files. Save the complete business report and use analysis_submit.' if role=='analyst'
                      else 'Continue reading the same immutable submission and submit analysis_review_submit. No prose approval.',
            'last_error':result.error_message}
        write_json(directory/'continuation.json',feedback)
