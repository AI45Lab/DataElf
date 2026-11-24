"""Budget views and concise delivery feedback; no business or tool-order rules."""
import time


def allocate_session(config, role, deadline, usage, closing_deadline=None):
    from .errors import OntologyError
    limits=config.modeler if role=='modeler' else config.reviewer
    if config.execution_policy=='unbounded':
        return limits,{'session_deadline_monotonic':None,'request_ceiling':None,'delivery':config.delivery.model_dump(mode='json'),'closing_recovery':False,'execution_policy':'unbounded'}
    policy=config.delivery
    now=time.monotonic()
    protected_seconds=(policy.validation_reserve_seconds + (policy.reviewer_reserve_seconds if role=='modeler' else 0)) if policy.enabled else 0
    protected_requests=policy.reviewer_reserve_requests if policy.enabled and role=='modeler' else 0
    request_ceiling=max(0,config.max_llm_requests-protected_requests)
    available=deadline-now-protected_seconds
    if closing_deadline is not None:available=min(available,closing_deadline-now)
    available=min(available,limits.timeout_seconds)
    remaining_requests=min(request_ceiling-usage['llmRequests'],limits.max_requests-usage.get('roleMetrics',{}).get(role,{}).get('requests',0))
    minimum=policy.minimum_session_seconds if policy.enabled else 1
    if available<minimum or remaining_requests<1 or (config.max_llm_tokens is not None and
            (usage['usageUnknownRequests'] or usage['knownTokens']>=config.max_llm_tokens)):
        raise OntologyError('ONTOLOGY_DELIVERY_BUDGET_EXHAUSTED','control',[{
            'message':'Insufficient effective budget to start another work session while protecting final validation and review',
            'recoverable':False,'seconds_available':max(0,available),'requests_available':max(0,remaining_requests),
            'usage_unknown':bool(usage['usageUnknownRequests'])}])
    timeout=max(1,min(limits.timeout_seconds,int(available)))
    return limits.model_copy(update={'timeout_seconds':timeout}), {
        'session_deadline_monotonic':now+timeout,'request_ceiling':request_ceiling,
        'delivery':policy.model_dump(mode='json'),'closing_recovery':closing_deadline is not None}


def budget_view(state, context):
    role = state.get('roles', {}).get(context['role'], {})
    if context.get('execution_policy')=='unbounded':
        pending=sum(r.get('pendingResponses',0) for r in state.get('roles',{}).values())
        return {'execution_policy':'unbounded','phase':'observing','session_seconds_remaining':None,'global_seconds_remaining':None,'effective_seconds_remaining':None,'requests_remaining':None,'role_requests_remaining':None,'knownTokens':state['knownTokens'],'usageUnknownRequests':state['usageUnknownRequests'],'inflight_requests':pending,'token_limit':None,'single_response_output_limit':context.get('control_limits',{}).get('max_output_tokens'),'internal_interrupt_available':False,'task_limits':None}
    limits = context.get('control_limits', {})
    policy = context.get('delivery', {})
    now = time.monotonic()
    def remaining(key):
        deadline = context.get(key)
        return max(0, round(deadline-now, 2)) if deadline is not None else None
    global_time = remaining('deadline_monotonic')
    session_time = remaining('session_deadline_monotonic')
    times = [v for v in (global_time, session_time) if v is not None]
    effective = min(times) if times else None
    global_requests = max(0, context.get('max_llm_requests', 160)-state['llmRequests'])
    usable_requests = max(0, context.get('request_ceiling', context.get('max_llm_requests', 160))-state['llmRequests'])
    role_requests = max(0, limits['max_requests']-role.get('requests', 0)) if 'max_requests' in limits else None
    request_values = [v for v in (usable_requests, role_requests) if v is not None]
    token_limit = context.get('max_llm_tokens')
    token_left = max(0, token_limit-state['knownTokens']) if token_limit is not None else None
    pending=sum(r.get('pendingResponses',0) for r in state.get('roles',{}).values())
    pressure = ((effective is not None and effective <= policy.get('closing_reserve_seconds', 0))
        or min(request_values) <= policy.get('closing_reserve_requests', 0)
        or (token_left is not None and policy.get('closing_reserve_tokens') is not None
            and token_left <= policy['closing_reserve_tokens']))
    stalled = state.get('diagnostics', {}).get('stalled', False)
    limited = ((effective is not None and effective <= 2*policy.get('closing_reserve_seconds', 0))
        or min(request_values) <= 2*policy.get('closing_reserve_requests', 0))
    closing = policy.get('enabled', False) and (context.get('closing_recovery', False) or pressure or (stalled and limited))
    return {'session_seconds_remaining': session_time, 'global_seconds_remaining': global_time,
        'effective_seconds_remaining': effective, 'seconds_remaining': effective,
        'request_limit': context.get('max_llm_requests', 160), 'requests_remaining': global_requests,
        'usable_requests_remaining': usable_requests, 'role_requests_remaining': role_requests,
        'knownTokens': state['knownTokens'], 'token_limit': token_limit, 'known_tokens_remaining': token_left,
        'usageUnknownRequests': state['usageUnknownRequests'], 'inflight_requests':pending,
        'token_usage_complete': state['usageUnknownRequests']==0 and pending==0,
        'token_budget_precision': 'observed usage only; unknown requests prevent an exact guarantee',
        'single_response_output_limit': limits.get('max_output_tokens'),
        'phase': 'closing' if closing else 'normal',
        'closing_interrupts': role.get('closingInterrupts', 0),
        'closing_reserve_seconds': policy.get('closing_reserve_seconds', 0),
        'autonomous_seconds_remaining': max(0,round(effective-policy.get('closing_reserve_seconds',0),2)) if effective is not None else None,
        'delivery_reserved_requests': min(min(request_values),policy.get('closing_reserve_requests',0)),
        'autonomous_requests_remaining': max(0,min(request_values)-policy.get('closing_reserve_requests',0)),
        'usage_states': {'settled_tokens':state['knownTokens'],'inflight_requests':pending,
            'interrupted_unknown_requests':state.get('usageInterruptedRequests',0),
            'completed_without_usage_requests':state.get('usageMissingResponses',0),
            'legacy_unclassified_unknown_requests':max(0,state['usageUnknownRequests']-state.get('usageInterruptedRequests',0)-state.get('usageMissingResponses',0)),
            'estimated_tokens':None},
        'stream_grace_seconds': policy.get('stream_grace_seconds', 0),
        'internal_interrupt_available': policy.get('enabled', False) and not context.get('closing_recovery', False)
            and role.get('closingInterrupts', 0)<policy.get('max_closing_interrupts', 0)}


def budget_message(budget, artifacts=None, feedback=None):
    import json
    value = {'budget': budget}
    if artifacts is not None:
        value['artifacts'] = artifacts
        value['delivery'] = ('Prioritize decisive gaps for a complete evidence-backed submission. '
            'Final validation and independent review remain required. If blocked, retain the draft/trial and state the unresolved blocker; partial work is not publication.')
    if feedback: value['observed_stagnation'] = feedback
    return 'ONTOLOGY_RUNTIME_STATUS\n'+json.dumps(value, ensure_ascii=False, separators=(',', ':'))
