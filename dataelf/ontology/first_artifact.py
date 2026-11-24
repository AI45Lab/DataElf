"""Facts about saving and validation are deliberately separate."""
import time
from .draft import draft_state
from .workflow_state import checkpoint_directory, load_state
from .sources import digest


def observe_status(context_path,context,state):
    draft=draft_state(context_path,context)
    components={k:list(draft.get(k) or {}) if k!='mappings' else [m['id'] for m in draft.get(k,[])]
        for k in ('classes','properties','mappings')}
    saved=any(components.values())
    diagnostics=state.get('diagnostics',{})
    trials=checkpoint_directory(context_path,context)/'trials'
    import json
    local=any(json.loads(p.read_text()).get('scope')=='component' for p in trials.glob('*/result.json'))
    role=state.get('roles',{}).get(context['role'],{})
    elapsed=max(0,time.monotonic()-role.get('started_monotonic',time.monotonic()))
    notes=load_state(context_path,context).get('component_notes',{})
    return {'has_saved_components':saved,'saved_components':components,
        'evidence_linked_components':[k for k,v in notes.items() if v.get('evidence')],
        'saving_is_validation':False,'local_execution_attempted':local,
        'validated_progress':diagnostics.get('effective_advances',0)>0,
        'informational_validation_advances':diagnostics.get('informational_advances',0),
        'successful_reads_observed':bool(diagnostics.get('reads') or (context.get('recovery_state') or {}).get('confirmed_reads')),
        'elapsed_seconds':round(elapsed,2),'requests':role.get('requests',0),
        'known_tokens':state.get('knownTokens',0),'usage_unknown_requests':state.get('usageUnknownRequests',0)}


def feedback(context_path,context,state,role,event):
    facts=observe_status(context_path,context,state)
    policy=context.get('delivery',{})
    due=(context['role'] in ('modeler','stage1_generator') and policy.get('enabled')
        and facts['successful_reads_observed'] and not facts['has_saved_components']
        and facts['elapsed_seconds']>=policy.get('first_artifact_notice_seconds',180))
    notice=None
    if due and event in ('request','first_artifact_feedback') and not role.get('first_artifact_notice'):
        notice={'id':digest({'kind':'no_saved_component','session':str(context_path)}),
            'observed':facts,'message':'Evidence reads are recorded, but no component has been saved. Save an evidence-backed provisional component that can be completed later, or identify the concrete evidence or interface gap preventing a save. A read does not establish sufficient evidence; saving does not establish validity.',
            'entry_points':['ontology_author (if advertised)','ontology_draft(action=patch)']}
        role['first_artifact_notice']=notice
    notice=role.get('first_artifact_notice') if due and event in ('request','first_artifact_feedback') and not role.get('first_artifact_delivered') else None
    boundary=False
    if due and event=='request' and policy.get('component_delivery_boundary') and not role.get('component_boundary_attempted'):
        if not context.get('control_limits',{}).get('component_tools',True):
            raise ValueError('Experimental component delivery requires the advertised typed author interface')
        role['component_boundary_attempted']=True;boundary=True
        state.setdefault('delivery_events',[]).append({'kind':'component_boundary_requested','role':context['role'],
            'request':state['llmRequests'],'at_monotonic':time.monotonic()})
    return facts,notice,boundary
