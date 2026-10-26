"""The executable expression vocabulary and its recursive authoring schema."""
import re

ARITY = {"hash": 1, "quote": 1, "lower": 1, "strip": 1, "normalize": 1,
         "string": 1, "flatten": 1, "is_object": 1, "is_present": 1, "as_array": 1,
         "if_null": 2, "eq": 2, "if": 3, "object": 0}
OPS = set(ARITY) | {"concat", "coalesce", "add"}
POINTER = {"type": "string", "pattern": r"^(?:/.*)?$"}
CONTEXT_FIELDS = {
    'namespace': 'Contract namespace.', 'source': 'Caller-supplied source ID.',
    'file': 'Original file relative to the workspace.', 'pointer': 'Selected source-record JSON Pointer.',
    'record_hash': 'Selected record content hash.', 'file_hash': 'Original file content hash.',
    'rank': 'One-based SOURCE RECORD rank; constant within that record, including all nested iterations.',
    'index': 'Zero-based CURRENT for_each array index; unavailable outside an iteration. '
             'Combine with an inherited parent node IRI for occurrence identity.',
}


def protocol_guidance():
    return ('\nEXPRESSION VALUE SEMANTICS (shared with the executor):\n'
            'path returns the first match; legacy v1 path treats an empty string as null. '
            'all returns an array of matches, including empty strings. '
            'Legacy coalesce skips BOTH null and empty strings: coalesce(null, "") returns null. '
            'Use if_null(value, fallback) for a literal fallback: if_null(null, "") returns "". '
            'concat/hash/quote propagate null; an empty-string fallback MUST use if_null. '
            'Zero and false are values, never missing. '
            'is_present(value) returns a boolean: false only for null, preserving zero and false as present; legacy path already converts empty strings to null. '
            'as_array(value) explicitly returns a list unchanged, null as [], and a scalar/object as [value]; '
            'choose it only when the evidence supports single-or-multiple values. '
            'Never guess a split delimiter or parse embedded JSON automatically.\n'
            'Operations and argument counts: ' + ', '.join(
                f'{op}({ARITY[op]} args)' if op in ARITY else f'{op}(1+ args)' for op in sorted(OPS))
            + '\nCONTEXT SCOPES:\n' + '\n'.join(f'- {key}: {value}' for key, value in CONTEXT_FIELDS.items())
            + '\nCaller metadata context keys are listed by ontology_inspect. Never invent context keys. '
            'Record selectors and for_each each change the current input; replay before choosing relative paths.\n')


def absolute_iri(value):
    return isinstance(value, str) and bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)) and not any(
        c.isspace() or c in '<>"{}|\\^`' for c in value)


def expression_definitions(context_keys=None):
    ref = {"$ref": "#/$defs/Expression"}
    forms = []
    for key in ("path", "all", "context", "node"):
        forms.append({"type": "object", "properties": {key: POINTER if key in ("path", "all") else {"type": "string"}},
                      "required": [key], "additionalProperties": False})
        if key == 'context' and context_keys is not None:
            forms[-1]['properties'][key]['enum'] = sorted(context_keys)
    for op in sorted(OPS):
        properties = {"op": {"const": op}}
        required = ["op"]
        if op == "object":
            properties.update(fields={"type": "object", "additionalProperties": ref},
                              args={"type": "array", "maxItems": 0})
        else:
            args = {"type": "array", "items": ref, "minItems": ARITY.get(op, 1)}
            if op in ARITY:
                args["maxItems"] = ARITY[op]
            properties["args"] = args
            required.append("args")
        if op == "hash":
            properties["length"] = {"type": "integer", "minimum": 1, "maximum": 64}
        if op == "quote":
            properties["safe"] = {"type": "string"}
        forms.append({"type": "object", "properties": properties, "required": required, "additionalProperties": False})
    return {"ExpressionObject": {"oneOf": forms}, "Expression": {"anyOf": [
        {"type": ["string", "number", "boolean", "null"]},
        {"type": "array", "items": {"$ref": "#/$defs/JsonValue"}},
        {"$ref": "#/$defs/ExpressionObject"},
    ]}}


def contract_schema(schema, context_keys=None):
    """Keep serialized contracts unchanged while describing expressions to Pi."""
    definitions = schema.setdefault("$defs", {})
    definitions.update(expression_definitions(context_keys))
    for name, field in (("Node", "iri"), ("Triple", "value"), ("Group", "for_each"), ("Mapping", "for_each"), ("Group", "when"), ("Mapping", "when")):
        if name not in definitions:
            continue
        prop = definitions[name]["properties"][field]
        description = prop.get("description", "")
        if field == "for_each":
            value = {"anyOf": [{"$ref": "#/$defs/ExpressionObject"}, {"type": "array", "items": {"$ref": "#/$defs/JsonValue"}}, {"type": "null"}]}
        elif field == 'when':
            value = {'anyOf': [{'$ref':'#/$defs/ExpressionObject'}, {'type':'boolean'}, {'type':'null'}]}
        elif name == 'Node':
            value = {'anyOf': [{'type':'string','pattern':r'^[A-Za-z][A-Za-z0-9+.-]*:'},
                               {'$ref':'#/$defs/ExpressionObject'}]}
        else:
            value = {"$ref": "#/$defs/Expression"}
        definitions[name]["properties"][field] = {**value, "description": description}
    if 'Triple' in definitions:
        definitions['Triple']['properties']['kind'].pop('default', None)
        definitions['Triple']['properties']['kind']['description'] = 'Omit to infer from the declared property. Explicit values must agree with that property.'
    return schema
