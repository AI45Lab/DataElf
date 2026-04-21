# Tool README 规范

本文档定义Tool README的标准格式，以及它与Agent Prompt的集成方式。

## 文件位置

README文件统一放在`docs/tools/`目录下：

```
docs/tools/
├── security_audit_en.md
├── security_audit_zh.md
└── data_scoring_en.md
```

## 文件命名

**文件名必须与`BaseTool.name`保持一致的基础名称，可选追加语言后缀。**

例如：
- `SecurityAuditTool.name = "security_audit"` → 文件名可为`security_audit.md`、`security_audit_en.md`或`security_audit_zh.md`
- `DataScoringTool.name = "data_scoring"` → 文件名可为`data_scoring.md`、`data_scoring_en.md`或`data_scoring_zh.md`

### Config与README 的对应关系

```yaml
#config.yaml
tools:
  - security_audit          # 对应 docs/tools/security_audit*.md
  - data_scoring            # 对应 docs/tools/data_scoring*.md
  - data_select             # 对应 docs/tools/data_select*.md
```

Agent加载README时，会按工具名查找文档，并优先匹配语言后缀文件：

```python
for tool_name in config.tools:
    readme_path = resolve_tool_readme_path(tool_name)
```

## 必需章节

每个Tool README必须包含以下章节：

| 章节 | 说明 |
|------|------|
| `## Overview` | 工具用途、适用场景 |
| `## Parameters` |`run_tool()`的参数列表（必填/可选、类型、默认值） |
| `## Output` | 返回值结构，包含`result`、`metadata`、`artifacts` |
| `## Example` | 完整的Pipeline DSL代码示例 |

## 可选章节

根据需要添加：

| 章节 | 适用场景 |
|------|----------|
| `## Input Schema` | 如果工具有数据格式要求或期望字段 |
| `## Checkers` / `## Algorithms` / ... | 工具特有的功能模块说明 |
| `## Configuration` | config.yaml相关配置 |
| `## Dependencies` | 外部依赖（如GPU、特定模型） |



## 章节优先级（Agent Prompt截断）

当README过长时，Agent Prompt按以下优先级保留章节（数字越小越优先保留）：

1. `## Overview`
2. `## Input Schema`
3. `## Parameters`
4. `## Output`
5. `## Example`
6. 其他自定义章节（如`## Checkers`）
7. `## Configuration`

这意味着即使截断，核心的功能描述和参数信息也会保留。
