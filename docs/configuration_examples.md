# 示例配置填写

示例只交付结构和功能开关。模型服务地址、模型与 provider 标识、认证信息和用户数据路径均置空；本地实际值不属于示例。

| 示例 | 本地副本 | 必填内容 |
|---|---|---|
| `dataelf.local.yaml.example` | `dataelf.local.yaml` | `explorer.pi.model`，所选 domain 的来源设置 |
| `.pi/agent/models.json.example` | `.pi/agent/models.json` | provider 的空键名、`baseUrl`、认证方式、模型 `id` 和 `name` |
| `dataelf/ontology/config.yaml.example` | `dataelf/ontology/config.yaml` | 可沿用默认角色继承；独立角色模型按需填写 |
| `dataelf_server/deployment/dataelf.example.yaml` | 单独的私有 YAML | Pi 模型、AI Index 来源、`server.intent.model_name` 和模型连接方式 |

YAML 和 JSON 示例可以解析；空值不代表可直接运行。Pi 注册表要求非空 provider、模型 ID 和服务地址。复制后用自己的 provider 名替换空键，填写模型 ID/name，再将 `explorer.pi.model` 设为对应的 provider/model。`api: openai-completions` 是合法协议标识，应保留；使用其他协议时按服务实际能力配置。

模型凭据优先通过启动进程的环境提供。在本地 Pi 注册表中为 `apiKey` 设置 Pi 支持的环境变量引用，不要将实际密钥写入版本管理文件。`server.intent.base_url` 和 `api_key` 为空时分别读取 `OPENAI_BASE_URL`、`OPENAI_API_KEY`，但 `server.intent.model_name` 必须填写裸模型 ID。不要在 YAML `env` 中添加空认证变量覆盖已有环境值。

AI Index 来源须填写自己的 API 基址；`AI_INDEX_BASE_URL` 和 `AI_INDEX_API_KEY` 可覆盖来源配置。不要将私有地址、认证头或 token 写入示例或文档。若网关需要特定工具协议、reasoning 兼容设置或输出限制，按已确认能力配置 Pi 的 `compat` 和服务参数，不根据模型名称推测。

Ontology 的空角色模型继承 `explorer.pi.model`。示例文件保留共享 API 的 bounded 默认策略；显式 AI Index CLI 策略会选择无累计预算的研究流程。不要将该策略复制到其他 domain 或服务配置以改变原有预算。

Finance 文件分析须填写 `domains.finance.source.files.data_path`；不使用该 domain 时可以移除其配置段。Trajectory 需安装 `.[trajectory]`，并在启动进程中提供 `WT_SDK_DB_URI`、`WT_SDK_S3_ENDPOINT`、`AWS_ACCESS_KEY_ID` 和 `AWS_SECRET_ACCESS_KEY`。这些字段的实际值只属于本地环境。

从仓库根目录运行 `dataelf setup` 准备 Pi 资源。复制配置前先检查本地目标文件是否已存在，保留已有文件；使用 `DATAELF_CONFIG_FILE` 或服务的 `--config` 选择独立配置。
