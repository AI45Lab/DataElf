# Data Select Tool

## Overview

`data_select` 工具负责在评分完成后从候选数据中选取指定数量的高质量子集，通常作为 `data_scoring` 的下游环节使用。

仅按分数排序取前 K 条数据容易导致所选样本高度同质，缺乏主题和难度上的多样性。为此，筛选工具先使用嵌入模型（` Llama-3.1-8B-Instruct `）提取每条样本的语义向量，再对这些向量进行 K-means 聚类并将总筛选配额按簇大小比例分配到各簇当中，最后在每个簇内按分数降序选取。这一策略既保留了所筛选数据的多样性，又确保了所选子集在每个数据簇内的高质量。

适用场景：

- 多样性约束下的高质量训练数据定额筛选


## Input Schema

工具目前需接收已评分的 Alpaca 格式数据记录列表，每条记录必须包含 `score` 字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `instruction` | `str` | 是 | 指令文本 |
| `input` | `str` | 否 | 补充输入 |
| `output` | `str` | 是 | 回答文本 |
| `score` | `float` | 是 | 质量分数 |

单条输入示例：
```json
{
    "instruction": "Give three tips for staying healthy.",
    "input": "",
    "output": "1. Eat a balanced diet. 2. Exercise regularly. 3. Get enough sleep.",
    "score": 3.72
}
```


## Parameters

通过 `run_tool("data_select", ...)` 调用，以下为可传入的参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `data` | `list[dict]` | 是 | — | 已评分的数据记录列表（必须含 `score` 字段） |
| `dataset_name` | `str` | 是 | — | 数据集名称，用于组织嵌入缓存，建议与 `load_dataset()` 中使用的名称一致 |
| `budget` | `int` | 是 | — | 目标筛选样本数 |
| `strategy` | `str` | 否 | `proportional` | 配额分配策略：`proportional`（按簇大小比例分配）、`uniform`（均匀分配） |
| `output_dir` | `str` | 是 | — | 结果保存目录，如 `outputs/<dataset_name>_data_<budget>` |


## Output

`run_tool("data_select", ...)` 返回筛选后的数据记录列表（`list[dict]`），保留原始字段和评分：

```yaml
- instruction: str
  input: str
  output: str
  score: float
```

## Algorithms

### 基于多样性感知的高效筛选策略

1. **过滤无效记录**：排除 score < 0 的样本
2. **加载或提取嵌入**：使用嵌入模型（默认 `Llama-3.1-8B-Instruct`）提取每条样本的语义向量，支持自动缓存
3. **K-means 聚类**：对嵌入向量进行聚类，将数据划分为语义子群
4. **配额分配**：按策略将总选取配额分配到各簇
   - `proportional`：按簇大小比例分配，簇越大则选取越多样本（默认推荐策略）
   - `uniform`：均匀分配，每个簇等量选取
5. **簇内 TopK 选取**：在每个簇内按分数降序选取

### 嵌入缓存机制

嵌入向量缓存至 `outputs/embeddings/<dataset_name>/embeddings_<N>.npy`，其中 `<dataset_name>` 对应 `load_dataset()` 中使用的数据集名称，`<N>` 为所提取的总记录数。后续对同一数据集的运行可直接复用缓存，无需重新提取。

**重要：** 请在 pipeline 中始终传入 `dataset_name` 参数以确保缓存命中的一致性。


## Example

### Pipeline DSL 示例：评分后筛选
```python
log_step("Loading dataset")

data = load_dataset("alpaca_data")

log_step("Scoring data quality")

scored = run_tool(
    "data_scoring",
    data=data,
    scorer="dataelf"
)

log_step(f"Scored {len(scored)} records")

log_step("Running diversity-aware selection")

selected = run_tool(
    "data_select",
    data=scored,
    dataset_name="alpaca_data",
    budget=500,
    n_clusters=100,
    strategy="proportional",
    output_dir="outputs/alpaca_data_500"
)

log_step(f"Selected {len(selected)} records")

save_result(selected)
```

### CLI 示例
```bash
# 端到端评分 + 筛选
elf run "score the alpaca data with dataelf, then give me the best 50" \
  -c config.yaml -v
```


## Configuration

默认参数在 `tools/select/defaults.yaml` 中配置：

```yaml
budget: 9000
n_clusters: 100
strategy: proportional

embeddings_dir: outputs/embeddings
embedding_model: <path-to-embedding-model>
embedding_batch_size: 64
embedding_max_length: 1024
embedding_device: cuda
embedding_dtype: bfloat16

kmeans:
  backend: sklearn
  max_iter: 100
  n_init: 3
```

## Dependencies

| 依赖 | 用途 | 是否必须 |
|------|------|----------|
| `torch` + `transformers` | 嵌入提取 | 无缓存时必须 |
| GPU | 加速嵌入提取与聚类 | 无缓存时必须 |
| `numpy` | 嵌入存储与计算 | 是 |
| `scikit-learn` | K-means 聚类 | 是 |
