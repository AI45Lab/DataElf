"""Small typed author operations compile to the existing executable contract."""
from copy import deepcopy
import re
from .draft import edit_document
from .expressions import absolute_iri


def escaped(key):return key.replace('~','~0').replace('/','~1')


def identity_expression(rule,namespace):
    prefix=rule['prefix'];paths=rule.get('paths',[]);parent=rule.get('parent_node')
    base={'node':parent} if parent else namespace
    start=[base,prefix] if parent or not absolute_iri(prefix) else [prefix]
    strategy=rule['strategy']
    if strategy=='occurrence':
        if not parent:raise ValueError('occurrence identity requires an explicit inherited parent node')
        if paths:raise ValueError('array occurrence uses context.index; choose field/composite for a stable local key')
        tail=[{'context':'index'}]
    elif strategy=='field':
        if len(paths)!=1:raise ValueError('field identity requires exactly one field path')
        tail=[{'op':'quote','args':[{'path':paths[0]}]}]
    elif strategy=='composite':
        if not paths:raise ValueError('composite identity requires field paths')
        tail=[]
        for path in paths:
            if tail:tail.append('/')
            tail.append({'op':'quote','args':[{'path':path}]})
    else:
        if not paths:raise ValueError('hash identity requires declared input paths')
        tail=[{'op':'hash','args':[{'op':'object','fields':{str(i):{'path':p} for i,p in enumerate(paths)}}]}]
    return {'op':'concat','args':start+tail}


def apply_author(document,payload):
    action=payload['action'];operations=[]
    if action=='configure':
        operations=[{'op':'add','path':'/namespace','value':payload['namespace']}]
        if payload.get('graphs') is not None:operations.append({'op':'add','path':'/graphs','value':payload['graphs']})
    elif action in ('upsert_class','upsert_property'):
        section='classes' if action=='upsert_class' else 'properties'
        operations=[{'op':'add','path':'/'+section+'/'+escaped(payload['id']),'value':payload['definition']}]
    elif action=='upsert_mapping':
        mapping=payload['mapping'];index=next((i for i,m in enumerate(document['mappings']) if m['id']==mapping['id']),None)
        operations=[{'op':'add' if index is None else 'replace','path':'/mappings/'+('-' if index is None else str(index)), 'value':mapping}]
    elif action=='set_checks':
        operations=[{'op':'add','path':'/'+key,'value':payload.get(key,[])} for key in ('constraints','queries')]
    else:
        index=next((i for i,m in enumerate(document['mappings']) if m['id']==payload['mapping_id']),None)
        if index is None:raise ValueError('unknown mapping ID')
        group_path=payload.get('group_path','')
        if not re.fullmatch(r'(?:/children/(?:0|[1-9][0-9]*))*',group_path):
            raise ValueError('group_path must locate a nested group using /children/index')
        group=document['mappings'][index]
        for token in group_path.split('/')[1:]:group=group[int(token)] if isinstance(group,list) else group[token]
        prefix='/mappings/'+str(index)+group_path
        if action=='set_identity':
            node_index=next((i for i,n in enumerate(group.get('nodes',[])) if n['id']==payload['node_id']),None)
            if node_index is None:raise ValueError('node alias not found in the selected group')
            rule=payload['identity']
            operations=[{'op':'replace','path':prefix+'/nodes/'+str(node_index)+'/iri',
                'value':identity_expression(rule,document.get('namespace',''))},
                {'op':'add','path':prefix+'/nodes/'+str(node_index)+'/identity_scope',
                 'value':'occurrence' if rule['strategy']=='occurrence' else 'entity'}]
        elif action=='add_relation':
            if payload['relation'] in group.get('triples',[]):return deepcopy(document)
            if 'triples' not in group:operations.append({'op':'add','path':prefix+'/triples','value':[]})
            operations.append({'op':'add','path':prefix+'/triples/-','value':payload['relation']})
        else:raise ValueError('unsupported author action')
    return edit_document(document,operations)


def component_note(document, payload, catalog):
    """Unverified author decisions live in the existing checkpoint, not RDF facts."""
    action=payload['action']
    if action not in ('upsert_class','upsert_property','upsert_mapping'):return None
    section={'upsert_class':'classes','upsert_property':'properties','upsert_mapping':'mappings'}[action]
    ident=payload['mapping']['id'] if section=='mappings' else payload['id']
    evidence=list(dict.fromkeys(payload.get('evidence',[])+(payload.get('mapping',{}).get('evidence') or [])))
    from types import SimpleNamespace
    from .tools import verify_evidence
    verify_evidence(SimpleNamespace(id=ident,evidence=evidence),catalog) if evidence else None
    from .sources import digest
    value=payload['mapping'] if section=='mappings' else payload['definition']
    return {'component_id':ident,'component_path':'/'+section+'/'+escaped(ident),
        'definition_hash':digest(value),'status':'saved_unvalidated','evidence':evidence,
        'rationale':payload.get('rationale',''),'open_questions':payload.get('open_questions',[]),
        'source_fingerprint':catalog.fingerprint}


def draft_gaps(document,catalog):
    """Advisory missing conditions; never substitutes for full preflight/execution."""
    gaps=[]
    for field in ('namespace','classes','mappings'):
        if not document.get(field):gaps.append({'code':'missing_component','path':'/'+field})
    for key,definition in document.get('classes',{}).items():
        parent=definition.get('subClassOf')
        if parent and parent not in document.get('classes',{}):
            gaps.append({'code':'unresolved_class','path':'/classes/'+escaped(key)+'/subClassOf','reference':parent})
    supplied={s.source_id for s in catalog.sources}
    for index,mapping in enumerate(document.get('mappings',[])):
        if mapping['source'] not in supplied:gaps.append({'code':'unknown_source','component':mapping['id'],'source':mapping['source']})
        if not mapping.get('evidence'):gaps.append({'code':'missing_mapping_evidence','component':mapping['id'],'path':'/mappings/'+str(index)+'/evidence'})
    covered={m['source'] for m in document.get('mappings',[])}
    for source in catalog.sources:
        if source.source_id not in covered:gaps.append({'code':'unmapped_source','source':source.source_id})
    if document.get('namespace'):
        from .contracts import OntologyContract
        from .engine import validate_contract
        from .errors import error_feedback
        try:validate_contract(OntologyContract.model_validate(document),{s.source_id for s in catalog.sources})
        except (ValueError,TypeError) as exc:
            report=error_feedback(exc,'contract')
            gaps.extend(report.get('issues') or [{'code':report.get('code'),'message':str(exc)[:800]}])
    else:
        # References can be reported even before the root is executable.
        classes=document.get('classes',{});properties=document.get('properties',{})
        for key,prop in properties.items():
            for field in ('domain','range'):
                if prop.get(field) and (field=='domain' or prop.get('kind')=='iri') and prop[field] not in classes:
                    gaps.append({'code':'unresolved_class','path':'/properties/'+escaped(key)+'/'+field,'reference':prop[field]})
        def visit(group):
            for n in group.get('nodes',[]):
                if n['class_id'] not in classes:gaps.append({'code':'unresolved_class','component':group.get('id'),'reference':n['class_id']})
            for t in group.get('triples',[]):
                if t['predicate'] not in properties and not t['predicate'].startswith(('rdf:','rdfs:')):gaps.append({'code':'unresolved_property','reference':t['predicate']})
            for child in group.get('children',[]):visit(child)
        for mapping in document.get('mappings',[]):visit(mapping)
    return {'issues':gaps[:12],'issues_truncated':len(gaps)>12,
        'pending_checks':['complete deterministic execution and source coverage','independent review','publication'],
        'status':'incomplete' if gaps else 'not_executed'}
