"""Explicit application checks, SHACL constraint projection and source integrity."""
from rdflib import Graph,BNode,URIRef,Literal,RDF
from rdflib.namespace import SH
from ..contracts import OntologyContract
from ..engine import validate_dataset,iri
from ..sources import file_hash
from .contracts import ValidationReport,Quad
from .provenance import fact_id,valid_facts
from .query import view


def shapes(contract):
    graph=Graph()
    for i,c in enumerate(contract.constraints):
        shape=URIRef(contract.namespace+'shape/'+str(i));prop=BNode()
        graph.add((shape,RDF.type,SH.NodeShape));graph.add((shape,SH.targetClass,iri(contract.namespace,c.class_id)))
        graph.add((shape,SH.property,prop));graph.add((prop,SH.path,iri(contract.namespace,c.predicate)))
        graph.add((prop,SH.minCount,Literal(c.min_count)))
        if c.max_count is not None:graph.add((prop,SH.maxCount,Literal(c.max_count)))
    return graph


def validate(state,dataset,evidence_root,scope='commit'):
    issues=[];contract=OntologyContract.model_validate(state['contract']) if state.get('contract') else None
    from ..capabilities import skill_review
    if skill_review(state.get('metadata',{})):
        for relative,expected in (state.get('review') or {}).get('evidence_hashes',{}).items():
            path=(evidence_root/relative).resolve()
            if not path.is_relative_to(evidence_root) or not path.is_file() or file_hash(path)!=expected:
                issues.append({'message':'Reviewer evidence changed or missing','file':relative})
    graphs=[str(g.identifier) for g in dataset.graphs() if len(g)]
    schema=[state['graph_names']['schema']] if 'schema' in state['graph_names'] else []
    inference=set(state['inference_graphs'])
    data=[g for g in graphs if g not in schema]
    if contract:
        selected=view(dataset,data+schema)
        errors,_,found=validate_dataset(selected,contract);issues.extend(found)
    live,valid=valid_facts(state,dataset,evidence_root)
    activities={a['id'] for a in state['provenance']['activities']}
    for activity in state['provenance']['activities']:
        for ref in activity.get('replayed_activities',[]):
            if ref not in activities:issues.append({'message':'unresolved replay activity','activity':ref})
    for fid,item in state['provenance']['facts'].items():
        for support in item['supports']:
            if support.get('activity') not in activities and state.get('provenance_precision')!='limited':
                issues.append({'message':'unresolved provenance activity','fact_id':fid,'activity':support.get('activity')})
            if support.get('file') and support['file'] not in state['source_hashes']:
                issues.append({'message':'evidence is not bound to a frozen hash','fact_id':fid,'evidence':[support['file']]})
    for raw in state['quads']:
        q=Quad.model_validate(raw)
        if q.object.kind=='literal' and getattr(q.object.to_node(),'ill_typed',False):issues.append({'message':'ill-typed literal','fact_id':fact_id(q)})
        provenance=state['provenance']['facts'].get(fact_id(q))
        if not provenance or not provenance['supports']:
            if state.get('provenance_precision')!='limited':issues.append({'message':'unsupported fact','fact_id':fact_id(q)})
        elif fact_id(q) not in valid:issues.append({'message':'fact support or inference requires revalidation','fact_id':fact_id(q)})
    for file,expected in state.get('source_hashes',{}).items():
        path=(evidence_root/file).resolve()
        if not path.is_relative_to(evidence_root) or not path.is_file() or file_hash(path)!=expected:issues.append({'message':'source snapshot changed or absent','evidence':[file]})
    historical=state.get('materialization_issues',[])
    for issue in historical:
        # Dataset rules are recalculated above; execution coverage/contract
        # diagnostics cannot be repaired merely by editing an unrelated quad.
        if issue.get('scope')=='graph' or issue.get('path','').startswith(('/properties/','/constraints/','/queries/')):continue
        issues.append({**issue,'scope':'contract_execution','message':'Unresolved contract execution: '+issue['message']})
    return ValidationReport(status='invalid' if issues else 'valid',scope=scope,data_graphs=data,schema_graphs=schema,
                            rules=['RDF term syntax','contract property kind/datatype/domain/range','declared cardinality and SPARQL','fact evidence and activity links','source hashes','transitive inference freshness'],
                            issues=issues,execution_diagnostics=historical,quad_count=len(state['quads']))
