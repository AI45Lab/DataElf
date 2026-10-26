"""Bounded neighborhoods and actual edge paths (not reachability proofs)."""
from collections import deque
from rdflib import URIRef
from .contracts import Quad,RDFTerm


def explore(dataset,seeds,direction='both',depth=None,predicates=None,mode='neighborhood',target=None,limit=200):
    if direction not in {'in','out','both'} or mode not in {'neighborhood','path','subgraph'}:raise ValueError('invalid exploration mode/direction')
    if depth is None:depth=6 if mode=='path' else 1
    if not 0<=depth<=6 or not 1<=limit<=1000:raise ValueError('exploration budget out of range')
    seeds=[URIRef(s) for s in seeds];queue=deque((s,[],[s]) for s in seeds);seen=set(seeds);edges=set();paths=[];truncated=False;visits=0
    while queue:
        node,path,nodes=queue.popleft()
        if target and node==URIRef(target):
            paths.append({'nodes':[RDFTerm.from_node(n).model_dump(mode='json') for n in nodes],
                          'edges':[Quad.from_nodes(*e).model_dump(mode='json') for e in path]})
            if mode=='path':break
        if len(path)>=depth:continue
        adjacent=[]
        if direction in {'out','both'}:adjacent.extend((q,q[2]) for q in dataset.quads((node,None,None,None)))
        if direction in {'in','both'}:adjacent.extend((q,q[0]) for q in dataset.quads((None,None,node,None)))
        for edge,next_node in adjacent:
            if predicates and str(edge[1]) not in predicates:continue
            visits+=1
            if visits>limit:truncated=True;queue.clear();break
            edges.add(edge)
            if next_node not in seen and not isinstance(next_node,__import__('rdflib').Literal):
                seen.add(next_node);queue.append((next_node,path+[edge],nodes+[next_node]))
    return {'quads':[Quad.from_nodes(*e).model_dump(mode='json') for e in sorted(edges,key=str)],'paths':paths,'truncated':truncated}
