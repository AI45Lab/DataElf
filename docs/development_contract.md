# DataElf Case Development Contract

本文是接入新 case/domain 时必须遵守的开发契约。它描述当前代码的稳定边界，而不是未来设想。示例实现是 `ai_index`，但新 case 不应复制其中的领域细节。

## 1. 核心原则

DataElf 将“分析什么”和“由什么 agent runtime 执行”拆成两个独立维度：

```text
DomainPlugin：数据源、领域语义、可选建模、分析要求、输出和审核
Explorer：在给定 workspace 和 contract 下执行探索
Core：job、workspace、artifact、调度、验证和 finalization
```

当前 explorer 只有 Pi。新增 case 必须新增 domain，不得新增 `pi_<domain>` explorer，也不得为 case 复制 workflow。

稳定依赖方向：

```text
CLI / API
  -> discovery contracts / workflow
       -> DomainRegistry -> DomainPlugin -> domain adapter / modeler / prompt / review
       -> ExplorerFactory -> Pi explorer
```

必须满足：

- `dataelf/discovery/` 不得 import `dataelf.domains.*`。
- domain 可以 import discovery contracts 和 core config types。
- domain 之间不得直接依赖对方的 connector、schema 或内部模型。
- domain-specific 类型、字段和默认值不得进入 core `DataElfConfig`。
- 通用抽象只能从至少两个真实 domain 的相同需求中提炼，不能提前把 AI Index 设计泛化为平台 contract。

## 2. Job contract

所有执行从 `dataelf.discovery.contracts.JobSpec` 开始：

```python
JobSpec(
    domain="example",
    objective="用户希望完成的研究目标",
    inputs={},
    parameters={},
    constraints={},
    requested_outputs=[],
    modeling_strategy=None,
    explorer="pi",
)
```

字段职责：

- `domain`：显式选择 domain，必须匹配 `^[a-z][a-z0-9_]*$`。
- `objective`：保留用户自然语言意图，不得被 domain 改写成另一项任务。
- `inputs`：执行所需的输入标识、文件引用或数据引用；不要埋在 prompt 文本中。
- `parameters`：结构化运行参数，例如 topic、time window、期望输出数量。
- `constraints`：预算、范围、合规或其他限制。
- `requested_outputs`：调用方期望的逻辑结果类型。
- `modeling_strategy`：可选 domain modeling 策略；其值和语义由 domain 负责。
- `explorer`：agent runtime 选择；当前只能是 `pi`。

`DomainPlugin.normalize_spec()` 可以补充 domain 默认值，但必须保留调用方已经显式提供的字段。建议使用 `setdefault`，并确保相同输入得到确定性结果。

当前 CLI 入口是 `dataelf run --domain <domain> "<objective>"`，由调用方显式选择 domain 并构造对应的 `JobSpec`。新 domain 的测试和程序化入口也应直接使用 `run_job()`；CLI 不应从自然语言猜测 domain，也不应把某个 domain 的参数写成全局必需参数。

## 3. Domain manifest 和加载

每个 domain 必须提供：

```text
dataelf/domains/<domain>/domain.yaml
```

最小示例：

```yaml
domain: example
version: "1"
display_name: Example Domain
plugin: dataelf.domains.example.plugin:create_plugin
capabilities:
  - dynamic_data_access
workspace_dirs:
  - raw/example
  - results
```

约束：

- manifest 由 `DomainManifest` 校验。
- manifest 中的 `domain` 必须与目录名和请求的 `JobSpec.domain` 一致。
- `plugin` 必须使用 `module:function` 形式。
- factory 签名必须兼容 `create_plugin(config, manifest)`。
- plugin 返回对象的 `plugin.manifest.domain` 必须与 manifest 一致。
- `workspace_dirs` 必须是 workspace-relative path，不允许绝对路径或 `..`。
- `capabilities` 是声明信息，不替代代码级校验。

不要在 core registry 中添加 `if domain == ...`。`DomainRegistry` 应仅通过 manifest 加载 plugin。

## 4. DomainPlugin contract

稳定接口定义在 `dataelf/discovery/contracts.py`：

```python
class DomainPlugin(Protocol):
    manifest: DomainManifest

    def normalize_spec(self, spec: JobSpec) -> JobSpec: ...
    def prepare(self, spec: JobSpec, workspace_path: str, config: Any) -> StageResult: ...
    def create_modeler(self, spec: JobSpec, config: Any) -> DomainModeler | None: ...
    def build_prompt(self, job: DiscoveryJob, context: DiscoveryContext) -> str: ...
    def output_contract(self, spec: JobSpec) -> OutputContract: ...
    def review(self, job: DiscoveryJob, workspace_path: str) -> ReviewResult: ...
    def result_ids(self, workspace_path: str) -> list[str]: ...
```

### 4.1 `normalize_spec`

负责把用户输入补全为稳定、结构化的 domain spec：

- 补充缺省 parameters、requested outputs 和 modeling strategy；
- 保留用户显式值；
- 不进行网络访问或写 workspace；
- 不读取 explorer 内部状态。

### 4.2 `prepare`

负责 domain 数据准备边界：

- 创建 manifest 声明的 domain 目录；
- 初始化 domain table/schema；
- 准备数据访问能力、初始 raw/tables 或工具入口；
- 返回后续阶段所需的 `context`、`env` 和 `ArtifactRef`。

返回 `StageResult`。`status="completed"` 时声明的 artifact 必须真实存在。可预期的准备失败应返回稳定 `error_code` 和可操作的 `error_message`，不要留下半成功状态。

secret 可以通过 `StageResult.env` 传给 explorer，但不得写入 prompt、artifact metadata、普通日志或提交文件。

### 4.3 `create_modeler`

不需要额外建模时返回 `None`。需要时返回实现以下接口的 domain-owned modeler：

```python
class DomainModeler(Protocol):
    def run(self, job: DiscoveryJob, context: DiscoveryContext) -> ModelingStageResult: ...
```

规则：

- modeling 是可选 domain stage，不是 core 或 explorer 的特殊模式。
- `status="completed"` 的 `ModelingStageResult` 至少包含一个 artifact。
- modeling artifact 的 `role` 通常是 `evidence`，并明确 `producer_stage="domain_modeling"`。
- modeling 失败必须阻止 explorer 启动，并以 domain modeling stage 归因。
- 不得把某次运行产生的 graph/RDF/模型结果回写到静态配置。

AI Index ontology 是 domain 私有实现，不是新 case 必须采用的架构。事件表、关系表、事件图或完全不建模都可以是合法策略。

### 4.4 `build_prompt`

只返回 domain-specific instructions，例如数据含义、分析方法、领域工具和质量要求。

不得重复或接管以下 core 内容：

- workspace 绝对路径和 containment 规则；
- 完整 `JobSpec`；
- prepared/modeling artifact inventory；
- required output paths；
- 通用 completion rule。

这些内容由 `dataelf/discovery/prompt_builder.py` 统一合成。Pi 不应根据 domain-specific Python 类型分支；领域差异通过 `DiscoveryContext`、artifacts、env 和 prompt 注入。

### 4.5 `output_contract`

声明 explorer 必须产出的正式文件：

```python
OutputContract(
    contract_id="example.result",
    version="1",
    artifacts=[
        OutputArtifactSpec(
            artifact_id="example_result",
            path="results/result.json",
            kind="example_result",
            media_type="application/json",
            required=True,
            json_root="items",
        )
    ],
)
```

规则：

- `artifact_id` 和 `path` 在一个 contract 内必须唯一。
- path 必须相对 workspace，不能逃逸 workspace。
- required output 不得在 explorer 运行前创建空占位文件。
- JSON output 应声明 `media_type`，有稳定 list root 时应声明 `json_root`。
- generic validator 只判断文件存在、非空、JSON 可解析、root 类型正确和路径安全，不承担领域语义审核。

### 4.6 `review`

负责 output contract 通过后的领域语义检查，例如必填业务字段、数量约束、evidence/provenance、confidence 和领域质量门槛。

状态语义：

- `pass`：满足领域要求，无 warning。
- `pass_with_warnings`：job 可以完成，但需要保留质量警告。
- `failed`：领域结果不可接受，最终 job 失败并记录 `DOMAIN_REVIEW_FAILED`。
- `skipped`：通常由 core 在更早阶段失败时生成，不是正常 review 成功状态。

review 必须只读取当前 workspace，不得依赖另一个 job 的隐式状态。

### 4.7 `result_ids`

从已通过的正式输出中提取稳定结果 ID，供 `workspace_index.json` 使用。解析失败时应安全返回空列表，不能改变正式输出。

## 5. Configuration contract

DataElf 只支持 nested schema：

```yaml
runtime: ...
explorer:
  type: pi
  pi: ...
domains:
  example: ...
env: ...
```

规则：

- domain 配置必须位于 `domains.<domain>`。
- domain 在自己的 `config.py` 中使用 typed model 校验配置。
- unknown key、错误类型和无效枚举必须尽早报错。
- 不得向 core `DataElfConfig` 添加 domain fields。
- 不得添加旧 flat schema alias 或双结构兼容层。
- 环境变量覆盖配置文件；domain-specific env 的映射由 domain config 负责。
- 只验证当前会执行的能力。功能关闭时，其保留参数是 dormant config，不应触发该功能的文件、模板或凭证校验。
- 功能开启时，必须在启动昂贵任务前完成 URL、凭证、文件、schema 和相互依赖的 preflight。

配置文件按第一个存在者加载：

```text
dataelf.local.yaml
dataelf.local.yml
dataelf.yaml
dataelf.yml
.dataelf/config.yaml
.dataelf/config.yml
.dataelf/config.json
```

不要读取或打印用户的 `dataelf.local.yaml` secret。提交版示例只能使用占位值。

## 6. Workspace and artifact contract

core 只创建 domain-neutral skeleton：

```text
<workspace>/
  job_spec.json
  raw/
  tables/
  scripts/
  notes/
  prompts/
  logs/
  reviews/
  artifacts/
```

domain-specific 目录由 manifest/`prepare()` 创建。required final outputs 由 explorer 创建。完成时 core 写入：

```text
reviews/quality_review.json
artifact_manifest.json
workspace_index.json
```

所有物质性阶段输出都必须通过 `ArtifactRef` 声明：

```text
artifact_id
kind
path                 workspace-relative
role                 input / evidence / output / log
producer_stage
media_type           optional
schema_id/version    optional
checksum             optional
provenance           optional
metadata             optional
```

必须使用 core artifact helper 校验路径。不得接受绝对 artifact path、`..`、symlink 逃逸或 workspace 外写入。

数据层职责应清晰：

- `raw/`：可审计的源响应或原始输入；
- `tables/`：稳定的结构化分析输入和派生数据；
- `scripts/`：可复现分析代码；
- domain modeling 目录：可选 graph/RDF/其他建模产物；
- domain result 目录：由 output contract 定义的正式结果。

不要把规划中的 `domain/objects.jsonl`、`relations.jsonl` 或完整知识图谱当作所有 case 的强制结构。

## 7. Runtime and failure contract

`run_job()` 的稳定顺序：

```text
load plugin
  -> normalize spec
  -> initialize job/workspace
  -> domain prepare
  -> optional domain modeling
  -> compose prompt
  -> explorer
  -> generic output validation
  -> domain review
  -> artifact manifest / workspace index / finalization
```

阶段之间只能通过 `DiscoveryContext`、result objects 和 artifact paths 传递数据。

失败规则：

- prepare/modeling/explorer 失败后不得继续启动下游昂贵阶段。
- 使用稳定、可测试的 `error_code`；message 应说明可操作原因。
- stage 声明不存在或越界 artifact 时使用 artifact contract failure。
- explorer process 成功、output contract 成功、domain review 成功是三个独立判断，不能互相替代。
- 无论成功或失败，尽可能写出 review、artifact manifest 和 workspace index，保留可审计状态。
- 不得通过预创建结果、吞异常或把 warning 当成功数据来伪造完成。

SQLite registry 是可选索引，不是 artifact source of truth。`runtime.enable_sqlite: false` 时 job 仍必须仅靠 workspace 完整复盘。

## 8. Explorer boundary

新 case 默认复用 `PiCliInsightsExplorer`，不要在 explorer 中加入 domain 分支。

Pi 接收 core 合成的 prompt、当前 `JobSpec`、prepared/modeling artifact inventory、domain/context env 和 output contract。Pi 必须把正式结果写入 workspace，不能修改 DataElf 源码。DataElf 不从 stdout 提取最终业务结果；stdout/stderr/event stream 只作为日志和运行状态。

模型职责边界：

- `explorer.pi.model` 选择 Pi 的 `provider/model`；
- `.pi/agent/models.json` 注册 provider endpoint、协议和模型 metadata；
- `.pi/settings.json` 只提供 Pi 未收到显式 model 时的 fallback；
- credential 通过本地 `env` 或环境变量注入，不能硬编码到 tracked provider 配置。

新增 case 通常不应修改 Pi provider。只有新增 runtime 能力时才考虑扩展 `ExplorerFactory`，并且必须保留相同 `InsightsExplorer` contract。

项目级 Pi runtime 由 `dataelf setup` 管理。它负责准备锁定的 Node/Pi CLI、Pi 分析包和项目内 npm cache，并在 `.dataelf/runtime/pi.json` 写入不含凭证的运行时清单。`dataelf run` 只消费已准备好的 runtime；不要在 domain plugin、case 脚本或用户文档中要求手工执行 npm/Pi 安装命令。Node.js/npm 仍是当前短期方案的主机前置依赖。

## 9. Recommended package layout

```text
dataelf/domains/example/
  __init__.py
  domain.yaml
  config.py
  plugin.py
  connector.py
  table_builder.py
  prompt.py
  review.py
  modeling/              # optional, domain-owned
  schemas/               # optional
  templates/             # optional
```

可以按 case 复杂度拆分文件，但 ownership 不变：connector/adapter 负责数据访问、raw persistence 和 provenance；normalizer/table builder 负责稳定结构化表示；modeler 负责可选语义建模；prompt 负责领域分析方法；output contract 负责文件接口；reviewer 负责领域质量。

## 10. 接入新 case 的最短路径

新 case 的推荐开发顺序如下：

1. **先定义 case contract**：明确输入数据、需要保留的 raw/provenance、分析用 tables、可选 modeling 产物、正式输出文件和领域质量标准。不要先改 core workflow。
2. **创建 domain package**：新增 `dataelf/domains/<name>/`、`domain.yaml`、`config.py` 和 `plugin.py`。先让 manifest 能被 `DomainRegistry` 加载。
3. **实现 typed config**：把所有 case 参数放在 `domains.<name>`，用 domain-owned Pydantic model 校验；为 API 和 fixture/offline 测试定义清楚必填项和环境变量覆盖规则。
4. **实现 `prepare()`**：创建 domain workspace 目录，接入 connector/adapter，保存 raw，生成 normalized tables，返回 `StageResult(context, env, artifacts)`。
5. **按需实现 modeling**：不需要建模就让 `create_modeler()` 返回 `None`；需要时实现 domain-owned modeler，并返回 workspace 内的 `ModelingStageResult` artifacts。建模失败必须在 explorer 前终止。
6. **实现 prompt、output contract 和 review**：`build_prompt()` 只写领域分析方法；`output_contract()` 声明相对 workspace 的正式文件；`review()` 检查领域语义、证据、数量和质量，不重复 generic validator。
7. **先用 fake Pi 跑通 core**：通过 `run_job(JobSpec(domain="<name>", ...), config, registry=...)` 做离线端到端测试，确认不需要改 `dataelf/discovery/`。现有最小参考是 `tests/test_discovery_mvp.py` 中的 `FakePlugin`、`_fake_registry()` 和 `test_fake_domain_runs_without_core_changes()`。
8. **再接真实数据和真实 Pi**：先跑 fixture/offline tests，再做显式 smoke/integration test；保留 job workspace、review、artifact manifest 和日志用于诊断。

完成后，新增 case 的主链路应是：

```text
domain.yaml
  -> DomainRegistry.load_plugin()
  -> normalize_spec()
  -> prepare()
  -> optional create_modeler()
  -> build_prompt() + core prompt composer
  -> Pi explorer
  -> output_contract() validation
  -> review()
  -> result_ids() / workspace_index.json
```

如果新增 case 必须修改 core workflow、给 explorer 增加 domain 分支、把参数放到顶层 flat config，或依赖另一个 job 的隐式文件，说明边界设计有问题，应先重新划分 ownership。

## 11. Minimum test contract

每个新 domain 至少覆盖：

1. manifest 能被 `DomainRegistry` 加载，factory 和 domain 名一致；
2. typed config 接受有效配置并拒绝 unknown/malformed 配置；
3. fixture/offline connector 能保存 raw 并生成预期 normalized 数据；
4. core workspace 不包含该 domain 的硬编码目录；
5. `prepare()` 返回的 artifact 全部存在且位于 workspace；
6. modeling disabled 时不校验或执行 dormant modeling 参数；
7. modeling enabled 的成功和失败路径都可归因，失败时 explorer 不启动；
8. prompt 同时包含 domain instructions、prepared artifacts 和 output contract；
9. fake Pi 可以通过 `run_job()` 完成整个新 domain，且不修改 discovery core；
10. required output 缺失、空文件、非法 JSON、错误 root 和 workspace escape 都会失败；
11. domain review 覆盖 pass、warning 和 hard failure；
12. `workspace_index.json` 包含正确 status/result IDs，`artifact_manifest.json` 可审计。

单元测试和默认 CI 不得依赖真实 API、真实模型、联网服务或用户 secret。真实 provider/API 只做显式 smoke/integration test，并保留 workspace 和日志用于诊断。

建议运行：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q dataelf
```

## 12. Review checklist

提交新 case 前逐项确认：

- [ ] 没有修改 core workflow 来加入 domain-specific 分支；
- [ ] 没有在 `dataelf/discovery/` import domain package；
- [ ] 所有 domain config 都位于 `domains.<domain>`；
- [ ] 没有兼容旧 flat config；
- [ ] `normalize_spec()` 保留调用方显式参数；
- [ ] prepare/modeling artifacts 全部 workspace-relative；
- [ ] required final outputs 没有被提前创建；
- [ ] output contract 和 review 职责分离；
- [ ] 失败有稳定 stage 和 error code；
- [ ] secret 不进入代码、prompt、日志、fixtures 或 artifacts；
- [ ] fake/offline 端到端测试通过；
- [ ] 新 domain 不要求复制 Pi runner；
- [ ] README、配置示例和本开发契约涉及的公共接口已同步。

最终判断标准：**删除这个新 domain 包后，core 仍然是完整、可运行、domain-neutral 的 runtime；把同一个 domain plugin 换到 fake Pi 下，也能仅依赖 contracts 和 workspace 完成测试。**
