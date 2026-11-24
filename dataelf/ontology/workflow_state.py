"""Explicit authoring checkpoints and component validation, independent of business vocabulary."""
from copy import deepcopy
import json
from pathlib import Path
from contextlib import contextmanager
import fcntl
from .sources import digest, write_json
from .errors import OntologyError


def checkpoint_directory(context_path, context):
    return Path(context.get('checkpoint_directory') or context_path.parent)


@contextmanager
def checkpoint_lock(context_path, context):
    directory=checkpoint_directory(context_path,context)
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/'checkpoint.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:yield directory
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def load_state(context_path,context):
    path=checkpoint_directory(context_path,context)/'workflow.json'
    if path.exists():return json.loads(path.read_text())
    return {'step':'inventory','items':{},'current_item':None,'evidence':[],
        'progress':0,'progress_marks':[],'global_hash':None,'issues':[],'feedback_hash':None}


def save_state(context_path,context,state):
    write_json(checkpoint_directory(context_path,context)/'workflow.json',state)


def operational(value):
    if isinstance(value,dict):return {k:operational(v) for k,v in value.items()
        if k not in {'label','comment','description','annotations','evidence'}}
    if isinstance(value,list):return [operational(v) for v in value]
    return value


def mark_progress(state,kind,key):
    mark=kind+':'+key
    if mark in state['progress_marks']:return False
    state['progress_marks'].append(mark);state['progress']+=1
    return True


def status(state):
    current=state['items'].get(state['current_item'])
    return {k:deepcopy(state[k]) for k in ('current_item','progress','global_hash')} | {'advisory':True,
        'issues':deepcopy(state['issues'][:8]),'current_details':deepcopy(current),
        'item_count':len(state['items']),'validated_items':[k for k,v in state['items'].items() if v.get('validated')],
        'pending_items':[k for k,v in state['items'].items() if not v.get('validated')]}


def select_next(state):
    pending=[k for k,v in state['items'].items() if not v.get('validated')]
    state['current_item']=pending[0] if pending else None
    state['step']=('build' if state['items'][pending[0]].get('researched') else 'research') if pending else 'global_validate'


def register_item(state,payload,catalog):
    if payload['source'] not in {s.source_id for s in catalog.sources}:
        raise ValueError('work item references an unknown source')
    ident=payload['id']
    if ident not in state['items'] and len(state['items'])>=64:raise ValueError('at most 64 modeling items are allowed')
    existing=state['items'].get(ident,{})
    if existing and existing['source']!=payload['source']:raise ValueError('a work item cannot change its source')
    mark_progress(state,'inventory',payload['source'])
    state['items'][ident]={'source':payload['source'],'goal':payload['goal'],
        'evidence':payload.get('evidence',[]),'researched':existing.get('researched',False),'validated':existing.get('validated',False)}
    if state['step']=='inventory' or state['current_item'] is None:select_next(state)


def edited(state,before,after):
    if operational(before)==operational(after):return
    state['global_hash']=None
    for section in ('classes','properties'):
        for key in after.get(section,{}):
            if key not in before.get(section,{}):mark_progress(state,'component',section+'/'+key)
    prior={m['id']:m for m in before.get('mappings',[])}
    for mapping in after.get('mappings',[]):
        if operational(prior.get(mapping['id']))!=operational(mapping) and mapping['id'] in state['items']:
            state['items'][mapping['id']]['validated']=False
    select_next(state)


def component_contract(document,mapping_id):
    from .contracts import OntologyContract
    mapping=next((m for m in document.get('mappings',[]) if m['id']==mapping_id),None)
    if mapping is None:raise ValueError('mapping does not exist: '+mapping_id)
    classes=set();properties=set()
    def visit(group):
        classes.update(n['class_id'] for n in group.get('nodes',[]))
        properties.update(t['predicate'] for t in group.get('triples',[]) if not t['predicate'].startswith(('rdf:','rdfs:')))
        for child in group.get('children',[]):visit(child)
    visit(mapping)
    for key in properties:
        prop=document.get('properties',{}).get(key,{})
        if prop.get('domain'):classes.add(prop['domain'])
        if prop.get('kind')=='iri' and prop.get('range'):classes.add(prop['range'])
    pending=list(classes)
    while pending:
        parent=document.get('classes',{}).get(pending.pop(),{}).get('subClassOf')
        if parent and parent not in classes:classes.add(parent);pending.append(parent)
    missing_classes=classes-set(document.get('classes',{}));missing_properties=properties-set(document.get('properties',{}))
    if missing_classes or missing_properties:
        raise OntologyError('ONTOLOGY_COMPONENT_DEPENDENCIES','contract',[{'path':'/mappings/'+mapping_id,
            'message':'Declare the referenced vocabulary first','classes':sorted(missing_classes),'properties':sorted(missing_properties)}])
    focused={**document,'classes':{k:document['classes'][k] for k in classes},
        'properties':{k:document['properties'][k] for k in properties},'mappings':[mapping], 'constraints':[],'queries':[]}
    return OntologyContract.model_validate(focused)


def validate_component(document,mapping_id,catalog):
    from .engine import materialize,require_valid_graph
    from .trials import validation_fingerprint
    from .sources import SourceCatalog
    contract=component_contract(document,mapping_id)
    selected=[s for s in catalog.sources if s.source_id==contract.mappings[0].source]
    dataset,lineage,validation=materialize(contract,SourceCatalog(catalog.workspace,selected))
    require_valid_graph(validation)
    return {'status':'valid','scope':'component','mapping_id':mapping_id,
        'recordCount':validation['recordCount'],'quadCount':validation['quadCount'],
        'content_fingerprint':validation_fingerprint(contract,dataset,lineage,'component',catalog.fingerprint),
        'deferred_checks':['other source coverage','cross-component constraints','global queries']}


def component_validated(state,mapping_id):
    if mapping_id not in state['items']:raise ValueError('register this mapping as a modeling item first')
    state['items'][mapping_id]['validated']=True
    mark_progress(state,'validated',mapping_id);state['issues']=[];select_next(state)
