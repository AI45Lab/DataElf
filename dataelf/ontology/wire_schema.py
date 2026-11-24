"""Expose JSON types to shallow tool parsers without changing validation.

Some GLM OpenAI-compatible parsers consult top-level properties only. Pydantic
unions describe properties inside oneOf/$defs, causing valid XML scalar/container
values to be delivered as strings. Redundant type/property declarations preserve
those unions and constraints while making the transport's type information visible.
"""
from copy import deepcopy


def provider_schema(schema):
    result=deepcopy(schema)
    definitions=result.get('$defs',{})

    def types(node,seen=frozenset()):
        if not isinstance(node,dict):return set()
        value=node.get('type')
        if value:return {value} if isinstance(value,str) else set(value)
        ref=node.get('$ref','')
        if ref.startswith('#/$defs/') and ref not in seen:
            return types(definitions.get(ref[8:],{}),seen|{ref})
        return set().union(*(types(branch,seen) for key in ('oneOf','anyOf','allOf') for branch in node.get(key,[])))

    def resolve(node):
        ref=node.get('$ref','')
        return definitions.get(ref[8:],node) if ref.startswith('#/$defs/') else node

    def visit(node):
        if isinstance(node,list):
            for child in node:visit(child)
            return
        if not isinstance(node,dict):return
        possible=types(node)
        if 'type' not in node and possible:node['type']=next(iter(possible)) if len(possible)==1 else sorted(possible)
        branches=node.get('oneOf') or node.get('anyOf') or []
        if branches and possible=={'object'} and 'properties' not in node:
            fields={}
            for branch in branches:
                for key,value in resolve(branch).get('properties',{}).items():fields.setdefault(key,[]).append(value)
            node['properties']={}
            for key,forms in fields.items():
                field_types=set().union(*(types(form) for form in forms))
                if not field_types:continue
                # Bounds, required fields and branch-dependent enums remain in
                # the original union, rather than tightening another branch.
                field={'type':next(iter(field_types)) if len(field_types)==1 else sorted(field_types)}
                constants=[resolve(form)['const'] for form in forms if 'const' in resolve(form)]
                if len(constants)==len(forms):field['enum']=list(dict.fromkeys(constants))
                node['properties'][key]=field
        for child in list(node.values()):visit(child)
    visit(result)
    return result
