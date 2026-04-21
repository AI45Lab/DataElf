# protein_analyzer

## Overview

基于 **BioPython ProteinAnalysis** 的蛋白质序列理化性质分析工具，本地计算，可选接入 **NCBI BLAST** 进行同源序列检索。支持多种输入格式，输出标准化 Parquet 表格并返回批次级分析摘要。

适用场景：

- 批量计算蛋白质分子量、等电点、稳定性、疏水性
- 二级结构比例估算（α-螺旋 / β-折叠 / 转角）
- 氨基酸组成统计
- 可选 BLAST 同源搜索（需要网络，约 30–60 秒/条）
- 作为 `enzyme_acquire` 的下游工具，直接接收其 Parquet 输出文件路径作为输入

## Input Schema

`data` 字段接受**对象列表**或来自上游 `enzyme_acquire` 调用的**文件路径字符串**：

### Option A — 对象列表

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | `string` | ✅ | 蛋白质唯一标识（如 UniProt ID）|
| `sequence` | `string` | ✅ | 单字母氨基酸序列（大小写均可，自动 uppercase）|
| `protein_name` | `string` | ❌ | 蛋白质名称，透传至输出 |

序列约束：最短 10 个氨基酸；仅允许标准单字母代码 `ACDEFGHIKLMNPQRSTVWYX`（X = 未知）；批次内按 `id` 自动去重。

### Option B — 文件路径字符串（与 enzyme_acquire 串联）

将 `enzyme_acquire` 调用结果中的 `result["artifacts"]["output_file"]` 直接作为 `data` 传入，工具自动读取 Parquet 文件并提取 `sequence` 列。

**输入示例（Option A）：**

```json
[
  {
    "id": "P00533",
    "sequence": "MRPSGTAGAALLALLAALCPASRALEEKKVCQGTSNKLTQLGTFEDHFLSLQRMFNN...",
    "protein_name": "Epidermal growth factor receptor"
  }
]
```

**输入示例（Option B — 串联）：**

```python
enzyme_result = run_tool("enzyme_acquire", data=["P00533"], fetch_smiles=False)
result = run_tool(
    "protein_analyzer",
    data=enzyme_result["artifacts"]["output_file"],  # 直接传文件路径
    run_blast=False,
)
```

## Parameters

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `data` | `list[dict]` 或 `str` | ✅ | — | 序列列表，或 `enzyme_acquire` 输出的 Parquet 文件路径 |
| `run_blast` | `bool` | ❌ | `false` | 是否运行 NCBI BLAST 同源搜索（需要网络）|

## Output

### result

#### 统计字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `n_records` | `int` | 成功分析的记录总数 |
| `n_errors` | `int` | 分析过程抛出异常的记录数 |
| `n_skipped` | `int` | 输入校验阶段过滤掉的记录数（序列过短、非法字符、重复 ID）|
| `columns` | `list[str]` | 输出表格的列名列表 |
| `errors` | `list[dict]` | 失败记录详情（含 `id`、`error`、`traceback`）|

#### 分析摘要（summary）

| 字段 | 类型 | 说明 |
|------|------|------|
| `n_local_success` | `int` | BioPython 本地分析成功的记录数 |
| `n_local_error` | `int` | 本地分析失败的记录数 |
| `pct_stable` | `float` | 稳定蛋白占比（%）。instability index < 40 视为稳定，该指数基于二肽组成计算 |
| `mean_seq_length` | `float` | 平均序列长度（氨基酸残基数）|
| `mean_mw` | `float` | 平均分子量（Da）。典型范围：小蛋白 < 30 kDa，大蛋白 > 100 kDa |
| `mean_pI` | `float` | 平均等电点（pH）。pI < 7 为酸性蛋白，pI > 7 为碱性蛋白，影响蛋白在不同 pH 下的溶解性 |
| `mean_gravy` | `float` | 平均 GRAVY 值（Grand Average of Hydropathicity）。负值表示亲水，正值表示疏水（多见于膜蛋白）|
| `n_blast_success` | `int` | BLAST 命中成功的记录数（仅 `run_blast=true` 时存在）|
| `n_blast_hits_total` | `int` | 所有记录的 BLAST 命中总数（仅 `run_blast=true` 时存在）|

**输出示例（`run_blast=false`，输入为 EGFR P00533）：**

```json
{
  "result": {
    "n_records": 1,
    "n_errors": 0,
    "n_skipped": 0,
    "columns": [
      "uniprot_id", "protein_name", "seq_length", "mw", "pI",
      "instability", "is_stable", "gravy", "helix_frac", "turn_frac",
      "sheet_frac", "aa_composition", "local_status",
      "n_blast_hits", "top_hit_id", "top_identity", "top_evalue",
      "top_title", "blast_status"
    ],
    "summary": {
      "n_local_success": 1,
      "n_local_error": 0,
      "pct_stable": 0.0,
      "mean_seq_length": 1210.0,
      "mean_mw": 134276.04,
      "mean_pI": 6.26,
      "mean_gravy": -0.316
    },
    "errors": []
  },
  "metadata": {
    "status": "success",
    "records_processed": 1,
    "n_errors": 0,
    "blast_enabled": false,
    "duration_ms": 70
  },
  "artifacts": {
    "output_file": "/tmp/sdc_outputs/bio/protein_analysis.parquet"
  }
}
```

> `aa_composition` 为各氨基酸占比字典，示例：`{"A": 0.072, "C": 0.018, "D": 0.053, ...}`（共 20 种标准氨基酸，省略展示）。

### metadata

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | `str` | `success` \| `partial_success` |
| `records_processed` | `int` | 同 `n_records` |
| `n_errors` | `int` | 同 `n_errors` |
| `blast_enabled` | `bool` | 本次运行是否开启 BLAST |
| `duration_ms` | `float` | 工具总运行时间（毫秒）|

### artifacts

| 字段 | 类型 | 说明 |
|------|------|------|
| `output_file` | `str` | 输出 Parquet 文件绝对路径 |

### 输出表格 Schema

```
uniprot_id | protein_name | seq_length | mw | pI | instability | is_stable | gravy
helix_frac | turn_frac | sheet_frac | aa_composition | local_status
n_blast_hits | top_hit_id | top_identity | top_evalue | top_title | blast_status
```

### 状态码说明

| status | 含义 |
|--------|------|
| `success` | 所有记录分析成功，`errors` 为空列表 |
| `partial_success` | 部分记录抛出异常，但仍有可用结果写入文件 |
| `error` | 无任何可用结果（输入加载失败 / 无有效序列 / 全部失败）|

| error code | 含义 |
|------------|------|
| `LOAD_FAILED` | 输入加载失败（文件不存在、格式不支持等）|
| `NO_SEQUENCES` | 校验后无任何有效序列 |
| `ALL_FAILED` | 所有序列分析均抛出异常 |

## Example

```python
# 基础用法：本地理化分析，跳过 BLAST
result = run_tool(
    "protein_analyzer",
    data=[
        {"id": "P00533", "sequence": "MRPSGTAGAA...", "protein_name": "EGFR"},
        {"id": "P68871", "sequence": "MVHLTPEEKS..."},
    ],
    run_blast=False,
)

# 分析摘要
summary = result["result"]["summary"]
print(f"稳定蛋白占比: {summary['pct_stable']}%")   # instability index < 40 为稳定
print(f"平均分子量: {summary['mean_mw']} Da")
print(f"平均等电点: {summary['mean_pI']}")           # < 7 偏酸性，> 7 偏碱性
print(f"平均疏水性: {summary['mean_gravy']}")        # 负值亲水，正值疏水

# partial_success 时查看失败详情
if result["metadata"]["status"] == "partial_success":
    for err in result["result"]["errors"]:
        print(f"[{err['id']}] {err['error']}")

# 与 enzyme_acquire 串联——直接传入输出文件路径
enzyme_result = run_tool("enzyme_acquire", data=["1.1.1.1"], fetch_smiles=False)
result = run_tool(
    "protein_analyzer",
    data=enzyme_result["artifacts"]["output_file"],  # 直接传文件路径
    run_blast=False,
)
```

## Dependencies

- 本地计算：`biopython`（`ProteinAnalysis`，无网络需求）
- BLAST：需要网络访问 NCBI BLAST API，每条序列约 30–60 秒
- 内部模块：`tools.scitools.sources.biopython_protein`（`analyze_sequence`）、`tools.scitools.sources.blast`（`run_blast`）