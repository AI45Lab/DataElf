"""Optional single-operation transport over the existing typed author backend."""
from copy import deepcopy
from pydantic import Field, TypeAdapter, create_model
from .contracts import StrictModel
from .tool_contracts import Configure, UpsertClass, UpsertProperty, UpsertMapping
from .wire_schema import provider_schema

OPERATIONS={'ontology_configure':('configure',Configure),
    'ontology_upsert_class':('upsert_class',UpsertClass),
    'ontology_upsert_property':('upsert_property',UpsertProperty),
    'ontology_upsert_mapping':('upsert_mapping',UpsertMapping)}


def adapter(model):
    fields={k:(v.annotation,deepcopy(v)) for k,v in model.model_fields.items() if k!='action'}
    if 'evidence' in fields:
        fields['evidence']=(list[str],Field(min_length=1,max_length=16,description='Required actual supplied file#pointer references supporting this provisional component.'))
    return TypeAdapter(create_model('Direct'+model.__name__,__base__=StrictModel,**fields))


ADAPTERS={name:adapter(model) for name,(_,model) in OPERATIONS.items()}


def schemas():
    out={}
    for name,a in ADAPTERS.items():
        schema=provider_schema(a.json_schema())
        # Inline just the component object; keep recursive mapping definitions
        # referenced instead of expanding an unbounded execution language.
        key='mapping' if name=='ontology_upsert_mapping' else 'definition'
        prop=schema.get('properties',{}).get(key,{})
        if prop.get('$ref','').startswith('#/$defs/'):
            schema['properties'][key]=deepcopy(schema['$defs'][prop['$ref'][8:]])
        out[name]=schema
    return out
