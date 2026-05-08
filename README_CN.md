[English](./README.md) ｜ 中文

<p align="center">
  <img src="./DataElf_logo.jpeg" alt="DataElf logo" width="320" />
</p>

# DataElf

DataElf 是一个面向大规模数据工作流的智能执行引擎。它将自然语言目标转化为可运行的流水线，自动执行内置工具，并在全过程中保持安全性、可追溯性与可扩展性。

开源版本面向需要一套统一框架来完成数据检查、安全扫描、质量评分、数据筛选和领域工具编排的团队，无需暴露私有数据处理基础设施。

[演示](#演示) |
[工具](#内置工具) |
[实验结果](#实验结果) |
[快速开始](#快速开始) |
[CLI](#cli-概览) |
[扩展](#扩展-dataelf)

## 演示

<div align="center">

https://github.com/user-attachments/assets/dd9038dd-660e-46bf-a06d-cdb76b254f27

*点击播放查看完整演示。*

</div>

## 核心能力

- 将自然语言需求转化为可执行流水线。
- 对明确任务运行单次执行模式，必要时进行轻量级澄清。
- 对复杂任务运行高自主性的 Pilot 循环，涵盖规划、执行、评审、修复与资产沉淀。
- 保留流水线、日志、报告和中间产物等执行痕迹，便于审查。
- 通过审批与提交机制支持可复用的稳定工作流资产。
- 支持通过自定义 `BaseTool` 实现扩展工具层。
- 在同一套 CLI 驱动的系统中整合数据安全检查与通用数据处理流程。

## 内置工具

DataElf 目前内置以下工具：

| 工具 | 功能 | 文档 |
| --- | --- | --- |
| `security_audit` | 数据集安全与风险扫描 | [security_audit_cn.md](docs/tools/security_audit_cn.md) |
| `data_scoring` | 样本级质量评分 | [data_scoring_cn.md](docs/tools/data_scoring_cn.md) |
| `data_select` | 基于预算或聚类的数据筛选 | [data_select_cn.md](docs/tools/data_select_cn.md) |
| `enzyme_acquire` | 酶信息检索工作流 | [enzyme_acquire_cn.md](docs/tools/enzyme_acquire_cn.md) |
| `protein_analyzer` | 蛋白质分析工作流 | [protein_analyzer_cn.md](docs/tools/protein_analyzer_cn.md) |
| `skillrl_skill_extraction` | 从轨迹数据中提取技能 | [skillrl_skill_extraction_cn.md](docs/tools/skillrl_skill_extraction_cn.md) |

## 实验结果

我们在标准基准上评估了 DataElf 核心工具的有效性，结果表明多维度整合分析在数据质量评估、安全审计和技能提取方面均具有显著优势。

### 数据价值评估

在 Alpaca-52k 数据集上，我们使用 9 个内置评分器对全部 52k 样本进行评分，每个评分器选出 9k 样本子集并微调 Qwen2.5-7B，最终在 AlpacaEval 2.0、MT-Bench 和 GSM8K 上进行评估。我们的复合评分器 **DataElf** 融合了 IFD 和 DEITA_Q 信号，以综合得分 98.7 名列第一，仅用不到原始数据 1/5 的训练量便超越了全量数据基线。单一维度评分器存在明显的筛选偏差——例如 `ask_llm` 偏好长文本对话，导致数学样本大量丢失，综合得分甚至低于全量数据基线。

<p align="center">
  <img src="./selected-vs-full.png" alt="数据评分：DataElf 与单一评分器对比" width="720" />
</p>

### 数据安全审计

我们构建了覆盖全部 13 类风险（每类约 100 个样本）的评测集，以召回率作为指标。DataElf 集成了 4 大类共 23 个检查器（规则匹配、LLM-as-a-Judge、专用模型、启发式分析），在 13 类风险上取得 **80.46%** 平均召回率——大幅领先最佳基线 DeepEval（48.62%）和 Qwen3Guard-Gen-8B（48.38%）。专用安全模型在其训练范围内表现优异，但在标签翻转、后门注入、事实不一致等域外风险上召回率骤降。

<p align="center">
  <img src="./recall_data_risk.png" alt="安全审计：13 类风险召回率对比" width="720" />
</p>

### 轨迹技能提取

在 RiOSWorld 基准上，将轨迹分析工具提取的技能注入 kimi-k2.5 后，整体安全率从 **32.17% 提升至 71.08%**，风险触发率降低一半——且无需修改模型参数。

<p align="center">
  <img src="./RiOSWorld_Skill_Safety.png" alt="轨迹技能提取 RiOSWorld 实验" width="540" />
</p>

## 安装

克隆仓库并安装：

```bash
git clone https://github.com/<your-org>/DataElf.git
cd DataElf
pip install -e .
```

如需在一个环境中使用所有内置工具，安装可选依赖组：

```bash
pip install -e ".[scitools,scoring,security_audit]"
```

安装完成后，CLI 入口为：

```bash
elf --help
```

## 配置

仓库根目录提供了一份公共示例配置 [`config.yaml`](config.yaml)，面向开源使用场景设计：

- LLM 凭据通过环境变量读取。
- 开源版本支持 `local_file` 和 `mock` 数据库策略。
- 示例配置列出了所有内置工具。

运行前设置环境变量：

```bash
export OPENAI_API_KEY="<your-api-key>"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export DATAELF_AGENT_MODEL="gpt-4o-mini"
export DATAELF_TOOL_LLM_MODEL="gpt-4o-mini"
```

默认配置使用 `./test_data` 下的本地 JSON 数据集，你可以将其替换为自己的数据集目录。

## 快速开始

执行单次任务：

```bash
elf run "run security_audit on security_audit_samples with a custom checker set" -c config.yaml --wait
```

执行评分任务：

```bash
elf run "score the alpaca_data dataset and summarize the highest-quality records" -c config.yaml --wait
```

运行高自主性 Pilot 循环：

```bash
elf pilot "screen alpaca_data for high-value and low-risk samples" -c config.yaml --wait --budget-steps 3
```

查看执行结果：

```bash
elf status <job_id>
elf result <job_id> --json --artifacts
elf inspect <job_id|candidate_id|asset_id> --json
```

审批并复用已沉淀的工作流资产：

```bash
elf approve <candidate_id>
elf submit <asset_id> -c config.yaml --wait
```

## CLI 概览

```bash
elf run "task" [--config PATH] [--wait] [--verbose]
elf pilot "task" [--config PATH] [--wait] [--budget-steps N] [--allow-experimental-tools]
elf status <job_id>
elf result <job_id> [--json] [--artifacts]
elf approve <candidate_id>
elf submit <asset_id> [--config PATH] [--wait]
elf inspect <job_id|candidate_id|asset_id> [--json]
```

## 执行模式

- `run`：针对相对明确的任务进行单次规划与执行。
- `pilot`：迭代式规划与修复循环，支持候选资产生成。
- `submit`：执行已审批的稳定流水线资产。

## 扩展 DataElf

通过实现 `BaseTool`、注册工具并在 `config.yaml` 中声明即可添加自定义工具。

最小示例：

```python
from typing import Any

from tools import BaseTool, ToolContext


class DomainRiskCheckTool(BaseTool):
    @property
    def name(self) -> str:
        return "domain_risk_check"

    @property
    def description(self) -> str:
        return "Check domain-specific risky patterns in dataset records."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "mode": {
                    "type": "string",
                    "default": "strict",
                },
            },
            "required": ["data"],
        }

    def run(self, context: ToolContext, **kwargs: Any) -> dict[str, Any]:
        data = kwargs.get("data", [])
        mode = kwargs.get("mode", "strict")
        checked = data
        return {
            "result": checked,
            "metadata": {
                "records_processed": len(data),
                "mode": mode,
            },
            "artifacts": {
                "report_md": "# Domain Risk Report\n",
            },
        }
```

详见开发者文档：

- [工具开发指南](docs/tool_development_guide.md)
- [工具规范](docs/tool_spec.md)
- [流水线 DSL 规范](docs/pipeline_dsl_spec.md)

## 项目结构

```text
DataElf/
├── ai_data_pilot/   # CLI 入口包
├── cli/             # 命令实现
├── agent/           # Agent 适配与提示构建
├── agentic/         # Pilot 循环与资产生命周期
├── runtime/         # 运行时执行层
├── tools/           # 内置工具
├── database/        # 开源数据库策略
├── config/          # 配置加载
├── docs/            # 用户与开发者文档
├── test_data/       # 本地示例数据集
└── config.yaml      # 公共示例配置
```
