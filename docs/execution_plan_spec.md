# DataElf Execution Plan Specification

DataElf uses a structured JSON execution plan as its controlled execution surface.
The planner does not emit Python code. It selects skills and composes safe runtime
operations that DataElf validates before execution.

## Plan Shape

```json
{
  "version": "dataelf_execution_plan_v1",
  "steps": []
}
```

Each step must have:

- `id`: unique step id.
- `op`: one supported operation.

Step outputs become variables. Later steps can reference them with `$name`.

## Operations

### `load_dataset`

Load a configured dataset.

Required fields:

- `id`
- `op`
- `dataset`
- `output`

Optional fields:

- `filters`
- `limit`
- `columns`

### `invoke_skill`

Invoke a discovered skill.

Required fields:

- `id`
- `op`
- `skill`
- `input`
- `output`

`skill` must exist in `SkillRegistry`. `input` is an object whose values may include
variable references from prior steps.

### `save_result`

Persist the final result to the job.

Required fields:

- `id`
- `op`
- `input`

### `write_file`

Write JSON data to a relative file path.

Required fields:

- `id`
- `op`
- `input`
- `path`

### `write_db`

Write records to a configured database table.

Required fields:

- `id`
- `op`
- `input`
- `table`

### `log`

Record a progress message.

Required fields:

- `id`
- `op`
- `message`

## Example

```json
{
  "version": "dataelf_execution_plan_v1",
  "steps": [
    {
      "id": "load_data",
      "op": "load_dataset",
      "dataset": "security_audit_samples",
      "output": "data"
    },
    {
      "id": "audit",
      "op": "invoke_skill",
      "skill": "security_audit",
      "input": {
        "data": "$data",
        "checker_names": ["PIIRule", "SecretRule"]
      },
      "output": "audit_result"
    },
    {
      "id": "save",
      "op": "save_result",
      "input": "$audit_result"
    }
  ]
}
```
