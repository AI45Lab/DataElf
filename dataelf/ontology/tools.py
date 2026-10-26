"""Bounded, role-scoped tools and checkpointed authoring."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from rdflib import Dataset
from .contracts import OntologyRequest,OntologyContract,ExtractionPlan,Review
from .engine import materialize,preflight_contract,validate_plan,validate_query,validate_contract
from .tool_contracts import ADAPTERS,normalize_payload,validate_arguments
from .templates import require_domain
from .sources import SourceCatalog,digest,select,write_json
from .expressions import CONTEXT_FIELDS
from . import resources
from .read_cache import context_feedback
from .capabilities import skill_review
from .tool_results import control_event as control_receipt
from .workflow_state import (load_state,save_state,status,checkpoint_lock,register_item,
    edited,validate_component,component_validated,mark_progress)


def available_tools(context,directory,state=None,*,all_tools=False):
    role=context['role'];names=['ontology_inspect','ontology_evidence','ontology_submit']
    if role in ('modeler','stage1_generator'):
        names += ['ontology_work','ontology_author','ontology_draft','ontology_execute']
        if context.get('control_limits',{}).get('author_interface')=='split':
            from .component_tools import OPERATIONS
            names += list(OPERATIONS)
    if context.get('graph_path') or context.get('graph_ref'):names.append('ontology_query')
    if context.get('graph_ref'):
        from .graph.tools import tool_names
        names += tool_names(read_only=role in ('reviewer','stage1_reviewer','stage2_reviewer'))
    if skill_review(context):
        from dataelf.domains.ai_index.skill_tools import schemas
        names += list(schemas(role))
        from .collaboration_tools import schemas as collaboration_schemas
        names += list(collaboration_schemas(context))
        if context.get('review_mode')=='local':names.remove('ontology_submit')
    if skill_review(context) and context.get('graph_ref') and not all_tools:
        names=[n for n in names if n!='ontology_query']
    if all_tools or role not in ('modeler','stage1_generator'):return names
    defaults=['ontology_inspect','ontology_evidence','ontology_draft','ontology_execute','ontology_submit']
    if context.get('control_limits',{}).get('component_tools',True):
        if context.get('control_limits',{}).get('author_interface')=='split':
            from .component_tools import OPERATIONS
            defaults += list(OPERATIONS)
        else:defaults.append('ontology_author')
    defaults += [n for n in names if n in ('graph_inspect','graph_query','graph_explore','graph_validate','graph_diff')]
    if skill_review(context):defaults += [n for n in names if n.startswith(('ai_index_','review_'))]
    if skill_review(context):defaults=[n for n in defaults if n!='ontology_submit']
    path=Path(directory)/'enabled_tools.json'
    enabled=json.loads(path.read_text()) if path.exists() else []
    return list(dict.fromkeys(defaults+[n for n in enabled if n in names]))


def finish(context_path,context,result,progress_key=None):
    work=load_state(context_path,context)
    if context['role'] in ('modeler','stage1_generator') and progress_key:
        with checkpoint_lock(context_path,context):
            work=load_state(context_path,context)
            if work['current_item'] in work['items']:
                work['items'][work['current_item']]['researched']=True
                if work['step']=='research':work['step']='build'
            mark_progress(work,'evidence',progress_key);save_state(context_path,context,work)
    from .draft import draft_state
    return {**result,'available_tools':available_tools(context,context_path.parent,work),
        **({'workflow':status(work),'draft_hash':digest(draft_state(context_path,context))} if context['role'] in ('modeler','stage1_generator') else {}),
        **({'progress_key':progress_key} if progress_key else {})}


def verify_evidence(mapping,catalog):
    if not mapping.evidence:raise ValueError('mapping lacks evidence: '+mapping.id)
    for reference in mapping.evidence:
        file,sep,pointer=reference.partition('#')
        if not sep or not any(r.file==file and (pointer==r.pointer or (pointer.startswith(r.pointer+'/') and select(r.data,pointer[len(r.pointer):]))) for r in catalog.records):
            raise ValueError('mapping cites unresolved evidence: '+reference)


def profile_continuation(tool,payload,value,page):
    """Finish exact byte pages before advancing the field index."""
    args={k:payload[k] for k in ('source','field_path','field_offset','limit','max_bytes','page_offset')}
    args['resource' if tool=='ontology_inspect' else 'operation']='profile'
    if page['next_offset'] is not None:args['page_offset']=page['next_offset']
    elif value.get('next_offset') is not None:args.update(field_offset=value['next_offset'],page_offset=0)
    else:args=None
    return {'field_offset':payload['field_offset'],'next_field_offset':value.get('next_offset'),
        'next_request':{'tool':tool,'arguments':args} if args is not None else None}


def dispatch(context_path:Path,name:str,payload:dict):
    context=json.loads(context_path.read_text());role=context['role']
    from .capabilities import skill_review
    if skill_review(context) and name.startswith('ai_index_'):
        from dataelf.domains.ai_index.skill_tools import dispatch as source_dispatch
        return source_dispatch(context_path,name,payload)
    if skill_review(context) and name.startswith('review_'):
        from .collaboration_tools import dispatch as review_dispatch
        return review_dispatch(context_path,name,payload)
    from .component_tools import OPERATIONS, ADAPTERS as COMPONENT_ADAPTERS
    if name in OPERATIONS:
        if role not in ('modeler','stage1_generator') or context.get('control_limits',{}).get('author_interface')!='split':
            raise ValueError('Independent component tools require split modeler permissions')
        args=COMPONENT_ADAPTERS[name].validate_python(normalize_payload(payload)).model_dump(mode='json',exclude_unset=True)
        return dispatch(context_path,'ontology_author',{'action':OPERATIONS[name][0],**args})
    if role=='stage2_generator':raise ValueError('Stage2 generator is retired; use the autonomous modeler')
    if name=='ontology_control':
        from .control import control_event
        return control_event(context_path,payload)
    if name not in {*ADAPTERS,'ontology_submit'}:raise ValueError('unknown ontology tool')
    if name=='ontology_execute' and role not in ('modeler','stage1_generator'):raise ValueError('reviewers cannot execute')
    if name in ('ontology_draft','ontology_author','ontology_work') and role not in ('modeler','stage1_generator'):raise ValueError('only the Stage1 generator may edit its contract draft')
    payload=normalize_payload(payload)
    from .task_sources import task_catalog
    request,catalog=task_catalog(context)
    if not skill_review(context) and catalog.fingerprint!=context['source_fingerprint']:raise ValueError('source snapshot changed')
    if name!='ontology_submit':payload=validate_arguments(name,payload)
    if name=='ontology_inspect':
        resource=payload['resource']
        if resource=='delivery':
            from .control import budget_status
            from .recovery import compact_artifacts
            return finish(context_path,context,{'budget':budget_status(context_path,context),
                'artifacts':compact_artifacts(context_path,context)})
        if resource=='summary':
            summary={'role':role,'domain':request.domain,'objective':request.objective,
                'sources':[{'id':s.source_id,'path':str(s.path),'records_path':s.records_path,
                    'evidence_roots':[r.file+'#'+r.pointer for r in catalog.records if r.source==s.source_id][:3]} for s in catalog.sources],
                'documents':[str(p) for p in request.documents],
                'context_keys':sorted(set(CONTEXT_FIELDS)|{k for r in catalog.records for k in r.metadata}),
                'resources':['schema','profile','document','draft','contract','feedback','validation','lineage','review_checks','trial','language'],
                'feedback':context_feedback(context.get('feedback',[])[-1:]),'graph_ref':context.get('graph_ref')}
            summary['optional_tools']=[n for n in available_tools(context,context_path.parent,all_tools=True) if n not in available_tools(context,context_path.parent)]
            summary['permitted_tools']=available_tools(context,context_path.parent,all_tools=True)
            summary['load_tool_hint']='ontology_inspect(resource="schema", tool=the_tool_name) loads its schema and enables that role-permitted tool. Contract schemas use field_path for sections.'
            if context.get('recovery_state'):summary['recovery_state']=context['recovery_state']
            from .control import budget_status
            summary['budget']=budget_status(context_path,context)
            from .draft import draft_state
            if role in ('modeler','stage1_generator'):
                saved=draft_state(context_path,context)
                summary['saved_artifacts']={'draft_hash':digest(saved),
                    'draft_components':{k:len(saved.get(k) or {}) for k in ('classes','properties','mappings')}}
                from .workflow_state import checkpoint_directory
                last_trial=checkpoint_directory(context_path,context)/'last_trial.json'
                if last_trial.exists():summary['saved_artifacts']['last_trial']=json.loads(last_trial.read_text())
            if context.get('graph_ref') and context.get('graph_root'):
                from .graph import GraphService
                summary['base_graph_ref']=GraphService(context['graph_root'],request.workspace).inspect(context['graph_ref'])['base_graph_ref']
            if context.get('review_checks'):
                summary['saved_review_checks']={'count':len(context['review_checks']),
                    'candidate_hash':context.get('candidate_hash'),'resource':'review_checks',
                    'tools':[check['tool'] for check in context['review_checks']]}
            if context.get('review_context'):summary['review_context']=context['review_context']
            if context.get('previous_review'):summary['previous_review']=context['previous_review']
            if 'contract_hash' in context:summary['contract_hash']=context['contract_hash']
            return finish(context_path,context,summary)
        if resource=='schema':
            if payload.get('tool'):
                name_to_load=payload['tool']
                if name_to_load not in available_tools(context,context_path.parent,all_tools=True):raise ValueError('tool is not permitted for this role or graph reference')
                if name_to_load=='ontology_submit':value=json.loads(context_path.with_name('schema.json').read_text())
                elif name_to_load in ADAPTERS:value=ADAPTERS[name_to_load].json_schema()
                elif name_to_load in COMPONENT_ADAPTERS:value=COMPONENT_ADAPTERS[name_to_load].json_schema()
                elif name_to_load.startswith(('ai_index_','review_')):value=json.loads(context_path.with_name('tool_schemas.json').read_text())[name_to_load]
                else:value=json.loads(context_path.with_name('graph_tool_schemas.json').read_text())[name_to_load]
                loaded=context_path.with_name('enabled_tools.json')
                enabled=json.loads(loaded.read_text()) if loaded.exists() else []
                write_json(context_path.with_name('enabled_tools.json'),list(dict.fromkeys([*enabled,name_to_load])))
            else:value=json.loads(context_path.with_name('schema.json').read_text())
            if payload.get('field_path'):
                matches=select(value,payload['field_path'])
                if len(matches)!=1:raise ValueError('schema section must resolve exactly once')
                value=matches[0][1]
        elif resource=='language':
            from .guidance import execution_reference
            value=execution_reference()
        elif resource=='draft':
            from .draft import draft_state
            value=json.loads((Path(context['review_snapshot'])/'review_object.json').read_text()) if skill_review(context) and role.endswith('reviewer') and context.get('review_mode')=='local' else draft_state(context_path,context)
        elif resource=='contract':value=context.get('contract')
        elif resource=='trial':
            from .workflow_state import checkpoint_directory
            if not payload['trial_id']:raise ValueError('trial inspection requires trial_id')
            trial_path=checkpoint_directory(context_path,context)/'trials'/payload['trial_id']/'result.json'
            value=json.loads(trial_path.read_text())
            if payload['field_path']:
                matches=select(value,payload['field_path'])
                if len(matches)!=1:raise ValueError('trial section must resolve exactly once')
                value=matches[0][1]
        elif resource=='profile':value=resources.profile(catalog,payload['source'],payload['field_path'],payload['field_offset'],payload['limit'])
        elif resource=='document':
            path=next((p for p in request.documents if str(p)==payload['document']),None)
            if path is None:raise ValueError('document is not in the supplied resource directory')
            value=path.read_text()
        else:value=context.get(resource)
        if payload.get('mapping_id') and isinstance(value,dict) and 'mappings' in value:
            value=next((m for m in value['mappings'] if m['id']==payload['mapping_id']),None)
        # profile offset is a field index; all other offsets are exact UTF-8 byte positions.
        byte_offset=(payload['page_offset'] or payload['offset']) if resource=='profile' else payload['offset']
        page=resources.json_page(value,byte_offset,payload['max_bytes'])
        key=None
        if resource in ('profile','document','validation','lineage'):
            key=digest({'resource':resource,'source':payload['source'],'document':payload['document'],'offset':payload['offset'],'value':value})
        extra=profile_continuation(name,payload,value,page) if resource=='profile' else {
            'next_request':{'tool':name,'arguments':{**payload,'offset':page['next_offset']}} if page['next_offset'] is not None else None}

        return finish(context_path,context,{'resource':resource,**page,**extra},key)
    if name=='ontology_evidence':
        operation=payload['operation']
        from .read_cache import read,save_read
        if operation=='cached':return finish(context_path,context,read(context_path,context,payload))
        def evidence_result(value,key=None):
            return save_read(context_path,context,name,payload,finish(context_path,context,value,key))
        if operation=='profile':
            value=resources.profile(catalog,payload['source'],payload['field_path'],payload['field_offset'],payload['limit'])
            page=resources.json_page(value,payload['page_offset'],payload['max_bytes'])
            return evidence_result({'resource':operation,**page,**profile_continuation(name,payload,value,page)},digest({'op':operation,'value':value}))
        if operation=='sample':
            value=resources.sample(catalog,payload)
            value.update(pagination_layer='source_record_index',next_request={'tool':name,'arguments':{**payload,'offset':value['next_offset']}} if value['next_offset'] is not None else None)
            key=digest([{'file':r['file'],'pointer':r['pointer']} for r in value['records']]) if value['records'] else None
            return evidence_result(value,key)
        if operation=='replay':
            for record in catalog.records:
                if record.file==payload['file'] and (payload['pointer']==record.pointer or payload['pointer'].startswith(record.pointer+'/')):
                    selected=select(record.data,payload['pointer'][len(record.pointer):])
                    if not selected:raise ValueError('pointer has no matches')
                    page=resources.json_page(selected,payload['offset'],payload['max_bytes'])
                    return evidence_result({'file':record.file,'pointer':payload['pointer'],**page, 'pagination_layer':'source_json_bytes',
                        'next_request':{'tool':name,'arguments':{**payload,'offset':page['next_offset']}} if page['next_offset'] is not None else None},digest({'file':record.file,'pointer':payload['pointer'],'offset':payload['offset']}))
            raise ValueError('evidence reference is outside supplied records')
        left={digest(v) for r in catalog.records if r.source==payload['left'] for _,v in select(r.data,payload['left_path']) if v is not None}
        right={digest(v) for r in catalog.records if r.source==payload['right'] for _,v in select(r.data,payload['right_path']) if v is not None}
        return evidence_result({'leftDistinct':len(left),'rightDistinct':len(right),'intersection':len(left&right),'unmatchedLeft':len(left-right)},digest(payload))
    if name=='ontology_query' and context.get('graph_ref'):
        from .graph import GraphService
        return GraphService(context['graph_root'],request.workspace).query(context['graph_ref'],payload['query'])
    if name=='ontology_execute' and role in ('modeler','stage1_generator'):
        from .trials import execute_trial
        with checkpoint_lock(context_path,context):
            return finish(context_path,context,execute_trial(context_path,context,payload))
    if name in ('ontology_draft','ontology_author','ontology_work'):
        with checkpoint_lock(context_path,context):
            try:return dispatch_authoring(context_path,context,name,payload,request,catalog)
            except (ValueError,TypeError) as exc:
                from .errors import error_feedback
                feedback=error_feedback(exc,'contract')
                work=load_state(context_path,context)
                work['issues']=feedback.get('issues') or [{'message':str(exc)[:1500]}]
                save_state(context_path,context,work)
                raise
    if role in ('modeler','stage1_generator'):
        return submit_contract(context_path,context,payload)
    if skill_review(context) and context.get('review_mode')=='local':
        raise ValueError('Local feedback must use review_feedback_submit; it cannot approve publication')
    if skill_review(context):
        from .collaboration import ReviewLedger
        payload=ReviewLedger(request.workspace,'graph').assemble({**context,'_context_path':str(context_path)},payload)
    result=Review.model_validate(payload)
    if skill_review(context):
        from .review_loop import check_review
        from .errors import OntologyError
        try:result=check_review(result,context.get('previous_review'),strict=True)
        except OntologyError as exc:
            # A valid external blocker is a final host-visible verdict, not a
            # malformed tool call to retry inside the reviewer.
            if exc.feedback['code']!='ONTOLOGY_REVIEW_BLOCKED':raise
        previous=context_path.with_name('submission.json')
        if previous.exists() and json.loads(previous.read_text())!=result.model_dump(mode='json'):
            raise ValueError('Formal review is immutable; use a new bound reviewer session')
    write_json(context_path.with_name('submission.json'),result.model_dump(mode='json'))
    return {'submitted':True,'control_event':control_receipt('review_submitted',context.get('candidate_hash') or digest(result.model_dump(mode='json')))}


def dispatch_authoring(context_path,context,name,payload,request,catalog):
    from .draft import draft_state,edit_document,save_draft
    from .author import apply_author
    work=load_state(context_path,context);draft=draft_state(context_path,context);action=payload['action']
    if name=='ontology_work':
        if action=='register':register_item(work,payload,catalog);save_state(context_path,context,work)
        return finish(context_path,context,{'workflow':status(work)})
    if action=='read':
        value=next((m for m in draft['mappings'] if m['id']==payload['mapping_id']),None) if payload.get('mapping_id') else draft
        page=resources.json_page(value,payload['offset'],payload['max_bytes'])
        return finish(context_path,context,{'resource':'draft',**page,'next_request':{'tool':name,
            'arguments':{**payload,'offset':page['next_offset']}} if page['next_offset'] is not None else None})
    if payload['base_hash']!=digest(draft):raise ValueError('stale draft base_hash; inspect the checkpoint before retrying')
    if name=='ontology_author' and action=='validate_component':
        result=validate_component(draft,payload['mapping_id'],catalog)
        mapping=next(m for m in OntologyContract.model_validate(draft).mappings if m.id==payload['mapping_id'])
        verify_evidence(mapping,catalog)
        if payload['mapping_id'] in work['items']:component_validated(work,payload['mapping_id'])
        save_state(context_path,context,work)
        return finish(context_path,context,result)
    if action in ('patch','configure','upsert_class','upsert_property','upsert_mapping','set_identity','add_relation','set_checks'):
        candidate=edit_document(draft,payload['operations']) if action=='patch' else apply_author(draft,payload)
        from .author import component_note, draft_gaps
        note=component_note(candidate,payload,catalog)
        if note:work.setdefault('component_notes',{})[note['component_path']]=note
        edited(work,draft,candidate);save_draft(context_path,candidate);save_state(context_path,context,work)
        return finish(context_path,context,{'saved':True,'status':'saved_unvalidated',
            'component_id':note['component_id'] if note else None,
            'component_path':note['component_path'] if note else None,
            'evidence':note['evidence'] if note else [],'missing_conditions':draft_gaps(candidate,catalog),
            'meaning':'Saved draft only. Dependencies, execution, source coverage and review are not certified.'})
    result=OntologyContract.model_validate(draft);require_domain(result.domain,request.domain)
    for mapping in result.mappings:verify_evidence(mapping,catalog)
    if skill_review(context):
        from .task_sources import task_catalog
        _,catalog=task_catalog(context,result)
    preflight_contract(result,catalog)
    if action=='validate':
        work['global_hash']=digest(draft);work['step']='submit';work['issues']=[]
        mark_progress(work,'gate','global');save_state(context_path,context,work)
        return finish(context_path,context,{'status':'valid','scope':'global'})
    return submit_contract(context_path,context,result.model_dump(mode='json'))


def prepare_candidate(context_path,context,payload):
    request=OntologyRequest.model_validate(context['request'])
    contract=OntologyContract.model_validate(payload);require_domain(contract.domain,request.domain)
    from .task_sources import task_catalog
    request,catalog=task_catalog(context,contract)
    validate_contract(contract,{s.source_id for s in request.sources})
    for mapping in contract.mappings:verify_evidence(mapping,catalog)
    if context.get('graph_ref'):
        from .graph import GraphService
        service=GraphService(context['graph_root'],request.workspace)
        current=service.state(context['graph_ref'])
        if current.get('contract')!=contract.model_dump(mode='json'):
            service.materialize(context['graph_ref'],contract,request.sources,run_id=context.get('run_id'),agent_id=context['role'],require_valid=True)
        report=service.validate(context['graph_ref'])
        if report.status!='valid':
            from .errors import OntologyError
            raise OntologyError('ONTOLOGY_GRAPH_INVALID','contract',report.issues)
        return {'validation':report.model_dump(mode='json'),'graph_ref':context['graph_ref']}
    return {'validation':preflight_contract(contract,catalog)}


def submit_contract(context_path,context,payload):
    prepared=prepare_candidate(context_path,context,payload)
    contract=OntologyContract.model_validate(payload)
    from .draft import save_draft
    save_draft(context_path,contract.model_dump(mode='json'))
    write_json(context_path.with_name('submission.json'),contract.model_dump(mode='json'))
    return {**prepared,'submitted':True,'control_event':control_receipt('candidate_submitted',digest(payload))}


if __name__=='__main__':
    try:print(json.dumps(dispatch(Path(sys.argv[1]),sys.argv[2],json.load(sys.stdin)),ensure_ascii=False,default=str))
    except Exception as exc:
        from pydantic import ValidationError
        if isinstance(exc,ValidationError):
            errors=[{'path':'/'+'/'.join(map(str,e['loc'])),'message':e['msg'],'type':e['type']} for e in exc.errors(include_input=False,include_url=False)]
            print(json.dumps({'code':'ONTOLOGY_ARGUMENT_INVALID','issues':errors}),file=sys.stderr)
        else:print(str(exc),file=sys.stderr)
        sys.exit(1)
