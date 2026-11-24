"""Host schema optimization preserves the public expression language."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, validators

from dataelf.ontology.contracts import OntologyContract
from dataelf.ontology.draft import edit_document
from dataelf.ontology.errors import OntologyError
from dataelf.ontology.expressions import ARITY, OPS, validation_schema


def expression_schema():
    schema = OntologyContract.model_json_schema()
    return {"$ref": "#/$defs/Expression", "$defs": schema["$defs"]}


def examples():
    values = [None, True, False, 0, 1.5, "", "text", [], [{"literal": 0}],
              {"path": "/id"}, {"path": ""}, {"all": "/items/*"},
              {"context": "index"}, {"node": "entry"}, {}, {"path": "bad"},
              {"path": 5}, {"path": "/a", "node": "x"}, {"op": "unknown"}]
    for op in sorted(OPS):
        valid = {"op": op, "args": ["value"] * ARITY.get(op, 1)}
        if op == "object":
            valid["fields"] = {"x": {"path": "/x"}}
        values += [valid, {**valid, "extra": True}, {**valid, "args": "bad"}]
        if op != "object":
            values.append({"op": op})
    values += [{"op": "hash", "args": ["x"], "length": True},
               {"op": "hash", "args": ["x"], "length": 65},
               {"op": "quote", "args": ["x"], "safe": 2},
               {"op": "object", "fields": {"x": {"op": "unknown"}}}]
    return values


@pytest.mark.parametrize("value", examples())
def test_host_and_public_expression_schema_accept_the_same_values(value):
    schema = expression_schema()
    before = deepcopy(schema)
    optimized = validation_schema(schema)
    assert schema == before
    assert Draft202012Validator(optimized).is_valid(value) == Draft202012Validator(schema).is_valid(value)


@pytest.mark.parametrize("depth", [1, 4, 12])
@pytest.mark.parametrize("valid", [True, False])
def test_nested_validation_visits_expression_union_once_per_node(depth, valid):
    expression = {"path": "/id"} if valid else {"op": "not-supported"}
    for _ in range(depth):
        expression = {"op": "if", "args": [True, expression, "urn:fallback"]}
    calls = 0
    original = Draft202012Validator.VALIDATORS["oneOf"]

    def counted(validator, branches, instance, schema):
        nonlocal calls
        calls += 1
        yield from original(validator, branches, instance, schema)

    validator = validators.extend(Draft202012Validator, {"oneOf": counted})
    assert validator(validation_schema(expression_schema())).is_valid(expression) == valid
    assert calls == depth + 1


def test_deep_draft_patch_preserves_partial_shape_validation_and_atomic_input():
    draft = {"schema_version": "dataelf.ontology.v1", "domain": "sample", "classes": {}, "properties": {}, "mappings": []}
    expr = "urn:entry:a"
    for _ in range(12):
        expr = {"op": "if", "args": [True, expr, "urn:fallback"]}
    mapping = {"id": "rows", "source": "rows", "nodes": [{"id": "a", "class_id": "Entry", "iri": expr}]}
    result = edit_document(draft, [{"op": "add", "path": "/mappings/-", "value": mapping}])
    assert result["mappings"][0]["nodes"][0]["iri"] == expr
    assert draft["mappings"] == []
    for invalid in [{**mapping, "unexpected": True}, {**mapping, "min_records": -1}]:
        with pytest.raises(OntologyError):
            edit_document(draft, [{"op": "add", "path": "/mappings/-", "value": invalid}])
    assert draft["mappings"] == []
