# 科学数据处理工具集 (scitools)

基于 SciDataCopilot 框架实现的科学数据处理工具，集成到 DataElf BaseTool 接口。

## 环境依赖

```bash
pip install biopython>=1.81 requests>=2.28.0 pandas>=2.0.0 pyarrow>=12.0.0 numpy>=1.24.0
```

## Case 1 - 生物医药 (Bio)

### EnzymeAcquireTool — 酶属性跨库检索

跨库检索酶的序列、反应、通路等属性信息，数据来源：UniProt / KEGG / PubChem。

**输入参数**
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| data | list[str] | ✅ | 查询列表，支持酶名、EC 编号（如 `1.1.1.1`）、UniProt ID（如 `P00533`） |
| max_results | int | ❌ | 每个查询最多返回条数，默认 5 |
| fetch_smiles | bool | ❌ | 是否从 PubChem 查询底物 SMILES，默认 True |
| fetch_kegg | bool | ❌ | 是否从 KEGG 补充反应/通路信息，默认 True |

**输出**
- 文件：`enzyme_attributes.parquet`
- 字段：`uniprot_id`, `protein_name`, `gene_name`, `organism`, `ec_number`, `reactions`, `substrates`, `products`, `pathways`, `substrate_smiles`, `sequence`, `seq_length`, `source_db`, `query`, `input_type`

---

### ProteinAnalyzerTool — 蛋白质序列理化分析

基于 BioPython 本地计算蛋白质理化属性（无需网络，速度极快），可选 NCBI BLAST 同源搜索。

**输入参数**
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| data | list[dict] | ✅ | 序列列表，每条含 `id`、`sequence`、`protein_name`（通常来自 EnzymeAcquireTool 输出） |
| run_blast | bool | ❌ | 是否运行 NCBI BLAST 同源搜索，默认 False（每条约 30-60 秒） |

**输出**
- 文件：`protein_analysis.parquet`
- 字段：`uniprot_id`, `protein_name`, `seq_length`, `mw`(分子量), `pI`(等电点), `instability`(稳定性指数), `is_stable`, `gravy`(疏水性), `helix_frac`, `turn_frac`, `sheet_frac`, `aa_composition`, `local_status`, `n_blast_hits`, `top_hit_id`, `top_identity`, `top_evalue`, `top_title`, `blast_status`

---

## 流水线

```
EnzymeAcquireTool
    输入：["1.1.1.1", "P00533"]
    输出：enzyme_attributes.parquet
                ↓
ProteinAnalyzerTool
    输入：[{id, sequence, protein_name}, ...]
    输出：protein_analysis.parquet
```

## 运行测试

```bash
pytest test/tools/scitools/bio/ -v
```
