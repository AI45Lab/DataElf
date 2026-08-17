from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ONTOLOGY_SCHEMA_VERSION = "dataelf-ontology.v2"
GROUNDING_SCHEMA_VERSION = "dataelf-grounding.v2"
REVIEW_SCHEMA_VERSION = "dataelf-ontology-review.v2"
VALIDATOR_VERSION = "dataelf-stage1-validator/4"

CLASS_ID = re.compile(r"^[A-Z][A-Za-z0-9]*$")
PROPERTY_ID = re.compile(r"^[a-z][A-Za-z0-9]*$")
XSD_RANGES = frozenset(
    {
        "xsd:string",
        "xsd:boolean",
        "xsd:integer",
        "xsd:nonNegativeInteger",
        "xsd:positiveInteger",
        "xsd:decimal",
        "xsd:date",
        "xsd:dateTime",
        "xsd:anyURI",
    }
)
TABLE_ROLES = frozenset(
    {"entity", "observation", "association", "metric", "provenance", "metadata", "ignored"}
)
COLUMN_ROLES = frozenset(
    {
        "identity",
        "entity_merge_key",
        "foreign_key",
        "datatype",
        "measure",
        "dimension",
        "provenance",
        "technical",
        "ignored",
    }
)


def schema_directory() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas"


def read_schema(name: str) -> dict[str, Any]:
    value = json.loads((schema_directory() / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"schema is not an object: {name}")
    return value


def schema_errors(instance: Any, name: str) -> list[str]:
    schema = read_schema(name)
    try:
        import jsonschema
    except ImportError:
        return _fallback_schema_errors(instance, schema)
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"/{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    ]


def _fallback_schema_errors(instance: Any, root: dict[str, Any]) -> list[str]:
    """Small Draft 2020-12 subset used when jsonschema is not installed.

    The checked-in Stage 1 schemas intentionally use only this subset. Keeping
    the fallback beside those schemas makes the standalone CLI dependency-free
    while a full jsonschema installation remains usable automatically.
    """

    errors: list[str] = []

    def resolve(reference: str) -> dict[str, Any]:
        if not reference.startswith("#/"):
            raise ValueError(f"unsupported external schema reference: {reference}")
        value: Any = root
        for part in reference[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
        if not isinstance(value, dict):
            raise ValueError(f"schema reference is not an object: {reference}")
        return value

    def at(path: tuple[str | int, ...]) -> str:
        return "/" + "/".join(str(part) for part in path)

    def visit(value: Any, schema: dict[str, Any], path: tuple[str | int, ...]) -> None:
        if "$ref" in schema:
            visit(value, resolve(str(schema["$ref"])), path)
        for child in schema.get("allOf", []):
            if isinstance(child, dict):
                visit(value, child, path)
        expected_type = schema.get("type")
        type_matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }
        if isinstance(expected_type, str) and not type_matches.get(expected_type, False):
            errors.append(f"{at(path)}: expected type {expected_type}")
            return
        if "const" in schema and value != schema["const"]:
            errors.append(f"{at(path)}: expected constant {schema['const']!r}")
        if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
            errors.append(f"{at(path)}: value is not one of {schema['enum']!r}")
        if isinstance(value, str):
            if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
                errors.append(f"{at(path)}: string is shorter than {schema['minLength']}")
            if isinstance(schema.get("pattern"), str) and not re.search(schema["pattern"], value):
                errors.append(f"{at(path)}: string does not match {schema['pattern']}")
        if isinstance(value, list):
            if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
                errors.append(f"{at(path)}: array has fewer than {schema['minItems']} items")
            if schema.get("uniqueItems") is True:
                serialized = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
                if len(serialized) != len(set(serialized)):
                    errors.append(f"{at(path)}: array items are not unique")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    visit(item, item_schema, path + (index,))
        if isinstance(value, dict):
            required = schema.get("required", [])
            for key in required if isinstance(required, list) else []:
                if key not in value:
                    errors.append(f"{at(path)}: required property {key!r} is missing")
            if isinstance(schema.get("minProperties"), int) and len(value) < schema["minProperties"]:
                errors.append(f"{at(path)}: object has fewer than {schema['minProperties']} properties")
            properties = schema.get("properties", {}) if isinstance(schema.get("properties", {}), dict) else {}
            additional = schema.get("additionalProperties", True)
            for key, child in value.items():
                if key in properties and isinstance(properties[key], dict):
                    visit(child, properties[key], path + (key,))
                elif isinstance(additional, dict):
                    visit(child, additional, path + (key,))
                elif additional is False:
                    errors.append(f"{at(path + (key,))}: additional property is not allowed")

    visit(instance, root, ())
    return errors
