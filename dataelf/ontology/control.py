"""Cross-session usage budgets. Prose and exploratory reads are permitted."""
import json
import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from .errors import OntologyError
from .sources import write_json


def control_path(context_path,context):return Path(context.get('control_path') or context_path.with_name('control.json'))

@contextmanager
def locked_control(context_path,context):
    path=control_path(context_path,context);path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=json.loads(path.read_text()) if path.exists() else {'agentSessions':0,'llmRequests':0,'knownTokens':0,'usageUnknownRequests':0,'roles':{}}
        try:yield state
        finally:write_json(path,state);fcntl.flock(lock,fcntl.LOCK_UN)

def failure(code,message,recoverable=False):
    raise OntologyError(code,'control',[{'message':message,'recoverable':recoverable,'required_change':'Inspect the retained artifact state. Resume executable components if present; if the draft is empty, save or trial a small evidence-backed component rather than repeating the design in prose.'}])

def unknown_usage(state,role,cause,count=1):
    state['usageUnknownRequests']+=count
    role['usageUnknownRequests']=role.get('usageUnknownRequests',0)+count
    state[cause]=state.get(cause,0)+count
    role[cause]=role.get(cause,0)+count


def start_session(context_path,context):
    with locked_control(context_path,context) as state:
        state['agentSessions']+=1
        r=state['roles'].setdefault(context['role'],{'requests':0,'completed':False});r['completed']=False
        r['sessions']=r.get('sessions',0)+1
        r.setdefault('started_monotonic',time.monotonic())
        r['sessionResponsesAtStart']=r.get('responses',0)
        if context.get('delivery',{}).get('enabled') or context.get('execution_policy')=='unbounded':
            from .delivery import budget_view
            state.setdefault('delivery_events',[]).append({'kind':'session_started','role':context['role'],
                'at_monotonic':time.monotonic(),'budget':budget_view(state,context)})

def control_event(context_path,payload):
    context=json.loads(context_path.read_text());role=context['role'];limits=context['control_limits']
    with locked_control(context_path,context) as state:
        r=state['roles'].setdefault(role,{'requests':0,'completed':False});event=payload['event']
        if event=='request' and context.get('execution_policy')=='unbounded':
            state['llmRequests']+=1;r['requests']+=1;r['pendingResponses']=r.get('pendingResponses',0)+1
        elif event=='request':
            from .delivery import budget_view
            before=budget_view(state,context)
            if before['effective_seconds_remaining'] is not None and before['effective_seconds_remaining']<=0:
                failure('ONTOLOGY_GLOBAL_BUDGET_EXHAUSTED' if before['global_seconds_remaining']==0 else 'ONTOLOGY_SESSION_BUDGET_EXHAUSTED','Effective execution deadline exhausted')
            if state['llmRequests']>=context.get('request_ceiling',context.get('max_llm_requests',160)) or r['requests']>=limits['max_requests']:failure('ONTOLOGY_REQUEST_BUDGET_EXHAUSTED','Provider request budget exhausted; reviewer reservation remains protected')
            if context.get('max_llm_tokens') is not None and state['usageUnknownRequests']:failure('ONTOLOGY_USAGE_UNAVAILABLE','Unknown request usage prevents an exact token-budget guarantee')
            if context.get('max_llm_tokens') is not None and state['knownTokens']>=context['max_llm_tokens']:failure('ONTOLOGY_TOKEN_BUDGET_EXHAUSTED','Observed token budget exhausted')
            state['llmRequests']+=1;r['requests']+=1
            r['pendingResponses']=r.get('pendingResponses',0)+1
        elif event=='tool' and payload.get('submitted'):
            r['completed']=True
            diagnostics=state.get('diagnostics',{})
            diagnostics['stalled']=False;diagnostics['notice']=None
            if role in ('modeler','stage1_generator'):
                for group in diagnostics.get('groups',{}).values():group['resolved']=True
        elif event=='response':
            r['pendingResponses']=max(0,r.get('pendingResponses',0)-1)
            r['responses']=r.get('responses',0)+1
            usage=payload.get('usage') or {};tokens=usage.get('totalTokens') or sum(usage.get(k,0) or 0 for k in ('input','output','cacheRead','cacheWrite'))
            if tokens:
                state['knownTokens']+=tokens;r['knownTokens']=r.get('knownTokens',0)+tokens
            else:
                unknown_usage(state,r,'usageInterruptedRequests' if payload.get('stop_reason')=='aborted' else 'usageMissingResponses')
                if context.get('max_llm_tokens') is not None:failure('ONTOLOGY_USAGE_UNAVAILABLE','Provider did not report usage for the requested token budget')
            if context.get('max_llm_tokens') is not None and state['knownTokens']>context['max_llm_tokens']:failure('ONTOLOGY_TOKEN_BUDGET_EXHAUSTED','Observed token budget exhausted')
            if not r['completed'] and payload.get('stop_reason')=='length':failure('ONTOLOGY_OUTPUT_TRUNCATED','Provider output was truncated; checkpoints retained',True)
            if payload.get('transport_markup'):failure('ONTOLOGY_TOOL_TRANSPORT_INVALID','Provider returned tool markup instead of a tool call',True)
        elif event=='capability':failure('ONTOLOGY_TOOL_CHOICE_UNSUPPORTED','Endpoint rejected the configured optional tool choice mode')
        elif event=='observe':
            from .feedback import observe
            observe(context_path,context,state,payload)
        elif event=='closing_interrupt':
            from .delivery import budget_view
            if r['completed'] or not budget_view(state,context)['internal_interrupt_available']:
                return {'interrupt':False}
            r['closingInterrupts']=r.get('closingInterrupts',0)+1
            return {'interrupt':True}
        elif event=='feedback_delivery':
            # Internal suppression flag also accepts the legacy event name. A
            # provider hook proves preparation; only wire capture proves sending.
            if payload['kind'] in ('first_artifact_request_prepared','first_artifact_delivered'):r['first_artifact_delivered']=True
            state.setdefault('delivery_events',[]).append({'kind':payload['kind'],'role':role,
                'request':state['llmRequests'],'at_monotonic':time.monotonic(),'notice_id':payload.get('notice_id')})
        elif event in ('budget','first_artifact_feedback'):pass
        from .delivery import budget_view
        budget=budget_view(state,context)
        changed=r.get('lastBudgetPhase')!=budget['phase']
        r['lastBudgetPhase']=budget['phase']
        from .first_artifact import feedback as first_feedback
        facts,first_notice,boundary=first_feedback(context_path,context,state,r,event)
        return {'first_artifact':facts,'first_artifact_feedback':first_notice,
            'component_boundary':boundary,'requests':r['requests'],'completed':r['completed'],'budget':budget,'phase_changed':changed,
            'feedback':state.get('diagnostics',{}).get('notice')}


def record_duration(context_path,context,seconds):
    with locked_control(context_path,context) as state:
        r=state['roles'][context['role']];r['elapsedSeconds']=round(r.get('elapsedSeconds',0)+seconds,3)


def record_interruption(context_path,context):
    """A killed in-flight request has unknown usage, not zero tokens."""
    with locked_control(context_path,context) as state:
        r=state['roles'].get(context['role'])
        if r is None:return  # No provider request was ever registered for this role.
        pending=r.get('pendingResponses',0)
        messages=[]
        events=context_path.with_name('events.jsonl')
        if events.exists():
            for line in events.read_text().splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if not isinstance(event,dict):continue
                if event.get('type')=='message_end' and event.get('message',{}).get('role')=='assistant':messages.append(event['message'])
        accounted=r.get('responses',0)-r.get('sessionResponsesAtStart',0)
        unrecorded=messages[accounted:accounted+pending]
        interrupted=pending-len(unrecorded);missing=0
        for message in unrecorded:
            usage=message.get('usage') or {}
            tokens=usage.get('totalTokens') or sum(usage.get(k,0) or 0 for k in ('input','output','cacheRead','cacheWrite'))
            if tokens:
                state['knownTokens']+=tokens;r['knownTokens']=r.get('knownTokens',0)+tokens
            else:
                if message.get('stopReason')=='aborted':interrupted+=1
                else:missing+=1
        unknown_usage(state,r,'usageInterruptedRequests',interrupted)
        unknown_usage(state,r,'usageMissingResponses',missing)
        r['responses']=r.get('responses',0)+pending
        r['pendingResponses']=0

def metrics(path):
    if not Path(path).exists():return {'agentSessions':0,'llmRequests':0,'knownTokens':0,'usageUnknownRequests':0}
    state=json.loads(Path(path).read_text());return {**{k:state[k] for k in ('agentSessions','llmRequests','knownTokens','usageUnknownRequests')},**{k:state.get(k,0) for k in ('usageInterruptedRequests','usageMissingResponses')},'roleMetrics':state['roles']}


def budget_status(context_path,context):
    usage=metrics(control_path(context_path,context))
    from .delivery import budget_view
    path=control_path(context_path,context)
    full=json.loads(path.read_text()) if path.exists() else {**usage,'roles':usage.get('roleMetrics',{})}
    return {**usage,**budget_view(full,context)}
