from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


PLAN_VERSION = "dataelf_execution_plan_v1"
ALLOWED_OPS = {"load_dataset", "invoke_skill", "save_result", "write_file", "write_db", "log"}


class ExecutionPlanError(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionStep:
    id: str
    op: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionPlan:
    version: str
    steps: list[ExecutionStep]
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.raw

    def to_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False, indent=2)


def parse_execution_plan(plan: str | dict[str, Any]) -> ExecutionPlan:
    if isinstance(plan, str):
        try:
            raw = json.loads(plan)
        except json.JSONDecodeError as exc:
            raise ExecutionPlanError(f"Execution plan must be valid JSON: {exc}") from exc
    elif isinstance(plan, dict):
        raw = plan
    else:
        raise ExecutionPlanError(f"Execution plan must be a JSON string or dict, got {type(plan).__name__}")

    return validate_execution_plan(raw)


def validate_execution_plan(raw: dict[str, Any], available_skills: set[str] | None = None) -> ExecutionPlan:
    if not isinstance(raw, dict):
        raise ExecutionPlanError("Execution plan must be a JSON object.")
    if raw.get("version") != PLAN_VERSION:
        raise ExecutionPlanError(f"Unsupported execution plan version: {raw.get('version')!r}")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ExecutionPlanError("Execution plan must include a non-empty steps list.")

    declared: set[str] = set()
    steps: list[ExecutionStep] = []
    for index, step in enumerate(steps_raw):
        if not isinstance(step, dict):
            raise ExecutionPlanError(f"Step {index} must be an object.")
        step_id = step.get("id")
        op = step.get("op")
        if not isinstance(step_id, str) or not step_id:
            raise ExecutionPlanError(f"Step {index} must include a non-empty string id.")
        if step_id in declared:
            raise ExecutionPlanError(f"Duplicate step/output id: {step_id}")
        if op not in ALLOWED_OPS:
            raise ExecutionPlanError(f"Illegal op for step {step_id}: {op!r}")

        _validate_step_shape(step_id, op, step, available_skills)
        _validate_references(step_id, step, declared)

        output = step.get("output")
        if isinstance(output, str) and output:
            declared.add(output)
        declared.add(step_id)
        steps.append(ExecutionStep(id=step_id, op=op, raw=dict(step)))

    return ExecutionPlan(version=PLAN_VERSION, steps=steps, raw=raw)


def resolve_value(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in variables:
            raise ExecutionPlanError(f"Unknown variable reference: {value}")
        return variables[name]
    if isinstance(value, list):
        return [resolve_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, variables) for key, item in value.items()}
    return value


def _validate_step_shape(
    step_id: str,
    op: str,
    step: dict[str, Any],
    available_skills: set[str] | None,
) -> None:
    if op == "load_dataset":
        if not isinstance(step.get("dataset"), str) or not step["dataset"]:
            raise ExecutionPlanError(f"load_dataset step {step_id} requires dataset.")
        if "output" not in step:
            raise ExecutionPlanError(f"load_dataset step {step_id} requires output.")
    elif op == "invoke_skill":
        skill = step.get("skill")
        if not isinstance(skill, str) or not skill:
            raise ExecutionPlanError(f"invoke_skill step {step_id} requires skill.")
        if available_skills is not None and skill not in available_skills:
            raise ExecutionPlanError(f"Unknown skill for step {step_id}: {skill}")
        if "input" in step and not isinstance(step["input"], dict):
            raise ExecutionPlanError(f"invoke_skill step {step_id} input must be an object.")
        if "output" not in step:
            raise ExecutionPlanError(f"invoke_skill step {step_id} requires output.")
    elif op == "save_result":
        if "input" not in step:
            raise ExecutionPlanError(f"save_result step {step_id} requires input.")
    elif op == "write_file":
        if "input" not in step or not isinstance(step.get("path"), str):
            raise ExecutionPlanError(f"write_file step {step_id} requires input and path.")
    elif op == "write_db":
        if "input" not in step or not isinstance(step.get("table"), str):
            raise ExecutionPlanError(f"write_db step {step_id} requires input and table.")
    elif op == "log":
        if not isinstance(step.get("message"), str):
            raise ExecutionPlanError(f"log step {step_id} requires message.")


def _validate_references(step_id: str, value: Any, declared: set[str]) -> None:
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in declared:
            raise ExecutionPlanError(f"Step {step_id} references unknown variable {value}.")
    elif isinstance(value, list):
        for item in value:
            _validate_references(step_id, item, declared)
    elif isinstance(value, dict):
        for item in value.values():
            _validate_references(step_id, item, declared)
