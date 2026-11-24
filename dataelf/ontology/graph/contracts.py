"""Typed public graph protocol. Literal lexical forms are never coerced."""
from typing import Literal as Kind
from pydantic import Field, model_validator
from rdflib import URIRef, BNode, Literal
from ..contracts import StrictModel
from ..expressions import absolute_iri

class GraphRef(StrictModel):
    model_config={**StrictModel.model_config,'json_schema_extra':{'oneOf':[
        {'required':['revision_id'],'properties':{'revision_id':{'type':'string'},'workspace_id':{'type':'null'}}},
        {'required':['workspace_id'],'properties':{'workspace_id':{'type':'string'},'revision_id':{'type':'null'}}}]}}
    graph_id: str = Field(pattern=r'^[A-Za-z0-9_-]+$')
    revision_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]+$')
    workspace_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]+$')

    @model_validator(mode='after')
    def explicit_ref(self):
        if bool(self.revision_id)==bool(self.workspace_id):
            raise ValueError('choose exactly one immutable revision or candidate workspace')
        return self

class RDFTerm(StrictModel):
    kind: Kind['iri','literal','blank']
    value: str
    datatype: str | None = None
    lang: str | None = None

    @model_validator(mode='after')
    def syntax(self):
        if self.kind=='iri' and not absolute_iri(self.value):raise ValueError('invalid absolute IRI')
        if self.kind!='literal' and (self.datatype is not None or self.lang is not None):raise ValueError('only literals have datatype/lang')
        if self.datatype is not None and (self.lang is not None or not absolute_iri(self.datatype)):raise ValueError('invalid literal metadata')
        if self.kind=='blank':
            from rdflib.plugins.parsers.ntriples import r_nodeid
            if not r_nodeid.fullmatch('_:'+self.value):raise ValueError('invalid blank node label')
        if self.lang is not None and not __import__('re').fullmatch(r'[A-Za-z]+(?:-[A-Za-z0-9]+)*',self.lang):raise ValueError('invalid language tag')
        if self.lang:self.lang=self.lang.lower()
        return self

    def to_node(self):
        if self.kind=='iri':return URIRef(self.value)
        if self.kind=='blank':return BNode(self.value)
        return Literal(self.value,datatype=URIRef(self.datatype) if self.datatype else None,lang=self.lang,normalize=False)

    @classmethod
    def from_node(cls,node):
        if isinstance(node,URIRef):return cls(kind='iri',value=str(node))
        if isinstance(node,BNode):return cls(kind='blank',value=str(node))
        if isinstance(node,Literal):return cls(kind='literal',value=str(node),datatype=str(node.datatype) if node.datatype else None,lang=node.language)
        raise ValueError('unsupported RDF term')

class Quad(StrictModel):
    subject: RDFTerm
    predicate: RDFTerm
    object: RDFTerm
    graph: str
    @model_validator(mode='after')
    def syntax(self):
        if self.subject.kind=='literal' or self.predicate.kind!='iri' or not absolute_iri(self.graph):raise ValueError('invalid quad positions')
        return self
    def nodes(self):return self.subject.to_node(),self.predicate.to_node(),self.object.to_node(),URIRef(self.graph)
    @classmethod
    def from_nodes(cls,s,p,o,g):return cls(subject=RDFTerm.from_node(s),predicate=RDFTerm.from_node(p),object=RDFTerm.from_node(o),graph=str(g))

class Evidence(StrictModel):
    file: str | None = None
    pointer: str = ''
    source_id: str | None = None
    fact_ids: list[str] = Field(default_factory=list)
    mapping: str | None = None
    query: str | None = None
    @model_validator(mode='after')
    def supported(self):
        if not self.file and not self.fact_ids:raise ValueError('evidence needs a file or premise fact IDs')
        return self

class Operation(StrictModel):
    action: Kind['add_fact','replace_fact','retract_fact','delete_entity']
    quad: Quad | None = None
    new_quad: Quad | None = None
    entity: RDFTerm | None = None
    graphs: list[str] = Field(default_factory=list)
    source_id: str | None = None
    require_present: bool = True
    @model_validator(mode='after')
    def complete(self):
        if self.action=='delete_entity':
            if not self.entity or self.entity.kind=='literal' or not self.graphs:raise ValueError('delete_entity needs entity and explicit graph scope')
        elif self.quad is None:raise ValueError('fact operation needs a quad')
        if self.action=='replace_fact' and not self.new_quad:raise ValueError('replace_fact needs new_quad')
        return self

class ChangeSet(StrictModel):
    base_revision: str | None = None
    operations: list[Operation] = Field(default_factory=list)
    sparql: str | None = None
    target_graphs: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    expected_hash: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    @model_validator(mode='after')
    def one_kind(self):
        if bool(self.operations)==bool(self.sparql):raise ValueError('choose structured operations or SPARQL update')
        if self.sparql and not self.target_graphs:raise ValueError('SPARQL update needs explicit target graphs')
        return self

class GraphDiff(StrictModel):
    added: list[Quad] = Field(default_factory=list)
    removed: list[Quad] = Field(default_factory=list)
    affected_entities: list[str] = Field(default_factory=list)
    named_graphs: list[str] = Field(default_factory=list)
    truncated: bool = False
    added_count: int = 0
    removed_count: int = 0

class ValidationReport(StrictModel):
    status: Kind['valid','invalid']
    scope: str = 'commit'
    data_graphs: list[str] = Field(default_factory=list)
    schema_graphs: list[str] = Field(default_factory=list)
    inference: str = 'none'
    rules: list[str] = Field(default_factory=list)
    issues: list[dict] = Field(default_factory=list)
    execution_diagnostics: list[dict] = Field(default_factory=list)
    quad_count: int = 0

class CommitResult(StrictModel):
    status: Kind['completed','pending_review','invalid','conflict','rejected']
    graph_ref: GraphRef | None = None
    revision_id: str | None = None
    candidate_hash: str | None = None
    manifest_path: str | None = None
    validation: ValidationReport | None = None
    review: dict | None = None
    issues: list[dict] = Field(default_factory=list)
    review_diagnostics: dict | None = None
