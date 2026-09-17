# DataElf Server 部署说明

DataElf Server 提供提交任务、查询进度、获取结果和重试失败任务四个接口。服务使用一个进程，默认同时执行最多 5 个任务，超出部分按提交顺序排队，默认状态目录为 `.dataelf/server`。

调用方文档见 [API 外部调用说明](../API_USAGE.md)，文档总入口见 [DataElf Server](../README.md)。本文命令均从 **DataElf 仓库根目录** 执行。

## 1. 运行要求

- Python 3.11 或更高版本。
- Node.js 22.19 或更高版本及 npm；当前仓库锁定的 Pi 包要求 Node.js >=22.19.0。
- 可以访问配置的 AI Index 和 OpenAI-compatible 意图模型端点，以及 Pi 配置的研究模型端点。
- 使用包含 `.pi/`、`package.json` 和 `package-lock.json` 的仓库部署。

FastAPI 和 Uvicorn 已包含在项目主依赖中，`uv sync` 会一并安装。Python wheel 包含服务模块和资源，但运行 Pi 仍需要 Node.js 和 npm 依赖。

## 2. 安装依赖

```bash
cd /path/to/DataElf
uv sync
npm ci
```

需要运行测试时安装测试依赖：

```bash
uv sync --extra dev
```

也可使用安装脚本；它通过 venv 和 pip 安装项目及 dev 依赖并执行 `npm ci`：

```bash
bash dataelf_server/deployment/install.sh
```

脚本默认使用 `python3.11`，可通过 `DATAELF_INSTALL_PYTHON` 指定其他 Python 3.11+ 可执行文件。

## 3. 服务配置

复用当前 DataElf 的 `dataelf.local.yaml`，不要覆盖已经配置好的本地文件。首次部署、尚无配置时，可以复制示例后编辑：

```bash
cp -n dataelf_server/deployment/dataelf.example.yaml dataelf.local.yaml
```

完整示例见 [dataelf.example.yaml](dataelf.example.yaml)。主要配置项：

| 配置 | 说明 |
| --- | --- |
| `runtime.workspace_dir` | 共用状态根目录，默认 `.dataelf` |
| `domains.ai_index.source` | AI Index 地址、凭据及来源模式；真实部署使用 `mode: api` |
| `explorer.pi` | Pi binary、cwd、provider/model、主执行超时等；服务要求 `mode: json` |
| `env` | 共用的模型凭据与子进程环境配置 |
| `server.source.max_pages` | 每来源最大扫描页数，默认50，范围1～1000；较早日期可按数据量提高 |
| `server.intent` | 独立的意图模型名、地址、凭据和超时 |
| `server.host` / `server.port` | 默认 `127.0.0.1:8000` |
| `server.max_concurrent_jobs` | 同时执行任务数，默认 5，允许 1～5；包括意图识别、采集、研究和重试阶段 |
| `server.state_dir` | 默认 null，派生为 `<runtime.workspace_dir>/server` |
| `server.pi` | 服务传输兼容模式及一次合成重试超时 |

在现有配置中增加或调整 server 段：

```yaml
server:
  host: 127.0.0.1
  port: 8000
  state_dir: null
  max_concurrent_jobs: 5
  intent:
    model_name: glm-5.2-1m
    base_url: null
    api_key: null
    timeout_seconds: 90
    max_tokens: 2048
  pi:
    transport: inherit
    synthesis_retry_timeout_seconds: 1800
```

通过 `--config` 或 `DATAELF_CONFIG_FILE` 显式选择配置；未指定时按当前 DataElf 规则依次发现根目录的 `dataelf.local.yaml/yml`、`dataelf.yaml/yml`，再查找 `.dataelf/config.yaml/yml/json`。相对路径以启动目录为准，启动脚本会先进入仓库根目录。

常用环境覆盖：

| 环境变量 | 用途 |
| --- | --- |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` | 意图模型的地址/凭据默认值及采用这些变量的 Pi provider |
| `AI_INDEX_BASE_URL` / `AI_INDEX_API_KEY` | 当前 AI Index 数据源地址/凭据 |
| `DATAELF_PI_MODEL` | 当前 Pi 的 `provider/model-id` |
| `DATAELF_PI_TIMEOUT_SECONDS` | Pi 主执行超时 |
| `DATAELF_SERVER_HOST` / `DATAELF_SERVER_PORT` | 监听地址和端口 |
| `DATAELF_SERVER_MAX_CONCURRENT_JOBS` | 覆盖同时执行任务数，默认及上限均为 5 |
| `DATAELF_SERVER_STATE_DIR` | 独立服务状态目录 |
| `DATAELF_SERVER_SOURCE_MAX_PAGES` | 覆盖每来源扫描上限，例如500；不改变查询日期窗口 |
| `DATAELF_SERVER_PI_TRANSPORT` | `inherit` 或 `nonstream` |
| `DATAELF_SERVER_PI_SYNTHESIS_RETRY_TIMEOUT_SECONDS` | 合成重试超时 |

不再读取旧服务的 `.env` 启动逻辑或 `DATAELF_API_*` 变量。旧 `DATAELF_API_HOST/PORT/STATE_DIR` 分别改用 `DATAELF_SERVER_HOST/PORT/STATE_DIR`；旧 `DATAELF_PI_SYNTHESIS_RETRY_TIMEOUT_SECONDS` 改为上表带 SERVER 的变量。旧 Ontology 模型环境参数不用于当前 Scope V2 确定性建模。

## 4. 模型配置

意图模型与研究模型分别选择：

1. `server.intent.model_name` 必须显式指定。`base_url/api_key` 未填时读取 `OPENAI_BASE_URL/OPENAI_API_KEY`，进程环境优先于配置的 `env`；显式 server.intent 字段优先于环境默认值。意图地址支持 `/v1` 基址或完整 `/v1/chat/completions`。
2. 研究模型使用 `explorer.pi.model` 的 `provider/model-id`，例如 `openai/glm-5.2-1m`。provider 必须在 Pi 注册表中存在；仓库部署通常使用 `.pi/agent/models.json`，由 `PI_CODING_AGENT_DIR` 选择 agent 目录。模型 ID、协议、上下文窗口和 reasoning 兼容参数按实际模型配置。
3. Pi provider 的端点和凭据按其注册表读取；使用 OpenAI-compatible provider 时通常配置 `/v1` 基址。修改意图模型配置不会自动修改 Pi 模型。

PJLab 的 `glm-5.2-1m` 已注册在 `.pi/agent/models.json`，研究模型选择
`explorer.pi.model: openai/glm-5.2-1m`，地址为 `https://token.pjlab.org.cn/v1`。
在启动服务的同一终端设置 `OPENAI_API_KEY` 为该平台的 Key；注册表中的
`"apiKey": "OPENAI_API_KEY"` 是环境变量引用，无需替换成真实 Key。
意图模型选择 `server.intent.model_name: glm-5.2-1m`、
`server.intent.base_url: https://token.pjlab.org.cn/v1`，并将
`server.intent.api_key` 保持为 `null`，即可共用该 Key。
确保 `env.PI_CODING_AGENT_DIR: .pi/agent` 指向仓库注册表。当前上下文和最大输出
分别按保守值 128000 / 16384 配置；模型名称中的 `1m` 不代表已验证该端点的上下文上限。

CLI Ontology 的 Stage 1 generator/reviewer 和 Stage 2 compiler/reviewer 也使用
`glm-5.2-1m`，在统一 `ontology/config.yaml` 中选择。它们读取 `OPENAI_API_KEY` 和
`OPENAI_BASE_URL`；本地配置及部署示例的 `env.OPENAI_BASE_URL` 已设为
`https://token.pjlab.org.cn/v1`。如果启动终端还保留旧的 `OPENAI_BASE_URL`，请更新或
取消该变量，否则按现有配置规则，环境变量会覆盖 YAML 中的地址。

使用 swproxy 的环境，先在独立终端按当前环境约定启用 swproxy，再在同一环境启动服务，使 URL 和 key 自动传入。不必将临时凭据复制进配置文件；服务和启动脚本本身不会调用 swproxy。

服务默认 `transport: inherit`，继承 Pi 传输；`nonstream` 是 openai-completions provider 的可选兼容模式。服务按任务创建 Pi 配置副本并显式加载 server 扩展，不改变 CLI 的全局模型选择。有效分析产物可用于一次合成重试，不重新采集或建模。

## 5. 启动服务

前台运行，默认加载仓库根目录的 `dataelf.local.yaml`，监听地址、端口及状态目录从配置读取：

```bash
bash dataelf_server/deployment/start.sh
```

等价 Python 入口：

```bash
python -m dataelf_server --config dataelf.local.yaml
```

`start.sh` 优先查找已安装的 Conda base Python，再检查 PATH 中的 Python；自动选择需满足 Python 3.11+ 且能导入 FastAPI/Uvicorn。脚本不会安装依赖，启动时会打印选中的解释器路径。自定义安装位置可用 `DATAELF_PYTHON` 指定。
本地使用仓库 `.venv` 时运行 `bash dataelf_server/deployment/start_local.sh`。
两个脚本均默认加载仓库的 `dataelf.local.yaml`，`DATAELF_PYTHON` 可覆盖解释器。
启动前自动输出 `[environment]` 检查结果：Python 路径和依赖、有效配置与监听地址、Node/Pi 版本、状态目录临时 SQLite 写入及可用空间。失败时停止启动，不安装依赖，也不输出密钥。检查不调用模型或数据源接口，不保证后续网络调用成功。

并发参数优先级为启动参数 `--max-concurrent-jobs` > 环境变量 `DATAELF_SERVER_MAX_CONCURRENT_JOBS` > 配置 `server.max_concurrent_jobs` > 默认值 5。仅允许 1～5，修改后重启生效。失败重试进入同一 FIFO 队列，占用同样的并发名额；任务完成顺序取决于各自耗时。保持单个 Uvicorn 进程，不要用 `--workers 5` 代替任务并发。关停时取消所有运行中的任务并标记排队任务为失败。

指定监听地址、端口和状态目录：

```bash
bash dataelf_server/deployment/start.sh \
  --config dataelf.local.yaml \
  --host 0.0.0.0 --port 8000 \
  --state-dir .dataelf/server \
  --max-concurrent-jobs 5
```

客户端将 `127.0.0.1` 换成实际服务地址。示例端口不代表当前已有进程运行；启动前确认端口未被其他实例占用。不要使用多个 uvicorn workers，也不要让两个进程共享同一状态目录；状态目录由实例锁保护。

启动会校验配置、数据源、意图配置、Pi/Node 依赖和包资源。没有 `/health` 路由；`/docs`、`/redoc`、`/openapi.json` 均关闭。可用不存在的任务 ID 检查路由是否可访问，预期返回 HTTP 404 和 `job_not_found` envelope：

```bash
curl -i http://127.0.0.1:8000/api/v1/insight/jobs/job_probe_nonexistent
```

正式部署可参考 [systemd 示例](dataelf-server.service)，修改部署账户、仓库路径、配置路径和 EnvironmentFile 后使用。该模板不自动初始化 swproxy；受管服务需要配置它自己的环境。

## 6. 停止和重启

前台终端按 `Ctrl+C`。也可以对已确认的服务 PID 使用：

```bash
bash dataelf_server/deployment/stop.sh SERVER_PID
```

systemd 部署使用 `systemctl stop dataelf-server`。停止服务会终止当前任务及其登记的子进程，并清理排队任务；这些任务记为 `failed`，原因 `service_shutdown`。网络调用可能需要等到单次请求超时才退出。已完成结果保留。

**目前没有取消单个任务的公开接口。停止服务会影响该实例所有未完成任务。** 不要通过直接修改 SQLite 来取消排队或运行中的任务。重启后可对 failed 任务调用 retry；异常退出遗留的 queued/running 会标记为重启失败，不自动恢复执行队列。

配置修改需要重启。历史数据库/workspace 不会从旧 DataElfAPI 自动导入，启动新服务也不会替换旧服务或切换流量。

## 7. 调用与验收

完整 curl 示例、状态码及三条分段指令见 [API_USAGE.md](../API_USAGE.md)。先启动服务，再运行真实 API 验收：

```bash
.venv/bin/python -m dataelf_server.deployment.smoke \
  --base-url http://127.0.0.1:8000 \
  --queries-file /path/to/queries.json \
  --output .dataelf/server-smoke-report.json
```

queries.json 格式为 `[{"query":"查询今天的GitHub数据。\n# 写作要求\n总结并分析，最多3条。"}]`，也可重复使用 `--query`。脚本提交任务、轮询并记录最终公开响应，有失败或超时则退出非零。该命令会实际调用已配置的数据源和模型。

离线回归入口：

```bash
.venv/bin/python -m pytest -q
```

## 8. 运行记录与排查

默认路径如下；自定义 state_dir 后将 `.dataelf/server` 替换为实际目录：

```text
.dataelf/server/
  jobs.sqlite
  instance.lock
  logs/server.log
  workspaces/<job_id>/
    request.json
    attempts/0001/
      job_spec.json
      logs/request_input.json
      scope_v2/<run>/
      raw/ai_index/
      modeling/server/
      prompts/insight_output_contract.json
      scripts/ tables/ notes/ deep_dives/ insights/ logs/ reviews/
      artifact_manifest.json
      workspace_index.json
```

| 要查看的内容 | 文件 |
| --- | --- |
| 原始请求与完整意图解析 | 当前 attempt 的 `logs/request_input.json` |
| 结构化执行任务、intent_input、output、采集计划 | `job_spec.json` |
| 原始分页响应、过滤结果及空来源警告 | `scope_v2/<run>/` |
| 用户写作字段与补齐默认值 | `prompts/insight_output_contract.json` |
| 条数、篇幅和来源覆盖检查 | `reviews/writing_review.json` |
| 任务执行与模型日志 | 当前 attempt 的 `logs/` |
| 对外最终结果 | GET result；验收脚本还会保存到指定 output 文件 |

retry 沿用原 job ID，但创建 `attempts/0002/` 等新目录，旧产物保留。查询默认展示最新 attempt，不再采用旧服务的 `job_xxx(1)` 归档方式。

正常任务保存上述追踪文件；意图模型完整 HTTP 请求/原始返回并非默认逐次单独存档。需要独立查看解析结果时可运行意图模块入口（[用法](../intent/README.md)），它只打印结果。之前真实测试的额外 model_request/model_response 文件属于测试记录，不是通用部署自动生成的文件。

当前意图由 LLM 解析；明确日期/区间精确过滤，不回退较早有数据的日期。部分来源为空时继续使用其余来源，全部为空时失败。写作分段、语言、条数及跨资料综合规则见 [API 调用说明](../API_USAGE.md) 和 [内容与写作配置](writing.md)。

历史日期验收时，若来源记录显示 `scan_complete=false` 且达到页数上限，表示扫描尚未覆盖完整窗口，不能据此认定当天没有数据。可提高 `server.source.max_pages`（或 `DATAELF_SERVER_SOURCE_MAX_PAGES`）并重试；页数增大会增加采集时间和请求量。2026年9月10日的8月历史样例复测使用500页上限。
