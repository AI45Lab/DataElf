"""Transactional operations; display limits never limit mutation."""
from rdflib import URIRef
from .contracts import Quad
from .provenance import support,retract,fact_id,refresh_dependencies
from .query import execute
from .store import decode_state


def apply(state,dataset,changes,activity,inference_graphs):
    def add(q):
        s,p,o,g=q.nodes();dataset.graph(g).add((s,p,o))
        support(state,q,changes.evidence,activity,q.graph in inference_graphs,dataset)
        refresh_dependencies(state,dataset)
    def remove(q,source=None,required=True):
        s,p,o,g=q.nodes()
        if (s,p,o) not in dataset.graph(g):
            if required:raise ValueError('fact precondition failed: missing old quad')
            return
        if retract(state,q,source):dataset.graph(g).remove((s,p,o))
        refresh_dependencies(state,dataset)
    for op in changes.operations:
        if op.action=='add_fact':add(op.quad)
        elif op.action=='replace_fact':remove(op.quad,op.source_id,op.require_present);add(op.new_quad)
        elif op.action=='retract_fact':remove(op.quad,op.source_id,op.require_present)
        else:
            entity=op.entity.to_node()
            affected=[Quad.from_nodes(*q) for q in dataset.quads((None,None,None,None))
                      if str(q[3]) in op.graphs and (q[0]==entity or q[2]==entity)]
            if op.require_present and not affected:raise ValueError('entity precondition failed')
            for q in affected:remove(q,op.source_id)
    if changes.sparql:
        target=set(changes.target_graphs)
        before=set(dataset.quads((None,None,None,None)))
        updated=decode_state(execute(dataset,changes.sparql,operation='update'))
        after=set(updated.quads((None,None,None,None)))
        if any(str(q[3]) not in target for q in before^after):raise ValueError('SPARQL modified a graph outside explicit targets')
        for q in before-after:remove(Quad.from_nodes(*q))
        for q in after-before:add(Quad.from_nodes(*q))
    return dataset
