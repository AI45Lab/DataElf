# enzyme_acquire

## Overview

跨数据库酶属性检索工具，从 **UniProt**、**KEGG**、**PubChem** 三个来源并发拉取蛋白质序列、催化反应、底物 SMILES 及代谢通路信息，将结果整合为固定 Schema 的 Parquet/CSV 表格，并随响应返回批次级分析摘要。

适用场景：

- 按 EC 号或酶名称批量获取酶功能注释
- 按 UniProt ID 检索蛋白质序列与代谢反应信息
- AI4Science 管线中底物 SMILES 的自动补全
- 混合格式批量查询，支持并发检索、自动去重与输入校验

## Input Schema

每条查询字符串支持四种格式，工具自动识别 `input_type`：

| 输入类型 | 示例 | 检索路径 |
|----------|------|----------|
| `uniprot_id` | `P00533` | UniProt 直接查询 |
| `ec_number` | `1.1.1.1` | UniProt 全文检索 + KEGG 补全 |
| `name` | `lipase` | UniProt 关键词检索 |
| `kegg_id` | `ec:1.1.1.1` | KEGG 直接查询（UniProt 无结果时兜底）|

输入约束：单条查询最长 200 字符；仅允许字母、数字、空格及 `. : - / ( )`；批次内自动过滤重复项。

**输入示例：**

```json
["1.1.1.1", "A2RUC4", "lipase"]
```

## Parameters

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `data` | `list[str]` | ✅ | — | 查询列表，每项为酶名称、EC 号或 UniProt ID |
| `max_results` | `int` | ❌ | `5` | 每条查询从 UniProt 最多返回的候选条目数 |
| `fetch_smiles` | `bool` | ❌ | `true` | 是否从 PubChem 拉取第一个底物的 SMILES |
| `fetch_kegg` | `bool` | ❌ | `true` | 是否通过 KEGG API 补充反应/通路信息 |
| `output_format` | `str` | ❌ | `"parquet"` | 输出格式：`parquet` \| `csv` |

## Output

### result

#### 查询级统计

| 字段 | 类型 | 说明 |
|------|------|------|
| `n_queries_requested` | `int` | 提交的查询总数 |
| `n_queries_succeeded` | `int` | 成功返回结果的查询数 |
| `n_queries_failed` | `int` | 无结果或网络失败的查询数 |
| `n_skipped` | `int` | 校验阶段过滤掉的查询数（空串/超长/非法字符/重复）|

#### 记录级统计

| 字段 | 类型 | 说明 |
|------|------|------|
| `n_records` | `int` | 写入输出文件的记录总行数 |
| `n_duplicates_dropped` | `int` | 按 `uniprot_id` 去重后删除的条目数 |
| `columns` | `list[str]` | 输出表格的列名列表（固定 Schema）|

#### 分析摘要（summary）

| 字段 | 类型 | 说明 |
|------|------|------|
| `n_with_sequence` | `int` | 含蛋白质序列的记录数 |
| `n_with_smiles` | `int` | 含底物 SMILES 的记录数 |
| `n_with_reactions` | `int` | 含催化反应描述的记录数 |
| `pct_with_sequence` | `float` | 序列覆盖率（%）。UniProt 来源记录通常有序列，KEGG only 记录无序列 |
| `pct_with_smiles` | `float` | SMILES 覆盖率（%）。仅 `fetch_smiles=true` 且 PubChem 有收录时才有值 |
| `ec_class_dist` | `dict` | EC 顶级类别分布。EC 号第一位数字代表酶的大类：1=氧化还原酶、2=转移酶、3=水解酶、4=裂解酶、5=异构酶、6=连接酶、7=转运酶 |
| `top_organisms` | `dict` | 前 5 高频物种及其记录数 |
| `source_db_dist` | `dict` | 来源数据库分布（UniProt vs KEGG）|

#### errors

失败查询的详情列表，每项包含：

| 字段 | 说明 |
|------|------|
| `query` | 原始查询字符串 |
| `error` | 错误码：`NO_RESULTS` / `NETWORK_ERROR` / `UNEXPECTED_ERROR` |
| `repair_hint` | 可读的修复建议 |
| `traceback` | 完整异常栈（仅 `NETWORK_ERROR` / `UNEXPECTED_ERROR`）|

**输出示例（`data=["1.1.1.1"]`，`fetch_smiles=false`，`fetch_kegg=false`）：**

```json
{
  "result": {
    "n_queries_requested": 1,
    "n_queries_succeeded": 1,
    "n_queries_failed": 0,
    "n_skipped": 0,
    "n_records": 5,
    "n_duplicates_dropped": 0,
    "columns": [
      "query", "input_type", "uniprot_id", "protein_name", "gene_name",
      "organism", "ec_number", "reactions", "substrates", "products",
      "pathways", "substrate_smiles", "sequence", "seq_length", "source_db"
    ],
    "summary": {
      "n_with_sequence": 5,
      "n_with_smiles": 0,
      "n_with_reactions": 0,
      "pct_with_sequence": 100.0,
      "pct_with_smiles": 0.0,
      "ec_class_dist": {"class_1": 5},
      "top_organisms": {
        "Homo sapiens": 2,
        "Mus musculus": 1,
        "Saccharomyces cerevisiae": 1,
        "Rattus norvegicus": 1
      },
      "source_db_dist": {"UniProt": 5}
    },
    "errors": []
  },
  "metadata": {
    "status": "success",
    "records_processed": 5,
    "n_errors": 0,
    "duration_ms": 1502
  },
  "artifacts": {
    "output_file": "/tmp/sdc_outputs/bio/enzyme_attributes.parquet"
  }
}
```

### metadata

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | `str` | `success` \| `partial_success`（部分查询失败但有可用结果）|
| `records_processed` | `int` | 同 `n_records` |
| `n_errors` | `int` | 同 `n_queries_failed` |
| `duration_ms` | `float` | 工具总运行时间（毫秒）|

### artifacts

| 字段 | 类型 | 说明 |
|------|------|------|
| `output_file` | `str` | 输出文件绝对路径（Parquet 或 CSV）|

### 输出表格 Schema（固定列顺序）

```
query | input_type | uniprot_id | protein_name | gene_name | organism
ec_number | reactions | substrates | products | pathways
substrate_smiles | sequence | seq_length | source_db
```

### 状态码与 partial_success 说明

| status | 含义 |
|--------|------|
| `success` | 所有有效查询均返回结果，`errors` 为空列表 |
| `partial_success` | 部分查询失败，但仍有可用记录写入文件；`errors` 包含失败详情 |
| `error` | 无任何可用结果（全部失败或全部被校验过滤）；不产生输出文件 |

### 错误码（error 状态下）

| code | 含义 |
|------|------|
| `OFFLINE_MODE` | `offline_mode=True`，工具需要网络访问 |
| `ALL_QUERIES_INVALID` | 所有输入均未通过校验 |
| `ALL_QUERIES_FAILED` | 所有有效查询均无检索结果 |
| `WRITE_FAILED` | 文件写入失败（磁盘空间不足等）|

## Example

```python
# 基础用法：混合输入类型
result = run_tool(
    "enzyme_acquire",
    data=["1.1.1.1", "P00533", "lipase"],
    max_results=5,
    fetch_smiles=True,
    fetch_kegg=True,
    output_format="parquet",
)

# 读取标准化输出表格
df = pd.read_parquet(result["artifacts"]["output_file"])

# 查询级统计
print(f"请求: {result['result']['n_queries_requested']} 条，"
      f"成功: {result['result']['n_queries_succeeded']} 条，"
      f"失败: {result['result']['n_queries_failed']} 条")

# 分析摘要
summary = result["result"]["summary"]
print(f"序列覆盖率: {summary['pct_with_sequence']}%")
print(f"EC 类别分布: {summary['ec_class_dist']}")
print(f"高频物种: {summary['top_organisms']}")

# partial_success 时检查失败详情
if result["metadata"]["status"] == "partial_success":
    for err in result["result"]["errors"]:
        print(f"[{err['error']}] {err['query']} → {err['repair_hint']}")

# 与 protein_analyzer 串联——直接传入输出文件路径
protein_result = run_tool(
    "protein_analyzer",
    data=result["artifacts"]["output_file"],  # 直接传文件路径
    run_blast=False,
)
```

## Configuration

```yaml
# config.yaml
skills:
  - enzyme_acquire   # 对应 skills/enzyme_acquire/SKILL.md
```

环境变量 `SDC_OUTPUT_DIR` 控制输出根目录（默认 `/tmp/sdc_outputs/bio/`）。输出文件与 `enzyme_acquire_meta.json` sidecar 均写入该目录，已加入 `.gitignore`，不应提交至仓库。

## Dependencies

- 网络访问：UniProt REST API、KEGG API、PubChem PUG-REST
- Python：`pandas`、`pyarrow`
- 内部模块：`tools.scitools.sources`（`fetch_by_id` / `fetch_by_name` / `fetch_enzyme` / `fetch_smiles` / `parse_entry`）
- 并发：最多 4 路 API 并发（`_API_SEMAPHORE`），失败后指数退避重试最多 3 次
