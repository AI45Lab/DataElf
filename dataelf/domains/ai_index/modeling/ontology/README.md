# DataElf Ontology 工作流

`dataelf/domains/ai_index/modeling/ontology/` 是 AI Index domain 私有的 Stage 1/2 实现；领域适配位于
`dataelf/domains/ai_index/modeling/`。正式入口不是分别运行
Stage 1 或 Stage 2，而是一次完整的 `dataelf discover`：

```text
AI Index API raw
  -> Ontology Stage 1（动态建模，或固定模板的确定性绑定）
  -> Ontology Stage 2（抽取、RDF 物化、验证、审核）
  -> 配置选择的 Pi 或 DeepAgentsCode 基于 graph.nq 调研
  -> quality review / finalize
```

每个新 job 都重新采集 raw 并重新生成 RDF。默认模式会重新建立 ontology；选择固定
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

推荐将本机配置写入 git 忽略的 `dataelf.local.yaml`：

```yaml
workspace_dir: .dataelf
ai_index_mode: api
ai_index_base_url: https://index.shlab.org.cn/api/v2
ai_index_api_key: ak_...
enable_sqlite: false

insights_explorer: pi
pi_binary: ./node_modules/.bin/pi
pi_model: openai/glm-5.2-1m
pi_mode: json
pi_cwd: .
pi_timeout_seconds: 3000
pi_log_mode: summary

ai_index_modeling:
  enabled: true
  ontology_template:               # ai_index_search 可跳过 Stage 1 模型生成
  stage1_config: dataelf/domains/ai_index/modeling/ontology/stage1/config.yaml
  stage2_config: dataelf/domains/ai_index/modeling/ontology/stage2/config.yaml
  raw_page_size: 50
  model_name: glm-5.2-1m
  model_max_tokens:
  stage1_process_timeout_seconds: 7200
  stage1_request_timeout_seconds: 900
  stage1_request_max_retries: 3
  stage2_request_timeout_seconds: 600
  stage2_request_max_retries: 3
  stage2_total_timeout_seconds: 1800

env:
  PI_CODING_AGENT_DIR: .pi/agent
  OPENAI_BASE_URL: https://token.pjlab.org.cn/v1
  OPENAI_API_KEY: sk-...
```

也可以不写密钥到文件：

```bash
export AI_INDEX_API_KEY='...'
export OPENAI_BASE_URL='https://token.pjlab.org.cn/v1'
export OPENAI_API_KEY='...'
```

环境变量优先于 YAML。完整字段和对应环境变量见下方“配置字段”。

## 3. 正式运行

使用自动加载的 `dataelf.local.yaml`：

```bash
dataelf discover \
  '围绕 Agentic LLMs，基于 AI Index，发现最近值得关注的 3 个 insight'
```

显式选择固定模板：

```bash
dataelf discover \
  '围绕 Agentic LLMs，基于 AI Index，发现最近值得关注的 3 个 insight' \
  --insights-explorer pi \
  --ai-index-modeling \
  --ontology-template ai_index_search
```

也可以设置 `ai_index_modeling.ontology_template: ai_index_search` 或
`DATAELF_AI_INDEX_MODELING_ONTOLOGY_TEMPLATE=ai_index_search`。模板模式严格校验三个 search endpoint；
如果规范化 raw 字段新增、缺失或变更类型，工作流明确失败并输出兼容性错误，不会
静默回退动态 Stage 1。

若要关闭建模并使用原 CSV-first 流程：

```yaml
insights_explorer: pi
ai_index_modeling:
  enabled: false
```

建模阶段与 Explorer runtime 独立；`pi` 和 `deepagentscode` 都消费同一个 typed artifact/prompt contract。

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

## 5. 配置字段

| YAML | 环境变量 | 含义 |
|---|---|---|
| `insights_explorer` | `DATAELF_INSIGHTS_EXPLORER` | Agent runtime：`pi` 或 `deepagentscode` |
| `ai_index_modeling.enabled` | `DATAELF_AI_INDEX_MODELING_ENABLED` | 在 Explorer 前启用 AI Index 建模 |
| `ai_index_modeling.stage1_config` | `DATAELF_AI_INDEX_MODELING_STAGE1_CONFIG` | Stage 1 内部配置路径 |
| `ai_index_modeling.stage2_config` | `DATAELF_AI_INDEX_MODELING_STAGE2_CONFIG` | Stage 2 内部配置路径 |
| `ai_index_modeling.ontology_template` | `DATAELF_AI_INDEX_MODELING_ONTOLOGY_TEMPLATE` | `ai_index_search` 跳过 Stage 1 模型；空值动态建模 |
| `ai_index_modeling.raw_page_size` | `DATAELF_AI_INDEX_MODELING_RAW_PAGE_SIZE` | 三个 endpoint 第一页大小，范围 1–50 |
| `ai_index_modeling.model_name` | `DATAELF_AI_INDEX_MODELING_MODEL_NAME` | 覆盖 Stage 1/2 模型角色 |
| `ai_index_modeling.model_max_tokens` | `DATAELF_AI_INDEX_MODELING_MODEL_MAX_TOKENS` | 可选统一输出上限 |

其余 timeout/retry 字段采用同样的 `DATAELF_AI_INDEX_MODELING_*` 前缀。

Pi 自身使用 `pi_model`、`pi_timeout_seconds`、`pi_log_mode` 等原配置。默认策略是
Stage 1 agent 进程不自动从头重跑；其中的单次模型请求可以重试。Stage 2 的每次请求
可以重试，但始终受共享 stage deadline 约束。

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
