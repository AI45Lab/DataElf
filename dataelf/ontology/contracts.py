"""Public, domain-independent ontology input and executable contract."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator, model_serializer, AfterValidator
from typing_extensions import TypedDict


DomainName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassDefinition(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra='forbid')
    label: str
    comment: str
    subClassOf: str | None
    annotations: dict[str, JsonValue]
    # Optional caller annotations retained for existing composed resources;
    # neither drives executor behavior or selects a business-specific branch.
    id: str
    kind: str


class SourceInput(StrictModel):
    source_id: str
    path: Path
    format: Literal["json", "jsonl", "csv"] | None = None
    records_path: str = ""
    # Optional exact selection and caller-supplied metadata; never replaces raw data.
    record_pointers: list[str] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    record_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Node(StrictModel):
    id: str
    class_id: str = Field(description="Key in contract.classes, e.g. Item; not the full class IRI")
    iri: JsonValue = Field(description="Absolute IRI constant or expression, e.g. {op: concat, args: [urn:item:, {path: /id}]}")
    graph: str = "domain"
    identity_scope: Literal['entity', 'occurrence'] = Field(default='entity',
        description='entity allows repeated identifiers for the same entity. occurrence requires distinct identifiers within each parent for_each iteration; use parent node plus context.index.')


class Triple(StrictModel):
    subject: str
    predicate: str
    value: JsonValue = Field(description='Expression or literal constant. To reference a node use {"node":"alias"}, never the bare alias string.')
    graph: str = "domain"
    kind: Literal["literal", "iri"] = "literal"
    datatype: str | None = None
    required: bool = False
    conflict: Literal["union", "error", "consensus"] = "union"


class Group(StrictModel):
    for_each: JsonValue = Field(default=None, description='Array expression relative to the current group input, e.g. {"path":"/calls"} or {"all":"/calls/*"}. A bare "/calls" is a string constant, not a path.')
    when: JsonValue = Field(default=None, description='Optional boolean expression evaluated per iteration before creating this group, with current input and inherited parent aliases. False skips this group and its children; null/omitted is unconditional. Non-boolean results are errors.')
    nodes: list[Node] = Field(default_factory=list)
    triples: list[Triple] = Field(default_factory=list)
    children: list[Group] = Field(default_factory=list)


class Mapping(Group):
    id: str
    source: str
    selector: str = Field(default="", pattern=r"^(?:/.*)?$", description="JSON Pointer relative to the source record; empty string means the record itself")
    min_records: int = Field(default=1, ge=0, strict=True)
    evidence: list[str] = Field(default_factory=list)


class Property(StrictModel):
    kind: Literal["literal", "iri"] = "literal"
    emit_schema: bool = True
    domain: str | None = None
    range: str | None = None


class QueryCheck(StrictModel):
    id: str
    query: str
    expect: bool | None = None
    min_rows: int | None = Field(default=None, ge=0, strict=True)


class Constraint(StrictModel):
    class_id: str
    predicate: str
    min_count: int = Field(default=0, ge=0, strict=True)
    max_count: int | None = Field(default=None, ge=0, strict=True)


def require_schema_graph(value):
    if 'schema' not in value:
        raise ValueError('/graphs/schema is required; include schema in the replacement graph map')
    return value


GraphConfiguration = Annotated[dict[str, str],
    Field(description='Graph key to RDF graph identifier, not a description. Values without a colon are appended to namespace (e.g. graph/schema); absolute IRIs such as urn:graph:schema are used directly. An object replaces the entire map; include schema and every graph key used by mappings.',
          json_schema_extra={'required': ['schema'], 'properties': {'schema': {'type': 'string'}}}),
    AfterValidator(require_schema_graph)]


class OntologyContract(StrictModel):
    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        from .expressions import contract_schema
        return contract_schema(super().model_json_schema(*args, **kwargs))

    schema_version: Literal["dataelf.ontology.v1"] = "dataelf.ontology.v1"
    domain: DomainName
    namespace: str
    classes: dict[str, ClassDefinition]
    properties: dict[str, Property] = Field(default_factory=dict)
    graphs: GraphConfiguration = Field(default_factory=lambda: {
        key: "graph/" + key for key in ("schema", "domain", "observation", "source")
    })
    mappings: list[Mapping]
    constraints: list[Constraint] = Field(default_factory=list)
    queries: list[QueryCheck] = Field(default_factory=list)
    description: str = ""

    @model_validator(mode='after')
    def infer_triple_kinds(self):
        def visit(group):
            for triple in group.triples:
                if 'kind' not in triple.model_fields_set and triple.predicate in self.properties:
                    triple.kind = self.properties[triple.predicate].kind
            for child in group.children:
                visit(child)
        for mapping in self.mappings:
            visit(mapping)
        return self


class TemplateSource(StrictModel):
    format: Literal["json", "jsonl", "csv"]
    records_path: str = ""


class OntologyTemplate(StrictModel):
    schema_version: Literal["dataelf.ontology.template.v1"] = "dataelf.ontology.template.v1"
    domain: DomainName
    contract: OntologyContract
    source_bindings: dict[str, TemplateSource] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_domain(self):
        if self.domain != self.contract.domain:
            raise ValueError("template domain differs from its contract domain")
        return self


class ExtractionPlan(StrictModel):
    schema_version: Literal["dataelf.ontology.plan.v1"] = "dataelf.ontology.plan.v1"
    contract_hash: str
    # Only physical record selection may be refined; semantics remain in the contract.
    selectors: dict[str, Annotated[str, StringConstraints(pattern=r"^(?:/.*)?$")]] = Field(default_factory=dict,
        description="Keys are contract mapping IDs, not source IDs. Values are JSON Pointers relative to source records; only physical selection may change.")
    contract_revision: ReviewIssue | None = None


class ReviewIssue(StrictModel):
    nature: Literal['confirmed_error','question','suggestion'] = 'question'
    issue_id: str | None = None
    location: str | None = None
    impact: str = ""
    blocking: bool = True
    severity: Literal["critical", "high", "medium", "low"]
    target: Literal["contract", "plan", "graph"]
    message: str
    evidence: list[str] = Field(min_length=1)
    required_change: str
    acceptance_criteria: str


ExtractionPlan.model_rebuild()


class ReviewUpdate(StrictModel):
    issue_id: str
    status: Literal['resolved','unresolved','regressed','withdrawn']
    evidence: list[str] = Field(min_length=1)
    explanation: str


class ReviewBlocker(StrictModel):
    kind: Literal['source_unavailable','source_inconsistent','execution_unsupported','tool_transport','infrastructure']
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


class Review(StrictModel):
    issue_updates: list[ReviewUpdate] = Field(default_factory=list)
    blocker: ReviewBlocker | None = None
    verdict: Literal["approve", "revise", "unusable"]
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str


class OntologyRequest(StrictModel):
    domain: DomainName
    workspace: Path
    sources: list[SourceInput] = Field(default_factory=list)
    workflow_policy: Literal['legacy', 'ai_index_cli_skill_review_v1'] = 'legacy'
    task_context: dict[str, Any] = Field(default_factory=dict)
    documents: list[Path] = Field(default_factory=list)
    objective: str = "Model the supplied data with replayable evidence."
    config: Path | None = None
    template: Path | None = None

    @model_validator(mode='after')
    def validate_source_policy(self):
        from .capabilities import validate_scope, skill_review
        validate_scope(self.workflow_policy, self.domain)
        if not self.sources and not skill_review(self):
            raise ValueError('ontology requires at least one source')
        return self

    @model_serializer(mode='wrap')
    def serialize_policy(self,handler):
        value=handler(self)
        if self.workflow_policy=='legacy':
            value.pop('workflow_policy',None)
            if not self.task_context:value.pop('task_context',None)
        return value


class OntologyRunResult(BaseModel):
    status: Literal["completed", "incomplete", "failed"]
    stage: Literal["modeling", "review", "stage1", "stage2", "rdf"]
    graph_id: str | None = None
    revision_id: str | None = None
    bundle_path: str | None = None
    contract_path: str | None = None
    graph_ref: dict | None = None
    # Legacy bundle aliases refer to the SAME revision, not two model stages.
    stage1_run_id: str | None = None
    stage2_run_id: str | None = None
    stage1_bundle: str | None = None
    stage2_bundle: str | None = None
    nquads_path: str | None = None
    rdfxml_path: str | None = None
    ntriples_path: str | None = None
    manifest_path: str | None = None
    validation_path: str | None = None
    template_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    def rdf_paths(self):
        return tuple(Path(p) if p else None for p in (self.nquads_path, self.rdfxml_path, self.ntriples_path))
