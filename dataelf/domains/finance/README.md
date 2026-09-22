# Finance

`finance` 用于多数据来源的财务问答和金融研究。你可以提供 SQLite 数据库、财报或表格文件，也可以启用 SEC EDGAR、行情和网页检索工具，让 Agent 获取证据、完成计算并生成带来源引用的分析结果。普通任务无需选择 benchmark；需要特定评测任务的分析约定时，可使用内置适配配置。

本文是用户使用指南。通用安装、模型与凭据配置见根 [README](../../../README.md)。以下终端命令以**仓库根目录**为工作目录，示例中的路径、公司和模型需替换为自己的输入。

## 安装与配置

首次准备源码环境时，安装 DataElf 并初始化公共 Pi/runtime：

```bash
uv venv
uv pip install -e ".[finance]"
source .venv/bin/activate
dataelf setup
dataelf init
```

`finance` 可选依赖提供 Excel（`.xlsx`、`.xlsm`）和 PDF 解析所需的 `openpyxl`、`pypdf`。只使用 SQLite、CSV、JSON 或在线数据工具时，安装基础包即可。旧版 `.xls` 不在该 extra 的支持范围内；已有环境无需重建，缺少文件解析依赖时补装 `uv pip install -e ".[finance]"` 即可。

将以下片段合入 `dataelf.local.yaml`。这个示例用于分析本地文件，`data_path` 可以是单个文件或目录：

```yaml
explorer:
  type: pi
  pi:
    model: your-provider/your-model
    mode: json
domains:
  finance:
    source:
      files:
        data_path: /absolute/path/to/financial-documents
    tools:
      sql: false
      python: true
    analysis:
      # Finance-only Pi process budget; this example allows 60 minutes.
      max_runtime_seconds: 3600
      # Optional SEC contact identity and helper-model override.
      user_agent: "DataElf Finance (contact: analyst@example.com)"
      retrieve_model: "provider/model"
```

`domains.finance.analysis.max_runtime_seconds` is converted to the job's
`max_runtime_minutes` constraint, so it applies only to Finance jobs. An
explicit `explorer.pi.timeout_seconds` still overrides it for every domain.

模型凭据按公共 provider 配置规则提供；也可用 `DATAELF_CONFIG_FILE` 选择其他本地配置文件。**未选择 benchmark 时，默认只开启 Python**，需要查询数据库时应显式设置 `sql: true`。

## 运行与产物

配置好文件来源后即可运行：

```bash
dataelf run --domain finance \
  "根据提供的财报，比较公司 2023 和 2024 财年的营收与营业利润，计算同比变化，说明币种、单位和数据来源。"
```

任务中尽量明确公司、期间、指标口径和交付要求。例如，指定使用财年还是自然年、报告币种还是换算币种，以及需要简短答案还是完整研究综述。

Finance 当前不提供独立 modeling 阶段：`modeling` 配置段仅为预留占位，`--no-modeling` 等价于默认行为，显式启用（`--modeling` 或 `modeling.enabled: true`）会以 `FINANCE_MODELING_UNSUPPORTED` 报错。

每次运行创建新的 job。先查看运行返回的 workspace 中的 `results/final_brief.md`，再按需读取结构化结果和审核记录：

| 当前 job 内的文件 | 用途 |
|---|---|
| `results/final_brief.md` | 面向读者的结论、依据与局限 |
| `results/results.json` | 便于后续处理的结构化分析结果 |
| `reviews/quality_review.json` | 产物审核状态及警告 |
| `tables/finance/source_manifest.json` | 本次使用的数据源及输入文件清单 |
| `raw/finance/input/` | 本地输入文件的副本；未提供文件时为空目录 |
| `tables/finance/finance.db`、`schema.json` | 提供数据库时生成的副本及表结构 |

`artifact_manifest.json` 和 `workspace_index.json` 记录产物与结果索引。成功运行应生成两份结果文件；失败或中断时，应结合 job 状态和审核记录检查原因。

默认结构化结果包含以下字段：

| 字段 | 内容 |
|---|---|
| `summary` | 问题的答案或研究综述 |
| `key_findings` | 支撑结论的发现列表，可包含逐条证据和计算数据 |
| `evidence_refs` | 来源 URL 或 workspace 内的证据路径列表 |
| `confidence` | 0 到 1 之间的整体置信度 |
| `limitations` | 数据缺口、口径差异及结论适用范围，可以是文字或列表 |

审核主要检查结果结构、必填字段、类型和数值范围。`pass` 不等于财务事实或计算已经独立验证；`pass_with_warnings` 表示仍有需要检查的字段问题。阅读结果时，应结合来源和计算过程判断结论是否得到支持。

## 选择数据源与工具

### 本地数据库和文件

`source.sqlite` 与 `source.files` 可以单独使用，也可以组合。下面的配置适合同时查询财务数据库和读取补充文档：

```yaml
domains:
  finance:
    source:
      sqlite:
        db_path: /absolute/path/to/finance.db
      files:
        data_path: /absolute/path/to/documents
    tools:
      sql: true
      python: true
```

运行准备阶段会把输入复制到当前 workspace；SQL 工具以只读方式查询数据库副本。只使用文件时关闭 `sql`；只使用数据库时可以省略 `files`。

临时更换数据库可通过运行参数指定：

```bash
dataelf run --domain finance \
  --param finance_source='{"sqlite":{"db_path":"/absolute/path/to/finance.db"}}' \
  --param finance_tools='{"sql":true,"python":true}' \
  "查询公司 2024 财年的营收，列出使用的表、筛选条件和单位。"
```

`finance_source` 按段合并已有配置。若之前配置过 `source.fixture`，切换真实数据前需移除该配置及对应环境变量，否则仍优先使用 fixture。

### 大型数据源：链接而非复制

数据源很大（例如数 GB 的 SQLite 语料库）时，逐 run 复制笨重且缓慢。给 `sqlite` 或 `files` 加 `link: true`，准备阶段会在 workspace 中放置指向原始路径的符号链接：零拷贝、即建即删，SQL 工具照常以只读模式查询。

```bash
dataelf run --domain finance \
  --param finance_source='{"sqlite":{"db_path":"/absolute/path/to/finance.db","link":true}}' \
  --param finance_tools='{"sql":true,"python":true}' \
  "查询公司 2024 财年的营收，列出使用的表、筛选条件和单位。"
```

YAML 配置中等价于 `source.sqlite.link: true`（文件源为 `source.files.link: true`）；环境变量 `DATAELF_FINANCE_DB_LINK=1`、`DATAELF_FINANCE_FILES_LINK=1` 也可开启。

存在链接源时 `execute_code` 自动启用只读强制（`tools/readonly_exec.py`）：对链接源的写入、改名/删除以及派生子进程都会被拒绝，错误信息注明 read-only enforcement；`sqlite3.connect` 对链接库自动改写为只读 URI，因此 `read_sql` 等读取正常、`to_sql` 会报 attempt to write a readonly database。workspace 内非链接路径的临时写入不受影响。`analysis.readonly_code: true/false`（或环境变量 `DATAELF_FINANCE_READONLY_CODE`）可强制开关，默认自动：有链接源即开启。

使用链接需注意：

- 运行与审核阶段必须能访问原始路径（同一共享存储即可）；workspace 不再自包含，`source_manifest.json` 会记录 `linked: true` 及目标路径、大小、mtime。
- 母库建议以非 WAL（或已完整 checkpoint）的冻结状态提供，`mode=ro` 打开最干净。
- 只读强制面向模型代码的误写防护，不是安全沙箱（ctypes/mmap 级系统调用不在拦截范围内）。

### 在线财报、行情与网页检索

只使用在线数据时可以省略整个 `source`。以下配置启用 SEC 财报与行情工具：

```yaml
domains:
  finance:
    tools:
      sql: false
      python: true
      edgar: true
      prices: true
```

```bash
dataelf run --domain finance "查询 AAPL 的 2024 财年营收及同比变化，并汇总 2024 自然年的月度收盘价，分别注明财报期间、交易日期和来源。"
```

EDGAR 和行情工具无需单独的 API key，但需要可访问数据源的网络。需要网页搜索时另加 `web: true`，并按公共 web 工具的要求配置检索凭据。

| 开关 | Agent 可用能力 | 适用场景 |
|---|---|---|
| `sql` | `get_database_info`、`describe_table`、`execute_query` | 查看表结构、只读查询财务数据 |
| `python` | `execute_code`、`list_files`、`get_field_description` | 读取文件、整理表格和计算指标 |
| `edgar` | `edgar_search`、`company_profile`、`company_facts`、`parse_html_page`、`retrieve_information` | 检索 SEC 申报、获取公司及 XBRL 数据、解析和定向抽取财报内容 |
| `prices` | `price_history` | 获取 Yahoo Finance 历史 OHLCV，使用 `AAPL`、`0700.HK` 等 ticker |
| `web` | 公共网页搜索与内容获取工具 | 补充研究资料、交叉核对来源 |

工具与 [Finance Skill](pi/skills/finance/SKILL.md) 随当前 domain 自动加载，无需手动注册。`web` 开关用于声明分析时使用的公共能力，不是隔离或禁用底层工具的安全开关。Pi 内置的 terminal 工具（`read`、`bash`、`edit`、`write`、`grep`、`find`、`ls`）随 explorer 始终可用，无需（也无法）通过开关配置。

工具配置的优先级从低到高为：通用默认值或 benchmark 声明 → 本地 `domains.finance.tools` → `DATAELF_FINANCE_TOOLS` → 运行参数 `finance_tools`。环境变量使用逗号分隔的完整能力列表（如 `python,edgar,prices`），未列出的能力会关闭；运行参数只覆盖所列字段。

### 使用示例数据

任意本地 SQLite 库即可体验数据库分析流程（离线 `source.fixture` 分支仍可用：fixture 目录内含 `finance.db` 和可选 `input/`，优先于真实数据库和文件源；完整运行需要配置模型及其访问凭据）：

```bash
dataelf run --domain finance \
  --param benchmark=ddr_10k \
  --param finance_source='{"sqlite":{"db_path":"/path/to/10k_financial_data.db","link":true}}' \
  --param finance_tools='{"sql":true,"python":true,"web":false,"edgar":false,"prices":false}' \
  "Analyze company with CIK 6201"
```

## 使用 benchmark 配置

Benchmark 为特定任务预设工具、分析要求和输出字段，不会自动下载评测数据。需要本地输入的配置仍须提供相应数据源。

| Benchmark | 主要用途 | 必需的本地数据 |
|---|---|---|
| `ddr_10k` | 10-K 财报数据库分析 | SQLite |
| `finfirst` | 定向金融问答 | 无 |
| `finsearchcomp_t2` | 基于网页内容的比较分析 | 无 |
| `finsearchcomp_t3` | 结合网页、PDF 和表格的比较分析 | 无 |
| `frontier_finance` | 多来源综合研究报告 | 文件 |
| `finance_agent_bench_v1_1` | SEC 财报检索与信息抽取 | 文件 |
| `finance_agent_bench_v2` | 财报与历史行情分析 | 文件 |
| `finance_complex_qa` | 复杂文档和表格问答 | 文件 |
| `dataclawbench` | 文件与表格分析 | 文件 |

“无”表示没有必需的本地数据源，仍需满足所启用在线工具的网络和凭据要求；也可以额外提供文件。具体预设见各 [benchmark 配置](benchmarks/)。

```bash
dataelf run --domain finance \
  --param benchmark=finsearchcomp_t2 \
  "比较两家指定公司在同一财年的营收增速，统一口径并引用原始来源。"
```

也可以设置 `domains.finance.benchmark` 或 `DATAELF_FINANCE_BENCHMARK`；优先级为运行参数 > 环境变量 > 本地配置。本地工具开关仍会覆盖 benchmark 默认值，切换任务时应检查是否保留了不适用的覆盖。

需要追加结构化交付字段时，可以使用 `finance_output_fields`：

```bash
dataelf run --domain finance \
  --param finance_output_fields='[{"name":"retrieval_date","prompt_hint":"YYYY-MM-DD"}]' \
  "根据已配置的数据源回答财务问题，并注明资料检索日期。"
```

新字段默认是必填字符串；同名字段用于修改默认定义。运行参数中的字段覆盖列表优先于 benchmark 的覆盖列表，基础结果字段仍会保留。字段定义见 [artifacts.py](artifacts.py)。

## 常见问题与限制

| 现象或错误码 | 检查与处理 |
|---|---|
| `FINANCE_DB_MISSING` | SQL 已开启但没有数据库。提供 `source.sqlite`，或在文件/在线任务中设置 `sql: false`。 |
| `FINANCE_SOURCE_MISSING` | 所选 benchmark 缺少必需数据源，按上表补充数据库或文件。 |
| `FINANCE_SOURCE_INVALID` | 检查配置路径是否存在，以及文件、目录类型是否正确。 |
| `FINANCE_FILE_DEPS_MISSING` | 在运行 DataElf 的 Python 环境中安装 `.[finance]`，然后重新运行。 |
| `FINANCE_MODELING_UNSUPPORTED` | Finance 未实现 modeling 阶段。移除 `modeling.enabled` 或 `--modeling`；占位段仅接受 `enabled`、`ontology_template`。 |
| 在线请求失败或返回 429 | 检查网络、代理和数据源限流；行情工具仅做一次退避重试。 |

使用环境代理访问 EDGAR/行情数据 时，Pi 的 Node 运行时需支持 `NODE_USE_ENV_PROXY`（Node 24+）；启用这些工具时，domain 在该变量未设置时默认设为 `1`。如需关闭环境代理，可在 shell 或根级 `env:` 中显式设置 `NODE_USE_ENV_PROXY: "0"`；配置值会保留并覆盖默认值。可在 `domains.finance.analysis.http_user_agent` 中设置包含联系信息的 SEC 请求标识。

**代理与 TLS 配置。** shell 中导出的 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`/`ALL_PROXY`（含小写变体）和 `NODE_EXTRA_CA_CERTS`/`SSL_CERT_FILE` 会经 explorer 的 env allowlist 透传给 Pi 子进程，无需在本地配置里重复声明；`dataelf` 根级 `env:` 段中的同名键优先级更高，可用于固定覆盖（benchmark 需要不依赖调用方 shell 即可复现时，用它显式声明代理）。注意：

- `NODE_USE_ENV_PROXY` 是进程级的：Pi 的模型 API 流量同样会走代理。若模型端点在内网（如 `OPENAI_BASE_URL` 指向内部 vLLM），必须在 `NO_PROXY` 中列出该主机，否则模型连接会被代理打断。
- Node 的 env-proxy 支持（undici）对 `NO_PROXY` 只做精确主机名/域名后缀匹配，**CIDR 条目（如 `10.0.0.0/8`）会被静默忽略**——内网端点需逐个列出主机名或 IP。
- TLS 拦截型代理需自带 CA：Node 侧导出 `NODE_EXTRA_CA_CERTS`，Python 侧 `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`。

工具结果有数量和长度限制：SQL 查询默认返回 20 行、最多 100 行；行情默认 100 行、最多 1000 行；HTML 正文默认返回 12000 字符、最多 30000 字符。返回中的 `truncated`、`*_truncated` 等标记表示内容未完整展示。遇到截断应缩小查询范围、调整时间粒度或分段抽取，不能把当前返回当作完整数据。

长财报可以先由 `parse_html_page` 保存完整正文，再用 `retrieve_information` 按范围抽取。该工具会额外调用一次模型，默认使用当前模型，也可通过 `domains.finance.analysis.retrieve_information_model: provider/model` 指定。

需要检查本地接入时，在已安装开发依赖和测试所需 runtime 的环境中运行：

```bash
.venv/bin/python -m pytest -q tests/test_finance_domain.py
```

测试覆盖配置、数据准备、工具边界、产物审核和 fake Pi 流程，不代表真实模型分析质量或在线数据源可用性。
