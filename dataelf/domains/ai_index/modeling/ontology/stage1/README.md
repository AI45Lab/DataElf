# Ontology Stage 1

> 正式工作流由 `dataelf discover` 调用本 Stage；以下命令只用于开发诊断。

Stage 1 直接读取 DataElf workspace 的 `raw/ai_index/*.json`，生成并审核三层
ontology contract：领域语义、API 观测和可回放 Source provenance。`tables/*.csv`
只作为内部 evidence cache，不是权威输入。

```bash
python dataelf/domains/ai_index/modeling/ontology/stage1/run.py generate \
  --workspace .dataelf/workspaces/<job_id> \
  --resume auto

python dataelf/domains/ai_index/modeling/ontology/stage1/run.py validate \
  --bundle .dataelf/workspaces/<job_id>/ontology/stage1/published/<run_id>
```

产物位于 `<workspace>/ontology/stage1/`，包括 source cache、checkpoint、候选、
review 记录、正式 bundle 和 `latest.json`。只有 schema/引用闭合、raw Pointer 全量
replay、missingness/identity/relation authority/SHACL/CQ 等确定性门禁通过，并得到
fresh-context reviewer approve 后才会发布。

独立 CLI 支持 `--resume <run_id>` 恢复兼容 checkpoint，或用 `--repair-from <run_id>`
进行开发修复。AI Index modeling runner 始终传入 `resume=None, repair_from=None`，不会走这些
复用路径。完整结构和 contract 见 `../ARCHITECTURE.md`。
