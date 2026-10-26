"""Explicit local handoff, author responses and reviewer-owned check records."""
import json
from pathlib import Path
import jsonschema
from .capabilities import skill_review
from .collaboration import ReviewLedger
from .graph.store import write_json
from .contracts import ReviewIssue
from .tool_results import control_event

DESCRIPTIONS = {
    'review_request': '请求对应 Reviewer 对准确草稿、候选或报告子集给局部反馈。保存只读快照并交接控制权，不要求完整交付，不发布。给具体问题和证据。',
    'review_feedback_submit': '局部模式保存回答、检查和具体问题并交回原作者；没有整体 approve 权限。',
    'review_respond': '作者对问题采纳、提供反证或延后可选建议，保存说明、证据及变更引用。不能自行关闭 Reviewer 问题。',
    'review_checks_save': 'Reviewer 保存独立检查的方法、实际结果、结论、覆盖和准确文件依赖。不把工具运行成功自动标成验收通过。',
    'review_issue_save':'Reviewer 增量保存一个有证据的问题；绑定当前审核对象和依赖，不代表正式 verdict。',
    'review_issue_update_save':'Reviewer 保存既有问题的复核状态及证据；作者无权关闭问题，正式提交时引用记录 ID。',
    'review_inspect': '分页读取本任务审核请求、全部未解决问题、作者回应或 Reviewer 检查索引。历史问题不因恢复而丢失。',
}


def family(context):
    return context.get('review_family') or ('graph' if 'request' in context else 'analysis')


def schemas(context):
    def obj(props, required=()):
        return {'type':'object','properties':props,'required':list(required),'additionalProperties':False}
    text={'type':'string','minLength':1};texts={'type':'array','items':text}
    integer={'type':'integer','minimum':0}
    ref=obj({'graph_id':text,'workspace_id':text,'revision_id':text},['graph_id'])
    result={'review_inspect':obj({'resource':{'type':'string','enum':['requests','issues','responses','checks','record']},
        'family':{'type':'string','enum':['graph','analysis']},
        'collection':{'type':'string','enum':['checks','requests','issues','responses']},'record_id':text,
        'page_offset':integer,'max_bytes':{'type':'integer','minimum':1024,'maximum':16000},'offset':integer,
        'limit':{'type':'integer','minimum':1,'maximum':100}},['resource'])}
    result['review_inspect']['allOf']=[{'if':{'properties':{'resource':{'const':'record'}},'required':['resource']},
        'then':{'required':['collection','record_id']}}]
    if not context['role'].endswith('reviewer'):
        kinds=['draft','candidate'] if 'request' in context else ['report']+(['candidate'] if context.get('graph_root') else [])
        result['review_request']=obj({'object_kind':{'type':'string','enum':kinds},'question':text,
            'evidence':texts,'idempotency_key':text,'paths':texts,'graph_ref':ref},
            ['object_kind','question','evidence','idempotency_key'])
        result['review_respond']=obj({'family':{'type':'string','enum':['graph','analysis']},'issue_id':text,
            'response':{'type':'string','enum':['accept','counterevidence','defer_suggestion']},
            'explanation':text,'evidence':texts,'change_refs':texts},['issue_id','response','explanation','evidence'])
    else:
        from dataelf.discovery.analysis_review.contracts import Issue,IssueUpdate
        from .contracts import ReviewUpdate
        saved_issue=(ReviewIssue if family(context)=='graph' else Issue).model_json_schema()
        saved_issue['required']=list(dict.fromkeys([*saved_issue.get('required',[]),'issue_id','location','impact','nature','blocking']))
        for field in ['issue_id','location','impact']:saved_issue['properties'][field]=text
        result['review_issue_save']=obj({'issue':saved_issue,
            'dependencies':{'type':'array','items':text,'minItems':1}},['issue','dependencies'])
        result['review_issue_update_save']=obj({'update':(ReviewUpdate if family(context)=='graph' else IssueUpdate).model_json_schema(),
            'dependencies':{'type':'array','items':text,'minItems':1}},['update','dependencies'])
        result['review_checks_save']=obj({'method':text,'result':{},'conclusion':text,'coverage':text,
            'dependencies':{'type':'array','items':text,'minItems':1}},['method','result','conclusion','coverage','dependencies'])
        if context.get('review_mode')=='local':
            from dataelf.discovery.analysis_review.contracts import Issue
            issue_schema=(ReviewIssue if family(context)=='graph' else Issue).model_json_schema()
            # Both issue models are flat; inline fields avoid tool root unions.
            issue_schema['required']=list(set(issue_schema.get('required',[]))|{'issue_id','location','impact','nature','blocking'})
            for field in ['issue_id','location','impact']:issue_schema['properties'][field]=text
            update=obj({'issue_id':text,'status':{'type':'string','enum':['resolved','unresolved','regressed','withdrawn']},
                        'evidence':{'type':'array','items':text,'minItems':1},'explanation':text},['issue_id','status','evidence','explanation'])
            result['review_feedback_submit']=obj({'request_id':text,'answer':text,'issues':{'type':'array','items':issue_schema},
                'checks':texts,'issue_updates':{'type':'array','items':update}},['request_id','answer','issues','checks'])
    if 'review_feedback_submit' in result:
        local=result['review_feedback_submit']
        for name in ['check_ids','issue_ids','issue_update_ids']:
            local['properties'][name]={'type':'array','items':{'type':'string','pattern':'^check-[0-9a-f]{64}$'},'uniqueItems':True}
        local['required']=[n for n in local['required'] if n not in {'checks','issues'}]
        local['allOf']=[{'anyOf':[{'required':['checks']},{'required':['check_ids']}]},
            {'anyOf':[{'required':['issues']},{'required':['issue_ids']}]}]
    return result


def dispatch(context_path,name,args):
    context_path=Path(context_path);context=json.loads(context_path.read_text())
    if not skill_review(context):raise ValueError('Review collaboration is not enabled')
    context['_context_path']=str(context_path)
    permitted=schemas(context)
    if name not in permitted:raise ValueError('Review tool is not permitted for this role/mode')
    jsonschema.Draft202012Validator(permitted[name]).validate(args)
    workspace=Path(context.get('workspace') or context['request']['workspace'])
    requested_family=args.get('family',family(context))
    if requested_family!=family(context) and (context['role'].endswith('reviewer') or 'request' in context or not context.get('graph_root')):
        raise ValueError('Review family is outside role permissions')
    ledger=ReviewLedger(workspace,requested_family)
    if name=='review_request':
        desired='analysis' if args['object_kind']=='report' else 'graph'
        ledger=ReviewLedger(workspace,desired)
        request=ledger.request(context,**args)
        if request['status'] in {'feedback_saved','author_scheduled','delivered'}:
            return {'request_id':request['request_id'],'feedback':request['feedback'],'publication_eligible':False}
        marker={'kind':'local_review','family':desired,'request_id':request['request_id']}
        write_json(context_path.with_name('handoff.json'),marker)
        return {'control_event':control_event(marker['kind'],marker['request_id']),'handoff':marker,'publication_eligible':False}
    if name=='review_respond':
        value={k:v for k,v in args.items() if k!='family'}
        issue=ledger.issues().get(args['issue_id'],{}).get('issue',{})
        if args['response']=='defer_suggestion' and issue.get('nature')!='suggestion':
            raise ValueError('Confirmed errors and required questions cannot be deferred as suggestions')
        return {'saved_response':ledger.respond(**value),'issue_status_unchanged':True}
    if name in {'review_issue_save','review_issue_update_save'}:
        kind='issue' if name=='review_issue_save' else 'issue_update'
        value=args['issue' if kind=='issue' else 'update']
        if kind=='issue_update' and value['issue_id'] not in ledger.issues():raise ValueError('Unknown prior issue')
        return ledger.save_record(context,kind,value,args['dependencies'])
    if name=='review_checks_save':return ledger.save_check(context,**args)
    if name=='review_feedback_submit':
        args=dict(args)
        args['checks']=[*args.get('checks',[]),*args.pop('check_ids',[])]
        if len(args['checks'])!=len(set(args['checks'])):raise ValueError('Duplicate reviewer check references')
        args=ledger.assemble(context,args)
        args.setdefault('issues',[])
        if args['request_id']!=context.get('review_request_id'):raise ValueError('Feedback request binding mismatch')
        request=ledger.load(args['request_id']);ledger.verify(request)
        if request['object_hash']!=context.get('review_object_hash'):raise ValueError('Feedback object hash mismatch')
        if request.get('feedback') is not None:
            if request['feedback']!=args:raise ValueError('Saved feedback is immutable; use a new request for new evidence')
            return {'control_event':control_event('local_feedback',args['request_id']),'saved':True,'handoff':{'kind':'local_feedback','family':requested_family,'request_id':args['request_id']},'publication_eligible':False}
        ledger.validate_checks(context,args['checks'])
        previous=ledger.prior();prior={i['issue_id'] for i in previous['issues'] if i.get('blocking')}
        updates={u['issue_id']:u for u in args.get('issue_updates',[])}
        if len(updates)!=len(args.get('issue_updates',[])) or not prior<=set(updates):
            raise ValueError('Every unresolved blocking issue needs an evidenced update')
        from .review_policy import validate_issues
        from types import SimpleNamespace
        issue_type=ReviewIssue
        if requested_family=='analysis':
            from dataelf.discovery.analysis_review.contracts import Issue
            issue_type=Issue
        issues=[issue_type.model_validate(i) for i in args['issues']]
        validate_issues(SimpleNamespace(issues=issues,verdict='local_feedback'))
        current={i.issue_id for i in issues if i.blocking}
        for ident in prior:
            if (updates[ident]['status'] in {'unresolved','regressed'}) != (ident in current):
                raise ValueError('Issue status and current blocking list disagree')
        ledger.record_review(args,request_id=args['request_id'])
        write_json(context_path.with_name('local_feedback.json'),args)
        marker={'kind':'local_feedback','family':requested_family,'request_id':args['request_id']}
        write_json(context_path.with_name('handoff.json'),marker)
        return {'control_event':control_event(marker['kind'],marker['request_id']),'handoff':marker,'saved':True,'publication_eligible':False}
    resource=args['resource']
    collection=args.get('collection') if resource=='record' else resource
    if collection not in {'checks','requests','issues','responses'}:raise ValueError('Record reads require a collection')
    rows={'requests':ledger.requests,'issues':lambda:list(ledger.issues().values()),
          'responses':ledger.responses,'checks':ledger.checks}[collection]()
    def identity(row):
        from .sources import digest
        return row.get('check_id') or row.get('request_id') or (row.get('issue') or {}).get('issue_id') or digest(row)
    budget=args.get('max_bytes',8000)
    if resource=='record':
        row=next((r for r in rows if identity(r)==args.get('record_id')),None)
        if row is None:raise ValueError('Unknown review record')
        # No arbitrary filesystem path input; role/family visibility remains enforced.
        from .resources import json_page
        amount=max(32,budget//3)
        while True:
            page=json_page(row,args.get('page_offset',0),amount)
            result={**page,'record_id':args['record_id'],'pagination_unit':'utf8_json_bytes',
                'full_data_location':str(ledger.root),
                'next_request':{'tool':name,'arguments':{**args,'page_offset':page['next_offset']}} if page['next_offset'] is not None else None}
            if len(json.dumps(result,ensure_ascii=False).encode())<=budget:return result
            if amount<=4:raise ValueError('max_bytes cannot hold page metadata')
            amount//=2
    indexes=[]
    for row in rows:
        if collection=='checks':item=ledger.check_index(row)
        else:
            item={'record_id':identity(row),'status':row.get('status'),
                'summary':str(row.get('question') or (row.get('issue') or {}).get('message') or row.get('explanation',''))[:200],
                'next_request':{'tool':name,'arguments':{'resource':'record','collection':collection,'record_id':identity(row),'family':requested_family}}}
        if collection=='checks':item['next_request']['arguments']['family']=requested_family
        indexes.append(item)
    start=args.get('offset',0);end=start;items=[]
    while end<len(indexes) and len(items)<args.get('limit',20):
        if len(json.dumps(items+[indexes[end]],ensure_ascii=False).encode())>budget-700:break
        items.append(indexes[end]);end+=1
    if end==start and start<len(indexes):raise ValueError('Increase max_bytes to hold one index entry')
    return {'items':items,'total':len(rows),'pagination_unit':'record_index','returned_bytes':len(json.dumps(items,ensure_ascii=False).encode()),
        'next_request':{'tool':name,'arguments':{**args,'offset':end}} if end<len(rows) else None}
