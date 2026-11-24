"""Fact-based diagnostic groups in the existing control ledger; never a reviewer."""
import json
import re
import time
from .sources import digest
from .draft import draft_state
from .workflow_state import component_contract, operational, checkpoint_directory


def error_group(document, code, issue, tool):
    path = issue.get('contract_path') or issue.get('path', '')
    tokens = path.strip('/').split('/')
    component = []
    current = document
    mapping_id = None
    for token in tokens:
        if isinstance(current, list) and token.isdecimal():
            current = current[int(token)] if int(token)<len(current) else {}
            if isinstance(current, dict):
                identity = current.get('id') or (str(current.get('class_id',''))+':'+current['predicate'] if current.get('predicate') else current.get('class_id'))
                if identity is None:
                    identity = digest({'for_each': current.get('for_each'),
                        'nodes': sorted((n.get('id',''),n.get('class_id','')) for n in current.get('nodes',[])),
                        'predicates': sorted(t.get('predicate','') for t in current.get('triples',[]))})[:16]
                component.append(str(identity))
                if mapping_id is None and tokens[0]=='mappings': mapping_id=current.get('id')
        elif isinstance(current, dict):
            component.append(token);current=current.get(token)
        else:
            if not token.isdecimal():component.append(token)
    message = issue.get('message', '').lower()
    rule = next((name for name, pattern in [
        ('identity', r'identity|invalid iri'), ('datatype', r'datatype|literal|property kind'),
        ('cardinality', r'cardinality|count|too few'), ('coverage', r'unmapped|uncovered|source'),
        ('reference', r'undefined|reference'), ('json', r'json|container')]
        if re.search(pattern, message)), issue.get('type') or (tokens[-1] if tokens else 'operation'))
    key = {'code': code, 'rule': rule, 'component': component or [tool],
        'evidence': sorted(issue.get('evidence') or []), 'array_indices': issue.get('array_indices', []),
        'fact_id': issue.get('fact_id')}
    return digest(key), {**key, 'mapping_id': mapping_id, 'latest_path': path}


def observe(context_path, context, state, event):
    diagnostics = state.setdefault('diagnostics', {'groups': {}, 'validated': [], 'reads': [],
        'events': [], 'effective_advances': 0, 'last_notice_request': -1000})
    tool = event['tool']; result = event.get('result') or {}
    if not isinstance(result,dict):result={'code':'TOOL_ERROR','message':str(result)[:1500]}
    request = state['llmRequests']; repeated = 0; fact = None
    entry = {'at_monotonic': time.monotonic(), 'request': request, 'tool': tool}
    if event.get('is_error'):
        exact=event.get('exact_signature')
        if exact:
            counts=diagnostics.setdefault('exact_repeats',{})
            counts[exact]=counts.get(exact,0)+1
        document = draft_state(context_path, context)
        code=str(result.get('code') or 'TOOL_ERROR')
        stage=0 if 'JSON' in code or 'TRANSPORT' in code else 1 if 'ARGUMENT' in code else 4 if 'GRAPH_' in code else 3 if 'EXECUTION_' in code or 'SELECTION_' in code else 2 if 'CONTRACT_' in code else 0
        if stage>diagnostics.get('furthest_validation_stage',-1):
            diagnostics['furthest_validation_stage']=stage
            diagnostics['informational_advances']=diagnostics.get('informational_advances',0)+1
            diagnostics['stalled']=False;diagnostics['notice']=None
            entry['new_validation_stage']=stage
        issues = result.get('issues') or [{'message': str(result.get('message', 'tool failed'))}]
        for issue in issues[:8]:
            trial_id = issue.get('trial_id')
            if isinstance(trial_id, str) and re.fullmatch('[0-9a-f]{32}', trial_id):
                path = checkpoint_directory(context_path, context)/'trials'/trial_id/'contract.json'
                if path.exists():document=json.loads(path.read_text())
            signature, group = error_group(document, code, issue, tool)
            existing = diagnostics['groups'].setdefault(signature, {**group, 'count': 0, 'resolved': False})
            if existing.get('resolved'):existing['episode_count']=0
            existing.update(latest_path=group['latest_path'], resolved=False, trial_id=trial_id)
            existing['count']+=1
            existing['episode_count']=existing.get('episode_count',0)+1
            repeated=max(repeated, existing['episode_count'])
            fact={'error_group':signature, **existing}
            entry.update(kind='error', error_group=signature, novel=existing['count']==1,
                exact_signature=event.get('exact_signature'))
    elif tool=='ontology_execute' and result.get('status')=='valid':
        signature=result['content_fingerprint']
        repeated=1+sum(e.get('kind')=='repeat_trial' and e.get('fingerprint')==signature for e in diagnostics['events'])
        if signature not in diagnostics['validated']:
            diagnostics['validated'].append(signature);diagnostics['effective_advances']+=1
            diagnostics['stalled']=False
            diagnostics['notice']=None
            entry.update(kind='validated_trial', scope=result['scope'], fingerprint=signature)
            for group in diagnostics['groups'].values():
                if result['scope']=='full' or group.get('mapping_id') in result['mapping_ids']:group['resolved']=True
        else:
            entry.update(kind='repeat_trial', fingerprint=signature)
            fact={'repeated_trial':result['trial_id'], 'scope':result['scope'], 'mapping_ids':result['mapping_ids'],
                'meaning':'The same validated experiment was repeated without a new result.'}
    elif tool=='ontology_author' and result.get('status')=='valid' and result.get('scope')=='component':
        document=draft_state(context_path,context)
        signature=result.get('content_fingerprint') or digest(operational(component_contract(document,result['mapping_id']).model_dump(mode='json')))
        if signature not in diagnostics['validated']:
            diagnostics['validated'].append(signature);diagnostics['effective_advances']+=1;diagnostics['stalled']=False;diagnostics['notice']=None
        entry.update(kind='validated_component', mapping_id=result['mapping_id'])
    elif tool in ('ontology_evidence','graph_query','graph_explore'):
        signature=(result.get('cached_read') or {}).get('result_id') or digest(result)
        if (result.get('cached_read') or {}).get('result_id'):
            from .read_cache import directory
            if re.fullmatch('[0-9a-f]{64}',signature):
                path=directory(context_path,context)/(signature+'.json')
                if path.exists():
                    cached=json.loads(path.read_text())
                    signature=digest({k:cached[k] for k in ('tool','arguments','source_fingerprint','graph_bindings')})
        signature=digest({'role':context['role'],'read':signature})
        if signature in diagnostics['reads']:
            repeated=1+sum(e.get('kind')=='repeat_read' and e.get('fingerprint')==signature for e in diagnostics['events'])
            entry.update(kind='repeat_read',fingerprint=signature)
            fact={'repeated_read':signature,'meaning':'Previously returned evidence was read again; no new validated artifact was observed.'}
        else:
            diagnostics['reads'].append(signature);entry.update(kind='new_read',fingerprint=signature)
    elif result.get('saved'):
        entry.update(kind='saved_unvalidated',component_id=result.get('component_id'),evidence=result.get('evidence',[]))
    else:entry['kind']='saved_or_other'  # Writes/hashes alone are not effective advancement.
    policy=context.get('delivery',{})
    if policy.get('enabled',True) and fact and repeated>=policy.get('stagnation_repeats',3):
        diagnostics['stalled']=True
        if request-diagnostics['last_notice_request']>=policy.get('feedback_cooldown_requests',3):
            diagnostics['notice']={'id':digest({'fact':fact,'request':request}), 'observed':fact,
                'repeat_count':repeated,'effective_advances':diagnostics['effective_advances'],
                'recovery':'Saved draft and trial inspection remain available. Adjust the approach based on this result; no particular next tool is required.'}
            diagnostics['last_notice_request']=request
    diagnostics['events'].append(entry)
    diagnostics['events']=diagnostics['events'][-512:]
