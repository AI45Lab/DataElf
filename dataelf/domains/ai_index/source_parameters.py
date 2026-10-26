"""On-demand OpenAPI parameter schemas, derived from the packaged API reference.

Only the Skill flow uses this definition; legacy domain clients remain unchanged.
"""
def command_schema(command):
    from .evidence import COMMANDS
    if command not in COMMANDS:raise ValueError('Unknown AI Index command')
    string={'type':'string'};strings={'type':'array','items':string}
    page={'page':{'type':'integer','minimum':1,'description':'API page number, starting at 1'},
          'size':{'type':'integer','minimum':1,'maximum':20 if command=='huggingface-search' else 50}}
    common={k:strings for k in ['institutions','domains','sub_domains','journal_name','conferences','award_list','country_code']}
    ecosystem={k:strings for k in ['institution_ids','institutions','domains','persons','products']}
    ecosystem.update({k:string for k in ['date_start','date_end','filter_type']})
    fields={}
    if command=='scholar-search':fields={'name':string,**common}
    elif command=='paper-search':fields={'title':string,**{k:v for k,v in common.items() if k!='country_code'},
        'min_publish_date':string,'min_citation_count':{'type':'integer','minimum':0},
        'sort_type':{'enum':['heat','citation_count','publish_time'],'type':'string'}}
    elif command=='institution-search':fields={'keyword':string,**common,'has_funding':{'type':'string','enum':['has_funding','no_funding']},
        'sort_type':{'type':'string','enum':['index','paperCount','scholarCount','funding']}}
    elif command=='institution-funding':
        return {'type':'object','properties':{'institution_id':{'type':'string','minLength':1}},'required':['institution_id'],'additionalProperties':False}
    else:
        fields={**ecosystem}
        if command=='news-search':fields.update({'news_ids':strings,'title':string,'person':string})
        else:fields['sort_type']={'type':'string','enum':['publish_time','view'] if command in ['twitter-search','youtube-search'] else ['heat','time']}
        if command=='huggingface-search':fields['asset_type']={'type':'string','enum':['all','model','dataset','space']}
    return {'type':'object','properties':{**fields,**page},'additionalProperties':False,
        'description':'Command parameter contract; dates are API date strings, page is API pagination, not saved-file byte offsets. Refer to references/api-reference.md for response field semantics.'}
