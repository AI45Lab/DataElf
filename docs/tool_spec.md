# Tool Specification

## Overview

A Tool is a pluggable component in DataElf. Each tool is responsible for
a specific data-processing task.

## Tool Interface

### Required Properties

```python
@property
def name(self) -> str:
    pass  # Unique identifier (snake_case)

@property
def description(self) -> str:
    pass  # Functionality description

@property
def parameters(self) -> dict:
    pass  # JSON Schema for parameters
```

### Required Method

```python
def run(self, context: ToolContext, **kwargs) -> dict[str, Any]:
    pass
```

## Input

Tools must receive `list[dict]` data through the `data` keyword argument:

```python
def run(self, context: ToolContext, **kwargs) -> dict:
    data = kwargs.get("data", [])  # list[dict]
```

## Output

Tools must return a dict containing at least a `result` key:

```python
{
    "result": Any,            # Primary result
    "metadata": dict,         # Optional: execution metadata
    "artifacts": dict         # Optional: reports, files, etc.
}
```

## Context

Tools can access the following through `context`:

| Attribute | Type      | Description    |
|-----------|-----------|----------------|
| `job_id`  | str       | Job identifier |
| `logger`  | JobLogger | Logger         |
| `config`  | dict      | Configuration  |

Direct database access through `context` is **forbidden**. Data must be passed
in via the pipeline.

## Logging

Use `context.log()` to record log messages:

```python
context.log("Processing started", "info")
context.log("Warning message", "warning")
context.log("Error occurred", "error")
```

## Example

```python
class MyTool(BaseTool):
    @property
    def name(self) -> str:
        return "my_tool"

    @property
    def description(self) -> str:
        return "Process data"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Data records"
                }
            },
            "required": ["data"]
        }

    def run(self, context: ToolContext, **kwargs) -> dict:
        data = kwargs.get("data", [])
        context.log(f"Processing {len(data)} records", "info")

        result = process(data)

        return {"result": result}
```
