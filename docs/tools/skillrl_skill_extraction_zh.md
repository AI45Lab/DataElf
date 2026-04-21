# SkillRL Skill Extraction

## Overview

`skillrl_skill_extraction` 从 SkillRL 风格的轨迹记忆中提取 Claude-style skill bank，当前支持 `alfworld`、`search`、`webshop` 三类环境。工具输入必须是 Pipeline 传入的 `data`，不能在 Tool 内部自行加载数据。

适用场景：

- 从 Agent 轨迹产生的 memory JSON 中提炼通用技能、类别技能、常见错误模式
- 将轨迹经验整理成后续分析、蒸馏、评测可复用的结构化结果
- 对比不同环境下 agent 的成功策略和失败模式

## Input Schema

`data` 期望为 `list[dict]`，每条记录建议包含这些字段：

```python
{
  "tags": {
    "outcome": "Success" | "Failure",
    "data_source": "..."   # search 域可选
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

缺少 `tags.outcome` 或 `content.task_meta.original_goal` 的记录会被跳过，并在日志中给出 warning。

## Parameters

- `data`:
  类型：`array[object]`
  必填：是
  说明：SkillRL 轨迹记忆列表。
- `domain`:
  类型：`string`
  必填：否
  默认值：`"alfworld"`
  可选值：`"alfworld"`, `"search"`, `"webshop"`
  说明：选择对应环境的经验提取逻辑。
- `llm_model`:
  类型：`string`
  必填：否
  默认值：`"o3"`
  说明：传给 `context.llm.generate()` 的模型名。
- `max_completion_tokens`:
  类型：`integer`
  必填：否
  默认值：`4096`
  说明：单次 LLM 调用的最大输出 token 数。

## Output

返回值遵循 Tool 标准结构：

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

该工具依赖 `ToolContext.llm`。如果在运行时需要真实 LLM 调用，请在 `config.yaml` 中配置 `tool_llm`，或保证 Tool 运行时可以回退到 Agent LLM。
