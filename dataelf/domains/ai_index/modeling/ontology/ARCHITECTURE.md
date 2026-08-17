# Ontology 代码结构与功能

本文描述 `dataelf/domains/ai_index/modeling/ontology/` 的内部代码结构，以及它与
`dataelf/domains/ai_index/modeling/` 领域建模层之间的稳定边界。

## 总体边界

```text
dataelf discover
  ├─ AIIndexModeler
  │   ├─ AIIndexRawCollector                  AI Index -> raw JSON
  │   └─ isolated ontology worker
  │       ├─ Stage 1 + AIIndexOntologyAdapter raw -> reviewed ontology contract
  │       └─ Stage 2 build                    contract + raw -> reviewed RDF
  └─ Pi or DeepAgentsCode Explorer            typed prompt/artifacts -> insights
```

`dataelf/domains/ai_index/modeling/ontology/` 不负责创建 DiscoveryJob、解析用户 query、执行 Explorer、quality review 或
finalize；这些由 DataElf 外层负责。AI Index schema、template 和 candidate mapping 由
domain modeling package 持有。内部 Stage 1 pipeline 通过显式 `Stage1DomainAdapter` 连接这些实现，
但该 ontology 子包本身不作为跨 domain 的通用 API 发布。

## 目录结构

```text
dataelf/domains/ai_index/modeling/ontology/
├── common/
│   ├── artifacts.py              canonical JSON、SHA-256、原子读写
│   └── contracts.py              Stage 1/2 共享文件名和 contract 常量
├── stage1/
│   ├── config.yaml               Stage 1 source、模型、预算、输出配置
│   ├── run.py                    独立诊断 CLI
│   ├── tool_bridge.py            Pi tool 到 Python evidence 查询的受限桥接
│   ├── schemas/                  published contract JSON Schema
│   ├── runtime/
│   │   ├── pi_runtime.ts         generator agent、工具和收敛控制
│   │   ├── reviewer_runtime.ts   fresh-context reviewer agent
│   │   └── nonstream_openai.ts   非流式兼容 transport、超时、重试、heartbeat
│   └── ontology_stage1/
│       ├── pipeline.py           checkpoint、round、review、publish 主流程
│       ├── raw_source.py         raw 发现、fingerprint、内部 evidence cache
│       ├── raw_semantics.py      raw path/类型/缺失性语义
│       ├── source.py             source index 和 provenance replay
│       ├── domain_adapter.py     Stage 1 领域适配协议
│       ├── contracts.py          schema 加载和结构校验
│       ├── checkpoints.py        run ID、兼容 fingerprint、锁和事件日志
│       └── model_runtime.py      Python/TypeScript runtime 编排
├── stage2/
│   ├── config.yaml               Stage 2 模型、质量门禁、输出配置
│   ├── run.py                    独立诊断 CLI
│   └── ontology_stage2/
│       ├── pipeline.py           compile/materialize/review/repair/publish 主流程
│       ├── contract.py           Stage 1 manifest/hash/source replay 验证
│       ├── compiler.py           endpoint extraction-plan seed 和受限编译
│       ├── rdf.py                确定性 IRI、quad、provenance 和 serialization
│       ├── validation.py         serialization、CQ、coverage、authority 门禁
│       ├── reviewer.py           独立审核上下文与 review contract
│       ├── model_runtime.py      JSON model 请求、超时、重试、heartbeat
│       └── prompts.py            compiler/reviewer prompt
├── ARCHITECTURE.md
└── README.md
```

DataElf 接入代码：

```text
dataelf/domains/ai_index/modeling/
├── acquisition.py       AI Index 三个确定性 breadth API call，只写 raw
├── ontology_adapter.py  AI Index 实体、属性和 grounding mapping
├── stage1_prompts.py    AI Index Stage 1 generator/reviewer prompt contract
├── stage1_validation.py AI Index contract 确定性门禁
├── stage1_shacl.py      AI Index SHACL 生成与验证
├── template.py          固定模板兼容校验与 job-local binding
├── ontology_runner.py   Stage 1/2 Python contract 适配
├── worker.py            隔离进程内执行 Stage 1/2
├── subprocess_runner.py 父进程启动、超时和环境隔离
├── pipeline.py          acquisition -> ontology -> typed artifacts
├── prompt.py            runtime-neutral RDF-first prompt
├── contracts.py         RawAcquisitionResult、OntologyRunResult、错误码
├── state.py             轻量、原子、脱敏 state.json
└── templates/           AI Index 固定模板 package data
```

## Stage 1：raw 到 ontology contract

输入是当前 job 的 `raw/ai_index/*.json`。Stage 1 首先对选定 raw 和 normalizer 实现
计算 fingerprint，再把规范化表写入 fingerprint 绑定的 `source_cache`。这些表只用于
evidence/profile/tool 查询，不是正式工作流的 CSV 分析输入。

Generator 负责领域概念和语义决策；controller 将结果补全为可执行 contract；
deterministic validator 检查 schema、引用闭合、raw JSON Pointer replay、missingness、
identity、relation authority、SHACL 和 competency questions；fresh-context reviewer 独立
给出 approve/revise/unusable。只有 validator 通过且 reviewer approve 的 candidate 才会
原子发布。

Stage 1 published bundle：

- `ontology.json`：类、object/datatype property、层级和语义元数据
- `grounding.json`：raw path、source binding、entity resolution、IRI、关系权威和 CQ
- `source_index.json`：raw 文件、record/fragment 和 JSON Pointer replay index
- `evidence.json`：profiling 和模型可引用 evidence
- `shacl.ttl`：图约束
- `normalization_lineage.json`：raw 到内部 evidence 的 lineage
- `validation.json`、`review.json`、`manifest.json`

## Stage 2：contract 到 RDF

Stage 2 只接受通过 hash、manifest、source fingerprint 和 offline validation 的 Stage 1
published contract。模型 compiler 只能在 controller 生成的 coverage key allowlist 中
确认 endpoint extraction plan，不能直接任意生成 triples。

RDF 由确定性代码物化：

- schema graph：ontology classes/properties/SHACL 相关定义
- source graph：请求、response document、record、fragment、raw URI/JSON Pointer
- observation graph：一次 API snapshot 中的 rank、heat、citation、funding 等观测值
- domain graph：稳定 Paper/Scholar/Institution 等实体和受 relation-authority 控制的关系

Reviewer 读取 compact review context，检查 source replay、信息完整性、观测语义、身份、
关系权威、serialization 和 competency queries。通过后发布：

- `graph.nq`：规范 N-Quads dataset，保留 named graph
- `graph.nt`：union N-Triples compatibility view
- `graph.rdf`：union RDF/XML compatibility view，同时复制为 workspace stable RDF
- `manifest.json`、`validation.json`、`review.json`、`metrics.json`
- `projection_lineage.json`、`iri_registry.json`、`extraction_plans.json`

## 新建式运行约束

正式 adapter 固定调用：

```python
generate_pipeline(..., resume=None, repair_from=None)
build(..., resume_run_id=None)
```

因此 workspace 中即使存在旧 `latest.json`、checkpoint 或 published bundle，也不会成为
本次运行输入。当前 job 内部仍保留 checkpoint/candidate，目的是失败诊断；DataElf 不会
自动 resume 或跨 job 复用它们。

## 验证与发布不变量

进入最终 Explorer 前必须同时满足：

1. Stage 1 status 为 `completed`，published bundle offline validation 为 `valid`。
2. Stage 2 status 为 `completed`，published bundle offline validation 为 `valid`。
3. `graph.nq`、`graph.nt`、`graph.rdf`、manifest、validation 均存在且非空。
4. manifest 的 artifact size/hash、contract fingerprint、serialization 可以重算通过。
5. 外层 `OntologyRunResult` 包含本次 Stage 1/2 run ID 和全部规范路径。

任一条件失败，domain modeling node 写入稳定错误码并禁止启动 Explorer。

## 模型运行与日志

Stage 1 generator/reviewer 是 Pi agent 进程，拥有多轮工具调用，因此区分进程总超时和
单次 HTTP 请求超时。Stage 2 compiler/reviewer 是严格 JSON 请求，单次请求超时之外还受
共享 stage deadline 约束。`request_max_retries` 表示首次失败后的重试次数。

事件日志记录 model、脱敏 endpoint、attempt、heartbeat、耗时、finish reason、usage 和
脱敏错误。正式版本不持久化完整 HTTP request/response，也不记录 API key。artifact、
state 和 latest 指针均使用原子写入；manifest 用 SHA-256 绑定发布内容。

## 修改边界

- AI Index schema 变化：优先改 `acquisition.py` 的 canonical projection、Stage 1 raw
  semantics/source index，再更新确定性 validator。
- Stage 1/2 返回 contract 变化：只在 `dataelf/domains/ai_index/modeling/ontology_runner.py` 适配，
  不把 Ontology 内部对象泄漏给其它 explorer。
- RDF 语义变化：修改 Stage 1 ontology/grounding contract 与 Stage 2 deterministic
  materializer，并同步提高 validation/reviewer 门禁。
- RDF 分析策略变化：修改 `dataelf/domains/ai_index/modeling/prompt.py`；Pi 与 DCode
  runtime 都只消费 `DiscoveryContext.modeling_artifacts.prompt_path`。
