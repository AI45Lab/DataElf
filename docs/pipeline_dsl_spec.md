# Pipeline DSL Specification

## Overview

Pipeline DSL is the task description language used in DataElf to bridge
user intent and tool execution.

## Available Functions

### load_dataset

```python
data = load_dataset(name: str, filters: dict = None, limit: int = None, columns: list = None) -> list[dict]
```

Load data from the configured database. Returns `list[dict]`.

### run_tool

```python
result = run_tool(tool_name: str, **kwargs) -> Any
```

Execute a registered tool. `kwargs` are forwarded to the tool's `run()` method.
The runtime automatically extracts and returns the tool's `result` field.

### save_result

```python
save_result(result: Any) -> None
```

Persist the final pipeline output.

### log_step

```python
log_step(message: str) -> None
```

Log a pipeline execution step.

### write_file

```python
write_file(data: Any, path: str) -> None
```

Write data to a local JSON file.

### write_db

```python
write_db(data: Any, table: str) -> None
```

Write data to a database table.

## Data Format

All pipeline data uses `list[dict]` format:

```python
[
  {"id": 1, "name": "example", "value": 100},
  {"id": 2, "name": "example2", "value": 200}
]
```

## Example

```python
data = load_dataset("companies")

result = run_tool(
    "security_audit",
    data=data,
    checker_names=["PIIRule", "PromptInjectionLLMJudge"]
)

save_result(result)
```

## Rules

1. Tools must receive data via the `data` parameter — they cannot load data internally.
2. Direct database access inside tools is forbidden.
3. Data must flow through the pipeline between steps.
