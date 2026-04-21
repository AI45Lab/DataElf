# Data Scoring Tool

## Overview

`data_scoring` 工具为数据集中的每条样本分配质量分数，是后续数据筛选的前置环节。该工具支持可插拔的评分方法：用户可通过配置文件灵活切换评分器，也可在同一数据集上运行多个评分器以获取多维度的质量信号。

平台目前集成了 9 种评分器，覆盖不同的质量评估维度：

| 方法 | 说明 |
|------|------|
| `dataelf` | **默认推荐。** 将 `ifd` 和 `deita_q` 的分数分别转换为百分位排名后加权融合，为每条样本生成一个同时反映问答清晰度、准确度与推理难度的质量分数 |
| `ppl` | 从概率角度衡量训练数据对目标模型的可预测性：模型越觉得文本自然流畅，质量得分越高，反之得分越低则说明该样本对模型而言越不自然 |
| `norm_loss` | 与 `ppl` 思路相近，从信息压缩的角度衡量文本是否自然流畅 |
| `ifd` | 比较模型在"有指令"和"无指令"两种条件下生成同一回答的相对难度，指令帮助越大则认为该训练数据质量越高 |
| `deita_q` | 评估指令与回答是否清晰、准确 |
| `deita_c` | 评估指令的复杂度 |
| `deberta` | 通过在人工标注数据上训练所得的专用分类器，从文本连贯性、语法准确性等角度评估数据质量 |
| `fineweb_edu` | 聚焦于训练样本所包含的教育价值，例如是否具有清晰的讲解和结构化信息 |
| `ask_llm` | 通过直接询问大语言模型“该样本是否属于高质量数据”来进行质量评估 |

所有评分器的输出分数均被归一化到 0～5 分（越高越好）。`output` 字段为空的记录将被标记为无效数据（-1），并在下游的筛选环节中被剔除。

下图展示了在 Alpaca-52k 数据集上，使用各评分器分别筛选 9,000 条数据微调 `Qwen2.5-7B` 后的综合表现（AlpacaEval 2.0、MT-Bench、GSM8K 三个基准测试上的归一化均值结果）。实验显示 `DataElf` 仅用不到 1/5 的数据即超越了用全量数据微调的基线模型，大幅领先其他评分器。

![评分器对比实验](data_scoring_benchmark.png)

适用场景：

- 大规模训练数据的质量评估与清洗
- 不同评分器的有效性分析与对比

## Input Schema

工具目前接收 Alpaca 格式的数据，每条数据记录为一个 `dict`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `instruction` | `str` | 是 | 指令文本 |
| `input` | `str` | 否 | 补充输入（可为空字符串） |
| `output` | `str` | 是 | 回答文本（为空则标记为无效） |

单条输入示例：
```json
{
    "instruction": "Give three tips for staying healthy.",
    "input": "",
    "output": "1. Eat a balanced diet. 2. Exercise regularly. 3. Get enough sleep."
}
```


## Parameters

通过 `run_tool("data_scoring", ...)` 调用，以下为可传入的参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `data` | `list[dict]` | 是 | — | 待评分的数据集名称 |
| `scorer` | `str` | 否 | `dataelf` | 评分方法，可选：`dataelf`, `ask_llm`, `ppl`, `ifd`, `norm_loss`, `deita_q`, `deita_c`, `deberta`, `fineweb_edu` |
| `model` | `str` | 可在 `tools/scoring/defaults.yaml` 中配置 | — | 打分模型路径 |
| `batch_size` | `int` | 可在 `tools/scoring/defaults.yaml` 中配置 | — | 批处理大小 |
| `output_dir` | `str` | 否 | `outputs/scores/<scorer>/` | 评分结果保存目录 |


## Output

`run_tool("data_scoring", ...)` 返回评分后的数据记录列表（`list[dict]`），每条记录在原始字段基础上新增 `score` 字段：

```yaml
- instruction: str
  input: str
  output: str
  score: float                   # 质量分数（0-5），无效为 -1
```

## Algorithms

### DataElf 混合策略评分（默认）

`dataelf` 是平台默认且推荐的同名评分策略。其内部运行 `ifd` 和 `deita_q` 两个子评分器，将各自的分数转换为百分位排名后加权融合：

```
fused = alpha * rank(ifd) + (1 - alpha) * rank(deita_q)
```

- `alpha=0.5`（默认），两边的评估信号权重对等
- `ifd` 侧重于推理能力，`deita_q` 侧重于对话清晰度与准确度
- 输出缩放至 [0, 5] 以与其他评分器统一，方便结果比较

### 分数缓存机制

所有评分器的结果均自动缓存至 `outputs/scores/<scorer>/scored_data.json`。后续对同一数据集的重复调用可直接复用缓存，避免重复计算。

`dataelf` 会额外缓存子评分器结果至 `outputs/scores/ifd/` 和 `outputs/scores/deita_q/`，支持双向复用：
- 运行 `dataelf` → 缓存 `ifd` 和 `deita_q` → 后续单独运行 `ifd` 或 `deita_q` 复用缓存
- 先单独运行 `ifd` 和 `deita_q` → 后续运行 `dataelf` 复用两者缓存


## Example

### Pipeline DSL 示例 1：使用默认评分器
```python
log_step("Loading dataset")

data = load_dataset("alpaca_data")

log_step("Scoring data quality")

scored = run_tool(
    "data_scoring",
    data=data
)

log_step(f"Scored {len(scored)} records")

save_result(scored)
```

### Pipeline DSL 示例 2：指定评分方法
```python
log_step("Loading dataset")

data = load_dataset("alpaca_data")

log_step("Scoring data quality with IFD")

scored = run_tool(
    "data_scoring",
    data=data,
    scorer="ifd"
)

log_step(f"Scored {len(scored)} records")

save_result(scored)
```

### CLI 示例
```bash
# 使用默认评分器（dataelf）评分
elf run "score the alpaca dataset using dataelf" -c config.yaml -v

# 使用指定评分方法
elf run "score the alpaca data with ifd" -c config.yaml -v
```


## Configuration

默认参数在 `tools/scoring/defaults.yaml` 中配置，例如：

```yaml
dataelf:
  alpha: 0.5
  ifd_model: <model-path>
  ifd_batch_size: 256
  deita_q_model: <model-path>
  deita_q_batch_size: 256
  max_length: 512
```

## Dependencies

| 依赖 | 用途 | 是否必须 |
|------|------|----------|
| `torch` + `transformers` | 所有评分器的模型推理 | 是 |
| GPU | 加载模型 | 是 |
