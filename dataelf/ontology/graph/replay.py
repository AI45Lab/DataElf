"""Snapshot-bound maintenance replay guards, separate from template bindings."""
from rdflib import RDF
from ..engine import iri
from ..contracts import OntologyContract
from ..sources import digest,file_hash


def occurrence_guards(state,entities):
    identities=state.get('projection_lineage',{}).get('identities',{})
    guards={entity:{'basis':'record_hash','hash':identities[entity]['record_hash']}
            for entity in entities if entity in identities and identities[entity]['scope']=='occurrence'}
    if identities or not state.get('contract'):return guards
    # Old manifests lack per-occurrence record hashes. Guard their generated
    # facts conservatively; never pretend their positional identity is durable.
    contract=OntologyContract.model_validate(state['contract']);classes=set()
    def visit(group):
        classes.update(str(iri(contract.namespace,n.class_id)) for n in group.nodes if n.identity_scope=='occurrence')
        for child in group.children:visit(child)
    for mapping in contract.mappings:visit(mapping)
    for q in state['quads']:
        if q['subject']['value'] in entities and q['predicate']['value']==str(RDF.type) and q['object']['value'] in classes:
            entity=q['subject']['value'];guards[entity]={'basis':'generated_facts','hash':generated_hash(state,entity)}
    return guards


def generated_hash(state,entity):
    rows=[r['quad'] for r in state.get('projection_lineage',{}).get('facts',[]) if r['quad']['subject']['value']==entity]
    return digest(sorted(rows,key=lambda q:digest(q)))


def verify_replay(saved,old,new,root):
    expected=saved.get('evidence_hashes')
    if expected is None:
        expected={e['file']:old['source_hashes'].get(e['file']) for e in saved['change']['evidence'] if e.get('file')}
    for file,hash in expected.items():
        path=(root/file).resolve()
        if hash is None or not path.is_relative_to(root) or not path.is_file() or file_hash(path)!=hash:
            raise ValueError('maintenance replay conflict: frozen evidence changed or missing: '+file)
        if file in new['source_hashes'] and new['source_hashes'][file]!=hash:
            raise ValueError('maintenance replay conflict: evidence snapshot requires explicit rebasing: '+file)
        new['source_hashes'][file]=hash
    guards=saved.get('occurrence_guards')
    if guards is None:
        entities={q['subject']['value'] for op in saved['change']['operations'] for q in [op.get('quad'),op.get('new_quad')] if q}
        guards=occurrence_guards(old,entities)
    identities=new.get('projection_lineage',{}).get('identities',{})
    for entity,guard in guards.items():
        actual=identities.get(entity,{}).get('record_hash') if guard['basis']=='record_hash' else generated_hash(new,entity)
        if actual!=guard['hash']:raise ValueError('maintenance replay conflict: occurrence identity changed; explicitly rebase '+entity)
