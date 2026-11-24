"""Versioned migration for persisted recovery; never infer executed work from prose."""
VERSION='dataelf.recovery.v1'


def normalize_recovery(value):
    value=dict(value or {})
    version=value.get('schema_version')
    if version not in (None,VERSION):raise ValueError('Unsupported recovery schema: '+str(version))
    if 'completed_tool_results' in value:
        migrated=value.pop('completed_tool_results')
        value.setdefault('confirmed_reads',migrated)
        value['migrated_from']='legacy.completed_tool_results'
    value['schema_version']=VERSION
    return value


def merge_recovery(previous,current):
    old=normalize_recovery(previous);new=normalize_recovery(current)
    rows={r.get('result_id') or str(r):r for r in old.get('confirmed_reads',[])}
    rows.update({r.get('result_id') or str(r):r for r in new.get('confirmed_reads',[])})
    new['confirmed_reads']=list(rows.values())[-12:]
    return new


def review_summary(value, family='analysis'):
    """Prompt index only. Full opinions/checks remain in their bound storage."""
    if not value:return value
    result={k:value[k] for k in ['verdict','task_assessment','submission_id','submission_hash','request_id'] if k in value}
    for key in ['summary','answer']:
        if key in value:result[key+'_excerpt']=value[key][:800]
    issues=value.get('issues',[])
    result.update(issue_count=len(issues),check_count=len(value.get('checks',[])),
        issues=[{**{k:i[k] for k in ['issue_id','severity','blocking','nature','target','location'] if k in i},
            'message_excerpt':i.get('message','')[:240],
            'read':{'tool':'review_inspect','arguments':{'resource':'record','collection':'issues','record_id':i['issue_id'],'family':family}}}
            for i in issues[:5] if i.get('issue_id')])
    result['read']=({'tool':'analysis_inspect','arguments':{'resource':'previous_review'}} if family=='analysis' else
        {'tool':'review_inspect','arguments':{'resource':'issues','family':'graph'}})
    if value.get('request_id'):
        result['read']={'tool':'review_inspect','arguments':{'resource':'record','collection':'requests','record_id':value['request_id'],'family':family}}
    return result
