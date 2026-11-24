"""Tool argument contracts and lossless transport normalization."""
import json
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, TypeAdapter, model_validator

from .contracts import GraphConfiguration, OntologyContract, ExtractionPlan, StrictModel, ClassDefinition, Property, Mapping, Triple, Constraint, QueryCheck
from .errors import OntologyError


def normalize_payload(payload):
    """Decode exactly one serialization layer; never guess or fill semantic fields."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise OntologyError('ONTOLOGY_PAYLOAD_JSON_INVALID', 'format', [{
                'path':'/payload','message':exc.msg,'line':exc.lineno,'column':exc.colno,
                'position':exc.pos,'required_change':'Pass an object directly; do not serialize JSON into a string.'}]) from None
        except RecursionError:
            payload = None
    if not isinstance(payload, dict):
        raise OntologyError('ONTOLOGY_ARGUMENT_INVALID', target='arguments', issues=[{
            'path': '/payload', 'message': 'Expected a JSON object (or one JSON serialization of an object). '
            'Pass the object directly; arrays, scalars and double serialization are not accepted.'
        }])
    return payload

Pointer = Annotated[str, StringConstraints(pattern=r"^(?:/.*)?$")]


class Inspect(StrictModel):
    resource: Literal['summary','schema','contract','draft','profile','document','feedback','validation','lineage','review_checks','trial','delivery','language'] = 'summary'
    trial_id: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{32}$')] | None = None
    source: str | None = None
    mapping_id: str | None = None
    field_path: Pointer = ''
    field_offset: int = Field(default=0,ge=0,strict=True,description='Profile field index; follow next_request.')
    page_offset: int = Field(default=0,ge=0,strict=True,description='UTF-8 byte position within the current profile field page.')
    tool: str | None = None
    document: str | None = None
    offset: int = Field(default=0,ge=0,strict=True,description='UTF-8 byte position for non-profile resources. Profiles use page_offset.')
    limit: int = Field(default=12,ge=1,le=40,strict=True)
    max_bytes: int = Field(default=8000,ge=512,le=16000,strict=True)


class Profile(StrictModel):
    page_offset: int = Field(default=0,ge=0,strict=True)
    max_bytes: int = Field(default=8000,ge=512,le=16000,strict=True)
    operation: Literal["profile"] = "profile"
    source: str | None = None
    field_path: Pointer = ''
    field_offset: int = Field(default=0,ge=0,strict=True,description='Profile field index; follow next_request.')
    # Compatibility input only; advertised profile requests use field_offset.
    offset: int = Field(default=0,ge=0,strict=True)
    limit: int = Field(default=12,ge=1,le=40,strict=True)


class Sample(StrictModel):
    operation: Literal["sample"]
    source: str
    limit: int = Field(default=10, ge=1, le=100, strict=True)
    pointer: Pointer = ''
    offset: int = Field(default=0,ge=0,strict=True)
    fields: list[Pointer] = Field(default_factory=list,max_length=16)
    depth: int = Field(default=3,ge=1,le=6,strict=True)
    max_bytes: int = Field(default=8000,ge=512,le=16000,strict=True)


class Replay(StrictModel):
    operation: Literal["replay"]
    file: str
    pointer: Pointer
    offset: int = Field(default=0,ge=0,strict=True)
    max_bytes: int = Field(default=8000,ge=512,le=16000,strict=True)


class Join(StrictModel):
    operation: Literal["join"]
    left: str
    right: str
    left_path: Pointer
    right_path: Pointer


class CachedRead(StrictModel):
    operation: Literal['cached']
    result_id: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]
    offset: int = Field(default=0, ge=0, strict=True)
    max_bytes: int = Field(default=8000, ge=512, le=16000, strict=True)


class Query(StrictModel):
    query: str = Field(min_length=1, description="Local read-only ASK/SELECT SPARQL")


class PatchOperation(StrictModel):
    op: Literal['add','replace']
    path: Pointer
    value: JsonValue


class RemoveOperation(StrictModel):
    op: Literal['remove']
    path: Pointer


class DraftRead(StrictModel):
    action: Literal['read']
    mapping_id: str | None = None
    offset: int = Field(default=0,ge=0,strict=True)
    max_bytes: int = Field(default=8000,ge=512,le=16000,strict=True)


class DraftPatch(StrictModel):
    action: Literal['patch']
    base_hash: str = Field(min_length=1)
    operations: list[Annotated[PatchOperation | RemoveOperation, Field(discriminator='op')]] = Field(min_length=1,max_length=100)


class DraftValidate(StrictModel):
    action: Literal['validate']
    base_hash: str = Field(min_length=1)


class DraftSubmit(StrictModel):
    action: Literal['submit']
    base_hash: str = Field(min_length=1)


DraftAction = Annotated[DraftRead | DraftPatch | DraftValidate | DraftSubmit, Field(discriminator='action')]


class WorkRegister(StrictModel):
    action: Literal['register']
    id: str = Field(min_length=1,max_length=80)
    source: str
    goal: str = Field(min_length=1,max_length=500)
    evidence: list[str] = Field(default_factory=list,max_length=16)


class WorkRead(StrictModel):
    action: Literal['read']


class Configure(StrictModel):
    action: Literal['configure']
    base_hash: str
    namespace: str
    graphs: GraphConfiguration | None = Field(default=None, description="Omitted or null leaves the current draft graph map unchanged. An object replaces it, never merges defaults; schema is required.")


class ComponentEvidence(StrictModel):
    evidence: list[str] = Field(default_factory=list, max_length=16,
        description='Actual supplied file#pointer references supporting this provisional decision.')
    rationale: str = Field(default='', max_length=1000)
    open_questions: list[str] = Field(default_factory=list, max_length=8)


class UpsertClass(ComponentEvidence):
    action: Literal['upsert_class']
    base_hash: str
    id: str
    definition: ClassDefinition


class UpsertProperty(ComponentEvidence):
    action: Literal['upsert_property']
    base_hash: str
    id: str
    definition: Property


class UpsertMapping(ComponentEvidence):
    action: Literal['upsert_mapping']
    base_hash: str
    mapping: Mapping


class IdentityRule(StrictModel):
    strategy: Literal['field','composite','hash','occurrence']
    prefix: str
    paths: list[Pointer] = Field(default_factory=list,max_length=8)
    parent_node: str | None = None


class SetIdentity(StrictModel):
    action: Literal['set_identity']
    base_hash: str
    mapping_id: str
    group_path: Pointer = ''
    node_id: str
    identity: IdentityRule


class AddRelation(StrictModel):
    action: Literal['add_relation']
    base_hash: str
    mapping_id: str
    group_path: Pointer = ''
    relation: Triple


class ValidateComponent(StrictModel):
    action: Literal['validate_component']
    base_hash: str
    mapping_id: str


class SetChecks(StrictModel):
    action: Literal['set_checks']
    base_hash: str
    constraints: list[Constraint] = Field(default_factory=list,max_length=32)
    queries: list[QueryCheck] = Field(default_factory=list,max_length=16)


WorkAction = Annotated[WorkRegister | WorkRead,Field(discriminator='action')]
ComponentAction = Annotated[Configure | UpsertClass | UpsertProperty | UpsertMapping, Field(discriminator='action')]

AuthorAction = Annotated[Configure | UpsertClass | UpsertProperty | UpsertMapping | SetIdentity | AddRelation | ValidateComponent | SetChecks,Field(discriminator='action')]


class Trial(StrictModel):
    contract: OntologyContract | None = None
    scope: Literal['full','component'] = 'full'
    mapping_ids: list[str] = Field(default_factory=list, max_length=100)
    base_hash: str | None = None

    @model_validator(mode='after')
    def check_scope(self):
        if self.scope == 'component' and not self.mapping_ids:
            raise ValueError('component trials require explicit mapping_ids')
        if self.scope == 'full' and self.mapping_ids:
            raise ValueError('mapping_ids are only valid for component trials')
        if len(self.mapping_ids) != len(set(self.mapping_ids)):
            raise ValueError('mapping_ids must be unique')
        return self


ADAPTERS = {
    "ontology_inspect": TypeAdapter(Inspect),
    "ontology_evidence": TypeAdapter(Annotated[Profile | Sample | Replay | Join | CachedRead, Field(discriminator="operation")]),
    "ontology_query": TypeAdapter(Query),
    "ontology_execute": TypeAdapter(Trial),
    'ontology_draft': TypeAdapter(DraftAction),
    'ontology_work':TypeAdapter(WorkAction),
    'ontology_author':TypeAdapter(AuthorAction),
}


def validate_arguments(name, payload):
    if name == "ontology_evidence" and payload == {}:
        payload = {"operation": "profile"}
    if name == 'ontology_evidence' and payload.get('operation') == 'profile' and 'offset' in payload:
        if 'field_offset' in payload:
            raise ValueError('Use field_offset once; offset is a deprecated profile alias')
        payload = {**payload, 'field_offset': payload['offset']}
        payload.pop('offset')
    return ADAPTERS[name].validate_python(payload).model_dump(mode="json",exclude_unset=name=='ontology_author')
