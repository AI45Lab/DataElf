"""Atomic candidate snapshots and immutable revision directories."""
import json
import shutil
import uuid
import fcntl
from contextlib import contextmanager
from pathlib import Path
from rdflib import Dataset, Graph, URIRef, BNode, Literal
from rdflib.compare import to_canonical_graph
from ..sources import digest
from .contracts import Quad


def write_json(path,value):
    """Durable same-directory replacement, including the directory entry."""
    import os
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('w',encoding='utf-8') as handle:
            json.dump(value,handle,ensure_ascii=False,indent=2,default=str);handle.write('\n')
            handle.flush();os.fsync(handle.fileno())
        temporary.replace(path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:temporary.unlink(missing_ok=True)

P=URIRef('urn:dataelf:canonical:predicate')
S=URIRef('urn:dataelf:canonical:subject')
O=URIRef('urn:dataelf:canonical:object')
G=URIRef('urn:dataelf:canonical:graph')


def canonical_quads(dataset):
    # Reify each quad before canonicalization: a union alone loses graph identity.
    quads=set(dataset.quads((None,None,None,None)))
    if not any(isinstance(term,BNode) for q in quads for term in q):return quads
    encoded=Graph()
    for s,p,o,g in quads:
        node=BNode()
        for key,value in [(S,s),(P,p),(O,o),(G,g)]:encoded.add((node,key,value))
    canonical=to_canonical_graph(encoded)
    return {(canonical.value(n,S),canonical.value(n,P),canonical.value(n,O),canonical.value(n,G))
            for n in canonical.subjects(P,None)}


def content_hash(dataset):
    return digest(sorted([[term.n3() for term in q] for q in canonical_quads(dataset)]))


def decode_state(state):
    ds=Dataset(default_union=True)
    for raw in state.get('quads',[]):
        q=Quad.model_validate(raw);s,p,o,g=q.nodes();ds.graph(g).add((s,p,o))
    return ds


def encode_dataset(dataset):
    return sorted([Quad.from_nodes(*q).model_dump(mode='json') for q in dataset.quads((None,None,None,None))],key=lambda q:json.dumps(q,sort_keys=True))

class GraphStore:
    def __init__(self,root):self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
    def graph_path(self,graph_id):
        if not __import__('re').fullmatch(r'[A-Za-z0-9_-]+',graph_id):raise ValueError('invalid graph ID')
        path=(self.root/graph_id).resolve()
        if not path.is_relative_to(self.root):raise ValueError('graph path escapes store')
        return path
    @contextmanager
    def lock(self,graph_id):
        path=self.graph_path(graph_id);path.mkdir(parents=True,exist_ok=True)
        with (path/'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            try:yield path
            finally:fcntl.flock(lock,fcntl.LOCK_UN)
    def path(self,ref):
        base=self.graph_path(ref.graph_id)
        path=base/('workspaces' if ref.workspace_id else 'revisions')/(ref.workspace_id or ref.revision_id)
        if not path.resolve().is_relative_to(base):raise ValueError('reference escapes graph store')
        return path
    def load(self,ref):
        state=json.loads((self.path(ref)/'state.json').read_text())
        if not ref.workspace_id:
            manifest=json.loads((self.path(ref)/'manifest.json').read_text())
            if manifest.get('schemaVersion')!='dataelf.ontology.graph.manifest.v2' or manifest.get('status')!='completed':raise ValueError('unsupported or incomplete graph revision')
            if manifest.get('graph_id')!=ref.graph_id or manifest.get('revision_id')!=ref.revision_id:raise ValueError('manifest reference mismatch')
            from ..sources import file_hash
            for name,value in manifest['artifacts'].items():
                path=(self.path(ref)/name).resolve()
                if not path.is_relative_to(self.path(ref).resolve()) or file_hash(path)!=value:raise ValueError('published artifact hash mismatch: '+name)
        return state
    def save(self,ref,state):
        if not ref.workspace_id:raise ValueError('published revisions are immutable')
        write_json(self.path(ref)/'state.json',state)
    def head(self,graph_id):
        path=self.graph_path(graph_id)/'head.json'
        return json.loads(path.read_text())['revision_id'] if path.exists() else None
    def recover(self,graph_id):
        # Staging directories are never visible as revisions. Retrying commit
        # discovers a finished revision by its commit key before advancing head.
        with self.lock(graph_id) as root:
            for p in (root/'revisions').glob('.staging-*'):
                shutil.rmtree(p)


def parse_nquads(path):
    """Read authoritative N-Quads without normalizing literal lexical values."""
    from rdflib.plugins.parsers.nquads import NQuadsParser
    from rdflib.plugins.parsers.ntriples import r_literal, unquote, uriquote
    from rdflib.parser import create_input_source
    class LexicalParser(NQuadsParser):
        def literal(self):
            if not self.peek('"'):return False
            text,lang,datatype=self.eat(r_literal).groups()
            dtype=URIRef(uriquote(unquote(datatype))) if datatype else None
            return Literal(unquote(text),lang=lang or None,datatype=dtype,normalize=False)
    ds=Dataset(default_union=True)
    source=create_input_source(source=str(path))
    try:LexicalParser().parse(source,ds)
    finally:source.close()
    return ds
