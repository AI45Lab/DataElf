"""Reference submit schemas use the same strict review models and ledger."""
from copy import deepcopy


def reference_schema(schema,*,checks=True):
    schema=deepcopy(schema)
    for source,target in [('checks','check_ids'),('issues','issue_ids'),('issue_updates','issue_update_ids')]:
        schema['properties'][target]={'type':'array','items':{'type':'string','pattern':'^check-[0-9a-f]{64}$'},'uniqueItems':True}
    if checks and 'checks' in schema.get('required',[]):
        schema['required'].remove('checks')
        # Both representations are supported; an empty refs list cannot replace checks.
        schema['anyOf']=[{'required':['checks']},{'required':['check_ids'],'properties':{'check_ids':{'minItems':1}}}]
    return schema
