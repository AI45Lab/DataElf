"""Single source of truth for the Finance output artifact contract.

One task kind, one contract: every job produces ``results/results.json``
plus ``results/final_brief.md``. The default payload schema is declared
here once (``DEFAULT_SCHEMA``); the prompt output section (rendered as JSON
Schema), ``plugin.output_contract``, review validation, and result-id
extraction all derive from it. Which depth to run at — convergent answer or
open synthesis — is the model's judgment, not a harness mode.

The contract pins only what downstream parsing genuinely needs (a summary,
findings, evidence, calibration, limitations) and deliberately leaves room
around it: ``key_findings`` elements are free-form (strings or objects
carrying their own evidence/confidence), ``limitations`` may be prose or a
list, and additional fields are welcome. Tasks specialize the schema
through field overrides in their benchmark config or the
``finance_output_fields`` runtime parameter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dataelf.discovery.contracts import OutputArtifactSpec, OutputContract

BRIEF_ARTIFACT_ID = "finance_brief"
BRIEF_KIND = "finance_brief"
# Harness-assigned result id for the workspace index; the model never
# fabricates ids (one job produces exactly one result, identified by the job).
RESULT_ID = "finance_results"


@dataclass(frozen=True)
class FieldSpec:
    """One member of the payload object, shared by prompt rendering and review."""

    name: str
    required: bool = True
    kind: str = "string"  # "string" | "number" | "list" | "str_or_list"
    prompt_hint: str = ""
    value_range: tuple[float, float] | None = None  # numeric bounds, inclusive
    free_items: bool = False  # list whose elements are intentionally unconstrained


@dataclass(frozen=True)
class OutputSchema:
    """The declared deliverable: payload file plus its paired brief."""

    contract_id: str
    artifact_id: str
    path: str
    kind: str
    label: str                     # human noun used in review messages
    deliverable_description: str
    brief_path: str
    fields: tuple[FieldSpec, ...] = ()
    requirement_prose: str = ""    # prompt finding-requirements paragraph
    metrics_key: str = ""          # review metrics naming

    def output_contract(self) -> OutputContract:
        artifacts = [
            OutputArtifactSpec(
                artifact_id=self.artifact_id, path=self.path, kind=self.kind,
                media_type="application/json",
            ),
            OutputArtifactSpec(
                artifact_id=BRIEF_ARTIFACT_ID, path=self.brief_path,
                kind=BRIEF_KIND, media_type="text/markdown",
            ),
        ]
        return OutputContract(contract_id=self.contract_id, version="1", artifacts=artifacts)


DEFAULT_SCHEMA = OutputSchema(
    contract_id="finance.summary",
    artifact_id="finance_results",
    path="results/results.json",
    kind="finance_results",
    label="results",
    deliverable_description="the unified result payload (answer or synthesis, as the objective demands)",
    brief_path="results/final_brief.md",
    fields=(
        FieldSpec("summary", prompt_hint="the definitive answer, or the synthesized overview for exploratory work"),
        FieldSpec(
            "key_findings", kind="list", free_items=True,
            prompt_hint="findings supporting the summary; items are free-form — plain strings, or objects "
                        "carrying the finding with its own evidence_refs / confidence / supporting_data",
        ),
        FieldSpec("evidence_refs", kind="list", prompt_hint="workspace paths or source URLs backing the result"),
        FieldSpec("confidence", kind="number", prompt_hint="calibrated confidence for the overall result", value_range=(0.0, 1.0)),
        FieldSpec("limitations", kind="str_or_list", prompt_hint="caveats and scope limits, as prose or a list"),
    ),
    requirement_prose=(
        "The summary must state either the verified answer or the synthesized overview, backed by concrete findings. "
        "Every finding carries evidence references, calibrated confidence from 0 to 1, and explicit limitations. "
        "Distinguish reported facts from derived calculations and never present an unverified guess as the answer."
    ),
    metrics_key="summary_present",
)


def output_schema(field_overrides: list[dict[str, Any]] | None = None) -> OutputSchema:
    """Return the default schema with overrides applied.

    ``field_overrides`` entries carry ``name`` plus optional ``required``,
    ``kind``, and ``prompt_hint``; an existing field is patched in place, an
    unknown name extends the payload. Overrides from runtime parameters are
    untyped, so ``kind`` and ``required`` are validated here — a bad value
    must fail at spec normalization, not as a ``KeyError`` during prompt
    rendering after prepare has already run.
    """
    if not field_overrides:
        return DEFAULT_SCHEMA
    fields = {field.name: field for field in DEFAULT_SCHEMA.fields}
    for override in field_overrides:
        if not isinstance(override, dict) or not isinstance(override.get("name"), str):
            raise ValueError("finance output field overrides must be objects with a name")
        name = override["name"]
        current = fields.get(name)
        if current is None:
            current = FieldSpec(name)
        updates: dict[str, Any] = {}
        required = override.get("required")
        if required is not None:
            if not isinstance(required, bool):
                raise ValueError(
                    f"finance output field {name!r}: 'required' must be true or false, got {required!r}"
                )
            updates["required"] = required
        kind = override.get("kind")
        if kind is not None:
            kind = str(kind)
            if kind not in _FIELD_KINDS:
                raise ValueError(
                    f"finance output field {name!r}: 'kind' must be one of "
                    f"{', '.join(_FIELD_KINDS)}, got {kind!r}"
                )
            updates["kind"] = kind
        if override.get("prompt_hint") is not None:
            updates["prompt_hint"] = str(override["prompt_hint"])
        fields[name] = replace(current, **updates) if updates else current
    return replace(DEFAULT_SCHEMA, fields=tuple(fields.values()))


def output_schema_for_spec(parameters: dict[str, Any]) -> OutputSchema:
    """Resolve the schema bound to a normalized job spec's parameters."""
    declared = parameters.get("finance_output_fields")
    if isinstance(declared, list):
        return output_schema(declared)
    return output_schema()


# -- prompt rendering ----------------------------------------------------------

# Kinds accepted in field overrides; mirrors FinanceOutputField's Literal.
_FIELD_KINDS = ("string", "number", "list", "str_or_list")

_JSON_TYPES = {"string": "string", "number": "number", "list": "array", "str_or_list": ["string", "array"]}


def json_schema(schema: OutputSchema) -> dict[str, Any]:
    """Render the payload schema as a standard JSON Schema object for the prompt."""
    properties: dict[str, Any] = {}
    for field in schema.fields:
        spec: dict[str, Any] = {"type": _JSON_TYPES[field.kind]}
        if field.kind == "list" and not field.free_items:
            spec["items"] = {"type": "string"}
        if field.kind == "number" and field.value_range:
            spec["minimum"], spec["maximum"] = field.value_range
        if field.prompt_hint:
            spec["description"] = field.prompt_hint
        properties[field.name] = spec
    return {
        "type": "object",
        "required": [field.name for field in schema.fields if field.required],
        "properties": properties,
    }


# -- payload validation --------------------------------------------------------

@dataclass
class PayloadReview:
    hard_error: str | None = None
    warnings: list[str] = None  # type: ignore[assignment]
    metrics: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.warnings = [] if self.warnings is None else self.warnings
        self.metrics = {} if self.metrics is None else self.metrics


def validate_payload(schema: OutputSchema, payload: Any) -> PayloadReview:
    """Validate a produced payload against its schema; undeclared fields are free."""
    review = PayloadReview()
    if not isinstance(payload, dict):
        return PayloadReview(hard_error=f"Finance {schema.label} must be a JSON object.")
    missing = _check_fields(schema, payload, schema.label.title(), review)
    review.metrics[schema.metrics_key] = not missing
    return review


def _check_fields(schema: OutputSchema, obj: dict[str, Any], noun: str, review: PayloadReview) -> list[str]:
    missing: list[str] = []
    for field in schema.fields:
        if not field.required:
            continue
        value = obj.get(field.name)
        if value in (None, "", []):
            missing.append(field.name)
            continue
        warning = _check_kind(field, value)
        if warning:
            review.warnings.append(f"{noun} {warning}")
    if missing:
        review.warnings.append(f"{noun} missing: {', '.join(missing)}")
    return missing


def _check_kind(field: FieldSpec, value: Any) -> str | None:
    if field.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{field.name} is not numeric."
        if field.value_range and not field.value_range[0] <= float(value) <= field.value_range[1]:
            low, high = field.value_range
            return f"{field.name} must be between {low:g} and {high:g}."
    elif field.kind == "list":
        if not isinstance(value, list):
            return f"{field.name} must be a list."
    elif field.kind == "str_or_list":
        if not isinstance(value, (str, list)):
            return f"{field.name} must be a string or a list."
    elif not isinstance(value, str):
        return f"{field.name} must be a string."
    return None


def has_result(workspace_path: str) -> bool:
    """Whether the workspace holds a parseable result payload (for result ids)."""
    try:
        payload = json.loads((Path(workspace_path) / DEFAULT_SCHEMA.path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict)


__all__ = [
    "BRIEF_ARTIFACT_ID",
    "DEFAULT_SCHEMA",
    "FieldSpec",
    "OutputSchema",
    "PayloadReview",
    "RESULT_ID",
    "has_result",
    "json_schema",
    "output_schema",
    "output_schema_for_spec",
    "validate_payload",
]
