# SkillRL Skill Extraction

## Overview

`skillrl_skill_extraction` extracts a Claude-style skill bank from SkillRL-style trajectory memories. It currently supports three environments: `alfworld`, `search`, and `webshop`. The tool input must be the `data` object passed in by the Pipeline; it should not load data internally.

Typical use cases:

- Extract general skills, category-specific skills, and recurring error patterns from agent trajectory memory JSON
- Convert trajectory experience into structured outputs that can be reused for downstream analysis, distillation, and evaluation
- Compare successful strategies and failure patterns across different environments

## Input Schema

`data` is expected to be a `list[dict]`. Each record should ideally contain the following fields:

```python
{
  "tags": {
    "outcome": "Success" | "Failure",
    "data_source": "..."   # optional for the search domain
  },
  "content": {
    "task_meta": {
      "original_goal": "..."
    },
    "refined_trajectory": {
      "refined_trajectory": [
        {"action": "...", "reasoning": "..."}
      ]
    },
    "strategic_guidelines": {
      "planning_pattern": "...",
      "mistakes_to_avoid": ["...", "..."]
    }
  }
}
```

Records missing `tags.outcome` or `content.task_meta.original_goal` will be skipped, and a warning will be logged.

## Parameters

- `data`
  Type: `array[object]`
  Required: Yes
  Description: A list of SkillRL trajectory memories.
- `domain`
  Type: `string`
  Required: No
  Default: `"alfworld"`
  Allowed values: `"alfworld"`, `"search"`, `"webshop"`
  Description: Selects the extraction logic for the target environment.
- `llm_model`
  Type: `string`
  Required: No
  Default: `"o3"`
  Description: The model name passed to `context.llm.generate()`.
- `max_completion_tokens`
  Type: `integer`
  Required: No
  Default: `4096`
  Description: The maximum number of output tokens allowed for a single LLM call.

## Output

The return value follows the standard Tool output structure:

```python
{
  "result": {
    "general_skills": [...],
    "task_specific_skills": {...},   # alfworld / webshop
    "query_type_skills": {...},      # search
    "common_mistakes": [...],
    "metadata": {
      "source": "...",
      "total_memories_analyzed": 123,
      "..._distribution": {...}
    }
  },
  "metadata": {
    "tool_name": "skillrl_skill_extraction",
    "domain": "alfworld",
    "records_received": 123,
    "records_processed": 120,
    "records_skipped": 3,
    "llm_model": "o3"
  },
  "artifacts": {
    "report_md": "..."
  }
}
```

## Example

```python
log_step("Load SkillRL memories")
data = load_dataset(
    "skillrl_memories",
    filters={"domain": "alfworld"},
    limit=200,
)

skill_bank = run_tool(
    "skillrl_skill_extraction",
    data=data,
    domain="alfworld",
    llm_model="o3",
    max_completion_tokens=4096,
)

save_result(skill_bank)
```

## Configuration

This tool depends on `ToolContext.llm`. If runtime execution requires real LLM calls, configure `tool_llm` in `config.yaml`, or ensure the Tool runtime can fall back to the Agent LLM.
