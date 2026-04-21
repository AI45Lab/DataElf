# Tool开发指南

本文档描述如何为 DataElf 开发自定义 Tool。

## 1. BaseTool标准接口

所有Tool必须继承 `BaseTool` 并实现以下属性和方法：

### 1.1 必须实现的属性

```python
from tools import BaseTool, ToolContext

class MyTool(BaseTool):

    @property
    def name(self) -> str:
        """Tool的唯一标识符。"""
        return "my_tool_name"

    @property
    def description(self) -> str:
        """Tool功能描述，用于 Agent 理解工具能力。"""
        return "工具功能的简要描述"

    @property
    def parameters(self) -> dict[str, Any]:
        """参数定义，使用JSON Schema格式（OpenAI Function风格）。"""
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "数据记录列表"
                }
            },
            "required": ["data"]
        }
```

### 1.2 必须实现的方法

```python
    @abstractmethod
    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        """执行Tool逻辑。

        Args:
            context: 执行上下文，包含logger、config（NO database access）
            **kwargs: Tool参数（来自parameters schema）

        Returns:
            必须返回包含以下键的字典：
            {
                "result": Any,        # 主要结果值
                "metadata": dict,     # 元数据
                "artifacts": dict      # 生成的副产品（报告、文件等）
            }
        """
        pass
```

---

## 2. Execution Context

`context` 参数由Runtime提供，包含以下属性：

```python
context = {
    "job_id": str,              # 任务 ID
    "logger": JobLogger,        # 日志记录器
    "config": dict[str, Any]    # 配置信息
}
```

**注意**: Tool不能通过 `context` 直接访问数据库。

### 2.1 使用 Logger

```python
# 记录信息日志
context.log("处理开始", "info")

# 记录错误
context.log("处理失败", "error")

# 记录警告
context.log("数据量较小", "warning")
```

### 2.2 数据传递

**正确的方式** - Tool接收 `data` 参数：

```python
def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
    data = kwargs.get("data", [])  # 接收实际数据

    context.log(f"Processing {len(data)} records")

    # 处理数据
    result = process(data)

    return {
        "result": result,
        "metadata": {"records_count": len(data)},
    }
```

**错误的方式** - Tool内部加载数据：

```python
# 禁止：Tool内部加载数据
def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
    # 不要这样做！数据应该由Pipeline加载后传入
    dataset_name = kwargs.get("dataset")
    data = context.read_data(table_name=dataset_name)
```

### 2.3 Pipeline示例

正确的Pipeline写法：

```python
# Pipeline加载数据
data = load_dataset("data_pilot_test", filters={"dataset_type": "RL"}, limit=100)

# 将数据传递给Tool
result = run_tool("my_tool", data=data)

# 保存结果
save_result(result)
```

---

## 3. Tool 返回值标准

Tool必须返回符合以下结构的字典：

```python
{
    "result": Any,           # [必需]主要执行结果
    "metadata": {             # [可选]执行元数据
        "records_processed": int,  #处理的行数
        "duration_ms": int,    # 执行耗时
        ...
    },
    "artifacts": {            # [可选] 生成的副产品
        "report_md": str,     # Markdown 格式报告
        "chart_data": dict,    # 图表数据
        "output_file": str,  # 生成的文件路径
        ...
    }
}
```

---

## 4. Tool命名规范

### 4.1 文件命名

格式：`{tool_name}_tool.py`

示例：
- `security_audit_tool.py`
- `trajectory_analysis_tool.py`
- `post_train_value_tool.py`

### 4.2 类名命名

格式：`{ToolName}Tool`（PascalCase）

示例：
- `SecurityAuditTool`
- `DataScoringTool`
- `ProteinAnalyzerTool`

### 4.3 name属性

格式：snake_case

示例：
- `security_audit`
- `data_scoring`
- `protein_analyzer`

---

## 5. Tool开发注意事项

### 5.1 禁止的操作

```python
# 禁止：直接访问CLI
import click  # 不要导入

# 禁止：Tool内部访问数据库
def run(self, context, **kwargs):
    data = context.read_data(...)  # NO!
    data = context.database.read_table(...)  # NO!

# 禁止：接收dataset名称而非实际数据
def run(self, context, dataset: str):  # NO!
    pass

# 禁止：写死文件路径
output_path = "/fixed/path/output.txt"  # 使用相对路径或配置
```

### 5.2 推荐的做法

```python
# 推荐：接收data参数
def run(self, context, **kwargs):
    data = kwargs.get("data", [])  #YES!
    result = process(data)
    return {"result": result}

# 推荐：输入输出可序列化
def run(self, context, **kwargs):
    data = some_processing(kwargs["data"])
    return {
        "result": data,
        "metadata": {"count": len(data)},
        "artifacts": {"summary": f"Processed {len(data)} items"}
    }

# 推荐：逻辑模块化
def _calculate_score(items: list) -> float:
    return sum(i.score for i in items) / len(items)

def run(self, context, **kwargs):
    score = _calculate_score(kwargs["data"])
    return {"result": score}

# 推荐：使用logger在关键步骤做好日志记录
def run(self, context, **kwargs):
    context.log("开始处理", "info")
    try:
        result = process_data(kwargs["data"])
        context.log(f"处理完成，结果: {result}", "info")
        return {"result": result}
    except Exception as e:
        context.log(f"处理失败: {e}", "error")
        raise
```

---

## 6. 完整示例

### 示例：见tools/example_tools.py


## 7. Tool注册

开发完成后，需要在CLI中注册Tool：

### 7.1 修改 `cli/common.py`

```python
_TOOL_MODULES = [
    ("tools.security_audit.tool", "SecurityAuditTool", "security_audit"),
    ("tools.scoring.data_scoring_tool", "DataScoringTool", "data_scoring"),
    ("tools.select.data_select_tool", "DataSelectTool", "data_select"),
    ("your.module", "MyTool", "my_tool_name"),
]
```

### 7.2 修改config.yaml

```yaml
tools:
  - security_audit
  - data_scoring
  - data_select
  - my_tool_name  # 添加Tool
```

---

## 8. 支持的输出类型

| 类型 | 用途 | 示例 |
|------|------|------|
| `float` | 分数、评分 | `0.85` |
| `string` | 文本结果 | `"Processing completed"` |
| `dict` | 结构化结果 | `{"score": 85, "count": 100}` |
| `list` | 结果列表 | `[item1, item2, item3]` |
| `artifacts.report_md` | Markdown 报告 | `# Report\n## Summary` |
| `artifacts.chart_data` | 图表数据 | `{"labels": [...], "values": [...]}` |
| `artifacts.output_file` | 文件路径 | `"/path/to/output.csv"` |

---

## 9. 测试Tool

### 9.1 单元测试
参考test/tools/example_tools_test.py

### 9.2 集成测试(TBD)
...

---

## 10. 常见问题

### Q: 如何处理大量数据？

A: 建议分块处理，并记录进度：

```python
def run(self, context, **kwargs):
    data = kwargs["data"]
    chunk_size = 1000
    results = []

    for i in range(0, len(data), chunk_size):
        chunk = data[i:i+chunk_size]
        result = process_chunk(chunk)
        results.append(result)
        context.log(f"Processed {i+len(chunk)}/{len(data)}")

    return {"result": combine_results(results)}
```

### Q: 如何返回文件？

A: 将文件保存到可配置的路径：

```python
import json
from pathlib import Path

def run(self, context, **kwargs):
    output_dir = Path(context.config.get("output_dir", "./artifacts"))
    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / f"result_{context.job_id}.json"
    with open(output_file, "w") as f:
        json.dump(result, f)

    return {
        "result": "saved",
        "artifacts": {"output_file": str(output_file)}
    }
```

### Q: 如何处理错误？

A: 使用try-except并记录错误：

```python
def run(self, context, **kwargs):
    try:
        data = kwargs.get("data", [])
        result = process_data(data)
        return {"result": result}
    except ValueError as e:
        context.log(f"Invalid input: {e}", "error")
        return {"result": None, "metadata": {"error": str(e)}}
    except Exception as e:
        context.log(f"Unexpected error: {e}", "error")
        raise
```

---

## 11. Checklist

开发完成前确认：

- [ ] 必须有注释但只写逻辑清晰明了的关键性注释 切忌小作文注释
- [ ] Tool类名符合PascalCase命名规范
- [ ] 文件名符合 `{tool_name}_tool.py` 格式
- [ ] name属性使用蛇形命名
- [ ] description清晰描述Tool功能
- [ ] parameters定义完整（type、properties、required）
- [ ] **Tool接收 `data` 参数，而非 `dataset` 名称**
- [ ] **Tool不直接访问主数据库**
- [ ] run()返回包含result的字典
- [ ] 适当使用context.log()记录日志
- [ ] 处理异常情况
- [ ] 添加单元测试
- [ ] 更新config.yaml添加工具名
- [ ] 在cli/run.py中注册Tool
