from __future__ import annotations

from copy import deepcopy
from typing import Any

from tools.base_tool import BaseTool, ToolContext
from tools.tool_registry import get_global_registry


class CompositeDerivedTool(BaseTool):
    def __init__(self, manifest: dict[str, Any]):
        self.manifest = deepcopy(manifest)

    @property
    def name(self) -> str:
        return self.manifest["name"]

    @property
    def description(self) -> str:
        return self.manifest.get("description", "Composite derived tool.")

    @property
    def parameters(self) -> dict[str, Any]:
        return self.manifest.get(
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Input data records.",
                    }
                },
                "required": ["data"],
            },
        )

    def usage_example(self) -> str:
        return self.manifest.get("usage_example", f'run_tool("{self.name}", data=data)')

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        registry = get_global_registry()
        env: dict[str, Any] = {"input": kwargs}

        for step in self.manifest.get("steps", []):
            step_type = step.get("type", "run_tool")
            if step_type != "run_tool":
                raise ValueError(f"Unsupported composite step type: {step_type}")

            tool_name = step["tool_name"]
            if tool_name == self.name:
                raise ValueError("Composite tool cannot recursively invoke itself")

            tool = registry.get(tool_name)
            if tool is None:
                raise ValueError(f"Composite tool step references unknown tool: {tool_name}")

            raw_kwargs = step.get("kwargs", {})
            resolved_kwargs = _resolve_value(raw_kwargs, env)
            tool.validate_parameters(**resolved_kwargs)
            output = tool.run(context, **resolved_kwargs)
            step_output = output.get("result") if isinstance(output, dict) else output
            env[step.get("output", tool_name)] = step_output

        result_spec = self.manifest.get("result", "$input")
        result = _resolve_value(result_spec, env)
        return {
            "result": result,
            "metadata": {
                "candidate_id": self.manifest.get("candidate_id", ""),
                "derived_type": self.manifest.get("candidate_type", "composite_tool"),
                "source_attempts": self.manifest.get("source_attempts", []),
            },
            "artifacts": {
                "manifest": deepcopy(self.manifest),
            },
        }


def _resolve_value(value: Any, env: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_ref(value[1:], env)
    if isinstance(value, list):
        return [_resolve_value(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(item, env) for key, item in value.items()}
    return value


def _resolve_ref(ref: str, env: dict[str, Any]) -> Any:
    current: Any = env
    for part in ref.split("."):
        if isinstance(current, dict):
            current = current[part]
        else:
            current = getattr(current, part)
    return current
