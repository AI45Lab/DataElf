# Trajectory Analysis

`trajectory_analysis` 只读查询 WT Serving，根据任务要求、轨迹行为与反馈分析有证据支持的关键偏差。Tool、Client、Skill、分析 prompt 和 review 属于本 domain；DataElf 公共层提供 job、workspace、Pi 执行和产物检查。分析不要求 ground truth，reward=0 仅用于筛选，不能证明原因。

本文是人类使用与开发指南，不是运行时 Skill。通用流程遵循根 [README](../../../README.md) 和 [开发契约](../../../docs/development_contract.md)。以下终端命令以**仓库根目录**为工作目录；Agent 示例只在已经 prepare 的 job 内执行。

## 安装与配置

首次准备源码环境时，通过 `trajectory` 可选依赖安装 WT SDK，再由公共 `dataelf setup` 准备 Pi/runtime：

```bash
uv venv
uv pip install -e ".[dev,trajectory]"
source .venv/bin/activate
dataelf setup
dataelf init
```

默认安装和 `dataelf setup` 不安装 WT SDK。`trajectory` extra 使用与 [requirements-wt.txt](requirements-wt.txt) 相同的 SDK Git commit，安装需要 Git、网络及依赖仓库访问权限。

非 editable 安装可在源码目录执行 `uv pip install '.[trajectory]'`，也可安装构建好的 wheel（替换文件路径）：

```bash
uv pip install --python .venv/bin/python '/absolute/path/dataelf-0.1.0-py3-none-any.whl[trajectory]'
```

wheel 和 sdist 收录 [Skill](pi/skills/wt-serving-query/SKILL.md) 及 `requirements-wt.txt`，无需另拷 Skill。SDK 由 extra 安装，不内嵌在 wheel 中。完整 job 仍需公共 Pi/runtime、provider 和凭据配置；当前完整运行流程以源码 checkout 为准。

已有环境不必重建或覆盖配置。prepare 只校验所选 Python、Skill 和 SDK 导入并传递运行信息，不安装依赖或查询 WT。

将以下无凭据片段合入本地配置，替换 provider/model；可用 `DATAELF_CONFIG_FILE` 选择配置文件：

```yaml
explorer:
  type: pi
  pi:
    model: your-provider/your-model
    mode: json
    timeout_seconds: 600
domains:
  trajectory_analysis:
    mode: tool
    profile: test
    modeling:
      enabled: false
```

`tool_python` 默认当前 Python，可由 `DATAELF_TRAJECTORY_TOOL_PYTHON` 覆盖；该解释器须能导入 DataElf 和固定 WT SDK。`skill_path` 默认包内 Skill；显式覆盖只影响 preflight 和传给 Agent 的读取路径，不替换公共资源发现结果，通常保留默认值。profile 仅支持 test，modeling 不支持开启；fixture 模式只供显式合成输入使用，不自动回退。

WT 凭据 `WT_SDK_DB_URI`、`WT_SDK_S3_ENDPOINT`、`AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` 由启动 DataElf 的 shell 提供；仅放入 YAML 的 env 映射不能满足 domain preflight。模型凭据按公共 provider 配置规则提供，不写进提交文件。

## 运行与产物

```bash
export DATAELF_WORKSPACES_DIR="$PWD/.dataelf/workspaces"
export DATAELF_TRAJECTORY_TOOL_PYTHON="$PWD/.venv/bin/python"
export DATAELF_PI_TIMEOUT_SECONDS=600
dataelf run --domain trajectory_analysis --no-modeling \
  "分析一条 WT 失败轨迹，定位关键偏差，说明原因并给出证据。"
```

绝对 workspace 路径避免 Agent 改变目录后解析漂移。每次运行创建新 job；当前 tool 模式不提供锁定历史样本的输入接口。超时或调用失败不代表模型分析质量结论。

| 当前 job 内的正式产物 | 用途 |
|---|---|
| `raw/trajectory_analysis/tool_calls.json` | recorder 保存实际调用参数、传输状态及原 envelope |
| `tables/trajectory_analysis/query_metadata.json` | 逐调用数量、字段状态、截断和省略投影 |
| `reports/failure_analysis.json` | Pi 生成有证据引用的分析报告 |

查看运行返回的 workspace 中的报告及 `reviews/quality_review.json`；`artifact_manifest.json` 和 `workspace_index.json` 记录产物与结果索引。不要将 raw、凭据或整段轨迹复制进公共日志。

| 报告状态 | 含义 |
|---|---|
| located | 当前可见证据支持具体关键偏差；不表示 ground truth 验证 |
| insufficient_evidence | 目标、行为或关键上下文不足，应列明缺口与补查尝试 |
| no_records | search 确实为空 |
| read_failed | Tool/transport 失败，job 审核失败 |

报告区分 observed、inferred 和未知，联系要求、偏差与影响，考虑错误恢复和反证。任何采集截断仍保守禁止 located；空 get 也是 insufficient_evidence。缺少 evaluator 不能单独否决定位，只有 reward=0 和猜测也不能定位。schema 由 [analysis.py](analysis.py) 定义并随 prompt 提供；历史报告不自动迁移。结构或引用不合法会被 review 拒绝。

## Skill 与 Client 接入

| 领域文件 | 职责 |
|---|---|
| [domain.yaml](domain.yaml)、[plugin.py](plugin.py)、[config.py](config.py) | manifest、typed config、prepare、输出契约及插件入口 |
| [prompt.py](prompt.py)、[Skill](pi/skills/wt-serving-query/SKILL.md) | 分析方法与读取/调用指令；Skill 负责查询约定 |
| [client.py](client.py)、[connector.py](connector.py) | Agent 入口、持久预算、raw 和来源投影 |
| [Tool](tools/wt_serving/adapter.py)、[bridge](tools/wt_serving/bridge.py)、[catalog](tools/wt_serving/catalog.py) | 只读参数、JSON 子进程协议与 SDK 适配 |
| [analysis.py](analysis.py)、[review.py](review.py) | 报告与采集一致性检查，插件委托 review_analysis |

本 domain 使用公共资源加载契约。Skill 位于 `pi/skills/wt-serving-query/SKILL.md`，无需全局注册或手工配置 `--skill`。

1. 公共 [workflow](../../discovery/workflow.py) 调用 [resolve_domain_resources](../../discovery/agent_resources.py)，发现当前 domain 的 `pi/skills/**/SKILL.md`，校验路径并写入 `DiscoveryContext.agent_resources`。
2. [Pi explorer](../../discovery/pi_cli_explorer.py) 使用 `--no-skills`、`--no-extensions` 关闭默认自动发现，再显式传入受管公共资源和当前 domain 的资源。WT Skill 通过 `--skill <绝对路径>` 交给 Pi，其他 domain 不会因此获得它。
3. `build_prompt()` 经公共 [prompt_builder](../../discovery/prompt_builder.py) 生成任务 prompt，由 Pi 的 `@文件` 参数接收。资源加载与任务指令分别传递。

`plugin.prepare()` 另外通过 `StageResult.env` 提供 `DATAELF_TRAJECTORY_SKILL`、`DATAELF_TRAJECTORY_TOOL_PYTHON` 和 capture 信息，公共执行器将其传给 Pi 子进程。默认 Skill 路径从 config 所在模块解析；env 路径用于指导读取，不承担 Skill 注册。Tool 仍由 Python Client 调用，不注册为 Pi extension 工具。

**不需要操作者手动读取或注入 Skill。** 当前领域 prompt 仍要求 Agent 在 WT 调用前阅读 Skill，可使用内置 read 或 Python `Path.read_text()`。这是查询操作指令；当前 review 检查调用证据、产物和报告引用，不以 Skill 读取事件作为通过条件。

公共资源加载成功不等于 Agent 已阅读或遵循正文。如需审计实际阅读，应另外核对读取事件的目标路径、成功返回内容及其与 Client 执行的先后顺序。不要打印整个 env 或凭据。

读完 Skill 后，Agent 在当前 workspace 写入脚本，并用选定解释器执行。任务内的最小 Client 示例为：

```python
from dataelf.domains.trajectory_analysis.client import TrajectoryClient

client = TrajectoryClient.from_env()
search = client.search_records(reward=0, limit=1)
if not search["isError"] and search["result"]["records"]:
    record = search["result"]["records"][0]
    detail = client.get_record(record["id"], fields=["chosen_trace"])
```

例如 Agent 已将脚本写为 acquire.py 后：

```bash
"$DATAELF_TRAJECTORY_TOOL_PYTHON" "$DATAELF_JOB_WORKSPACE/acquire.py"
```

Client 校验当前 domain/workspace 并记录实际调用。不要直接执行 SDK/bridge 绕过 recorder，不恢复全局 WT extension 或共享 Skill 注册配方。prepare 不填分析答案，raw/tables 不由 LLM 重写。

### 查询与证据边界

每 job 最多一次 reward=0、limit=1 search 和五次 get，共六次；首读 chosen_trace，按需补读 messages、response、rejected_trace、meta_json，每次一个新字段、每字段一次。锁定首条实际 locator，适用时带入 job_id 并核对返回身份，不假定 id 全局唯一。预算持久化，失败计数；新 Client/脚本不重置。空 search、空 get 或真实错误后停止，不重试、不换记录。

Tool 通用 limit 上限20不改变本 case 预算。仅 Serving/test/serving_test；不添加 completed 条件，禁止 Delivery、Landing、production、写操作、raw SQL 和自带地址/凭据参数。保持原数值与 list/dict/null、deserialize_json 和64KiB整字段省略规则；missing/null/empty/omitted 分别记录，不拆分重建省略字段。

补查保留逐次来源，不覆盖首次 chosen_trace。报告的 workspace 相对路径和 RFC6901 pointer 必须指向实际请求、保存且未省略的字段，不能固定引用 calls/1、跨 job 或用数组位置冒充 WT step_id。

### 同事复用步骤

在自己的 domain 的 `pi/skills/<名称>/SKILL.md` 保存 Skill，由公共执行器按当前 domain 发现并传给 Pi。领域持有业务 Client 和 typed config；prepare 通过 StageResult 提供必要信息，prompt 指导读取及调用，再用 OutputContract、只读当前 workspace 的 review 和 result_ids 管理输出。自有环境变量、凭据和业务预算在本领域定义，不照抄 WT。复用现有 DomainPlugin 和 Pi 代码执行即可，无需新增公共加载器或全局注册。

## 测试与限制

准备好源码依赖后，从仓库根目录运行正式回归：

```bash
.venv/bin/python -m pytest -q tests/test_trajectory*.py
```

六个测试文件分别覆盖基础接入及打包、分析证据、有界采集、SDK preflight、Pi/跨域隔离及 Tool 约束。五组合成 fixture 覆盖早期偏差、恢复、无 ground truth、补查和省略；expected.json 只供测试/假 Pi 输出。fake SDK/fake Pi 验证工程行为，不证明模型能力。

Pi 集成测试需 Node22.19+ 和已有 Pi package；使用 `DATAELF_TRAJECTORY_TEST_NODE`/PATH 及可选 `DATAELF_TRAJECTORY_TEST_PI_ROOT` 配置，不在测试中安装依赖。部分离线测试需要已安装固定 WT SDK，但不连接真实服务。

分发验证应检查实际 wheel 中的 Skill、依赖文件及可选 SDK 声明，并在源码目录外验证默认 Skill 路径和公共加载结果。

recorder 使用 POSIX fcntl，不支持原生 Windows Python。领域指令和 containment 不是操作系统沙箱。文件存在、进程得到路径、实际阅读成功、Client 查询成功是不同层次；review、completed 或 located 均不能单独证明模型推断正确。
