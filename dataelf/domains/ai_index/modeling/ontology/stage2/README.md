# DataElf Ontology Stage 2

> 正式工作流由 `dataelf discover` 在当前 job 的 Stage 1 成功后调用；以下命令只用于开发诊断。

Stage 2 读取正式 Stage 1 contract 和 `raw/ai_index/*.json`。模型只从 Controller
提供的 coverage key 中编译三个 endpoint extraction plan；RDF、IRI、consensus、
provenance 和 authority projection 均由确定性代码生成。

```bash
python dataelf/domains/ai_index/modeling/ontology/stage2/run.py build \
  --workspace .dataelf/workspaces/<job_id>

python dataelf/domains/ai_index/modeling/ontology/stage2/run.py validate \
  --workspace .dataelf/workspaces/<job_id> \
  --bundle .dataelf/workspaces/<job_id>/ontology/stage2/published/<run_id>
```

正式输入由 `ontology/stage1/latest.json` 解析。开发期可显式提供
`--stage1-bundle <candidate> --allow-draft`，但 draft 结果只能留在 candidates，不能
更新 `ontology/stage2/latest.json` 或 `<workspace>.rdf`。

`graph.nq` 是包含 schema/source/observation/domain named graph 的规范产物；
`graph.nt` 和 `graph.rdf` 是 union compatibility view。发布始终需要确定性验证和独立
reviewer；`manual_audit_required` 只用于开发期人工暂停，默认关闭。相同 raw 与 contract
对应的 compiled cache 只服务独立 CLI/checkpoint 诊断；AI Index modeling runner 固定以
`resume_run_id=None` 新建本次 run，不复用历史 job 或旧 published RDF。完整结构和
artifact contract 见 `../ARCHITECTURE.md`。
