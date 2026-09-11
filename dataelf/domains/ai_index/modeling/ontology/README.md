# DataElf Ontology 工作流

`dataelf/domains/ai_index/modeling/ontology/` 是 AI Index domain 私有的 Stage 1/2 实现；领域适配位于
`dataelf/domains/ai_index/modeling/`。正式入口不是分别运行
Stage 1 或 Stage 2，而是一次完整的 `dataelf discover`：

```text
AI Index API raw
  -> Ontology Stage 1（动态建模，或固定模板的确定性绑定）
  -> Ontology Stage 2（抽取、RDF 物化、验证、审核）
  -> Pi 基于 graph.nq 调研
  -> quality review / finalize
```

每个新 job 都重新采集 raw 并重新生成 RDF。动态模式会重新建立 ontology；选择固定
模板后跳过 Stage 1 generator/reviewer，只生成与当前 raw hash 绑定的 source index、
grounding 和验证产物。正式流程不会读取其他 job 的 ontology、checkpoint、compiled
plan 或 RDF。Stage 1/2 的独立 CLI 仅保留为开发诊断入口。

代码分层、模块职责和 artifact contract 见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 1. 安装

要求 Python 3.11+、Node.js 22.19+、npm：

```bash
uv venv
uv pip install -e ".[dev]"
npm install
```

如果项目级 Pi package 尚未安装：

```bash
PI_CODING_AGENT_DIR=.pi/agent npm_config_cache=.npm-cache \
  ./node_modules/.bin/pi install npm:@quarkos/pi-fusion --local --approve
```

## 2. 配置

Agent 配置（例如 `dataelf.local.yaml`）中的建模部分只保留开关与统一配置路径：

```yaml
domains:
  ai_index:
    modeling:
      enabled: true
      ontology_config: dataelf/domains/ai_index/modeling/ontology/config.yaml
```

省略 `ontology_config` 时使用本包的 `ontology/config.yaml`，开启建模时该文件必须存在且有效。
关闭建模时不读取文件。显式空路径无效。

所有 ontology 参数集中在 [config.yaml](config.yaml)：

```yaml
ontology_template: ai_index_search  # null 使用动态 Stage 1
raw_page_size: 50
worker_timeout_seconds: 9120       # 整个 worker 的总时限，单位秒
stage1:
  source: ...                      # raw / profiling 配置
  ontology: ...                    # namespace、domain pack、competency questions
  generator: ...                   # 模型、token、进程/请求超时和重试
  reviewer: ...
  pi: ...                          # runtime 路径
  quality: ...
  checkpoint: ...
  artifacts: ...
stage2:
  compiler: ...                    # 模型、token、请求超时和重试
  reviewer: ...
  total_stage_timeout_seconds: 1800
  output: ...
  vocabulary: ...
  quality: ...
```

上面展示分区结构；可运行的完整内容以 `config.yaml` 为准。
Stage 1 / Stage 2 参数直接生效，外层不再用默认值覆盖它们。模型凭证仍通过各角色的
`api_key_env` / `base_url_env` 指定环境变量，从进程环境或 DataElf 的 `env` 映射注入。

统一配置内的 `domain_pack_path`、`pi.repo`、带目录的 `pi.node` 相对于统一文件所在目录解析；
raw 和 artifact 子目录仍相对于 job workspace。复制配置到其他位置时需相应调整资源路径。
外层的相对 `ontology_config` 路径相对于启动目录解析，启动 worker 前转换为绝对路径。

## 3. 正式运行

```bash
dataelf discover --ai-index-modeling \
  '围绕 Agentic LLMs，基于 AI Index，发现最近值得关注的 3 个 insight'

# 使用另一份完整的统一配置
dataelf discover --ai-index-modeling --ontology-config /path/to/ontology.yaml \
  '围绕 Agentic LLMs，发现最近值得关注的 3 个 insight'
```

固定模板通过统一文件中的 `ontology_template: ai_index_search` 选择；设为 `null` 则运行
Stage 1 generator/reviewer。模板模式仍会绑定本次 raw、校验 source 兼容性，并执行 Stage 2。
不兼容时明确失败，不会静默回退动态 Stage 1。

关闭建模使用 `--no-ai-index-modeling` 或 `domains.ai_index.modeling.enabled: false`。

## 4. 输出

一次成功运行会产生：

```text
.dataelf/workspaces/job_<id>/
├── raw/ai_index/                         # 本次三类 API response
├── ontology/
│   ├── stage1/
│   │   ├── source_cache/                 # fingerprint 绑定的内部 evidence
│   │   ├── checkpoints/、candidates/、run_logs/
│   │   ├── published/<stage1_run_id>/    # ontology/grounding/SHACL/manifest/review
│   │   └── latest.json
│   └── stage2/
│       ├── compiled/、checkpoints/、candidates/
│       ├── published/<stage2_run_id>/    # graph.nq/nt/rdf + manifest/review/validation
│       └── latest.json
├── modeling/ai_index/state.json          # 轻量阶段、run ID、artifact path 和稳定错误码
├── prompts/discovery_prompt.md
├── scripts/、tables/、notes/、deep_dives/
├── insights/
│   ├── candidate_signals.json
│   ├── insight_candidates.json
│   └── final_brief.md
├── reviews/quality_review.json
└── logs/
    ├── pi_events.jsonl
    ├── pi_model_events.jsonl             # 使用兼容 transport 时的耗时/重试事件
    ├── pi_stdout.log
    └── pi_stderr.log
```

`graph.nq` 是规范分析输入，保留 schema、source、observation、domain 四个 named
graph；`graph.nt` 和 `graph.rdf` 只是 union compatibility view。Pi 不会把 workspace
原始 CSV 当作 AI Index 主分析输入，但可以生成 SPARQL/RDFLib 查询结果 CSV。

## 5. 配置迁移

| 位置 | 配置 |
|---|---|
| Agent config | `domains.ai_index.modeling.enabled`、`domains.ai_index.modeling.ontology_config` |
| 外层环境变量 | `DATAELF_AI_INDEX_MODELING_ENABLED`、`DATAELF_AI_INDEX_MODELING_ONTOLOGY_CONFIG` |
| 统一 ontology config | `ontology_template`、`raw_page_size`、`worker_timeout_seconds`、`stage1`、`stage2` |

旧 `stage1/config.yaml`、`stage2/config.yaml` 合并为 `ontology/config.yaml`，不再保留独立配置。
外层旧字段 `stage1_config`、`stage2_config`、`ontology_template`、`model_name`、
`model_max_tokens` 以及各 timeout/retry 字段均移除；模型和 token 上限分别填写到
`stage1.generator`、`stage1.reviewer`、`stage2.compiler`、`stage2.reviewer`。
`stage2_config.stage1_config` 交叉引用也已移除。

旧模板/模型/timeout 环境变量不再覆盖 ontology 参数，CLI 的 `--ontology-template`
替换为 `--ontology-config`。`worker_timeout_seconds` 控制完整 worker 的总时限；
角色进程/请求超时和 Stage 2 总时限各自在分区内配置。

## 6. 失败与排查

先查看：

```bash
JOB=.dataelf/workspaces/job_<id>
python -m json.tool "$JOB/modeling/ai_index/state.json"
tail -n 50 "$JOB/logs/pi_stderr.log"
tail -n 50 "$JOB/logs/pi_model_events.jsonl"
```

稳定错误码包括：

- `AI_INDEX_MODELING_RAW_ACQUISITION_FAILED`
- `AI_INDEX_MODELING_RAW_EMPTY`
- `AI_INDEX_MODELING_STAGE1_INCOMPLETE`
- `AI_INDEX_MODELING_STAGE1_FAILED`
- `AI_INDEX_MODELING_STAGE2_INCOMPATIBLE`
- `AI_INDEX_MODELING_STAGE2_FAILED`
- `AI_INDEX_MODELING_RDF_INVALID`

任何 ontology/RDF 门禁失败都会阻止最终 Pi。模型事件日志只记录 endpoint 元数据、
attempt、heartbeat、耗时、状态和脱敏错误，不保存 API key，也不保存完整请求/响应。

## 7. 独立诊断入口

这些命令用于开发、验证 artifact 或定位 checkpoint，不是生产工作流入口：

```bash
python dataelf/domains/ai_index/modeling/ontology/stage1/run.py validate \
  --bundle .dataelf/workspaces/job_<id>/ontology/stage1/published/<run_id>

python dataelf/domains/ai_index/modeling/ontology/stage2/run.py validate \
  --workspace .dataelf/workspaces/job_<id> \
  --bundle .dataelf/workspaces/job_<id>/ontology/stage2/published/<run_id>
```

正常使用只运行一次 `dataelf discover`。
