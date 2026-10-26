# 工作流选择与兼容范围

## AI Index CLI

新研究流程必须显式选择 `ai_index_cli_skill_review_v1`，默认仍为 `legacy`。

```bash
dataelf run --domain ai_index --workflow-policy ai_index_cli_skill_review_v1 \
  --modeling "根据可获取的来源开展研究，提交有证据支持的中文分析"
dataelf run --domain ai_index --workflow-policy ai_index_cli_skill_review_v1 \
  --no-modeling "根据可获取的来源开展研究，提交有证据支持的中文分析"
```

开启建模时，domain 内的来源 Skill 获取证据，modeler 生成可执行契约，确定性引擎验证候选图，独立 reviewer 审核后发布版本。分析继续通过完整报告快照提交和独立审核完成。关闭建模时只进行来源分析及分析审核，不创建或发布图。

该显式流程不设置累计时间、请求、token 或审核轮数上限；单次模型服务限制、工具查询边界和用户取消仍有效。角色保留检查点和真实错误，不将截断输出视为有效工具调用。图版本、来源 hash 和审核绑定实际交付产物。

AI Index CLI 不再接受旧模板选择或导出入口。已有正式图版本可通过相同 workflow policy 下的 `--resume-graph-manifest` 继续分析。共享 Python API 的模板能力不因该 CLI 入口变化而删除。旧建模配置名保留迁移提示；新配置使用根级 `ontology`。

## HTTP serve

服务入口、运行资源和预算独立于 CLI。Scope V2 根据意图选择来源，使用服务自有固定模板建图，再进行分析、校验及结果交付。根级 `ontology.enabled` 不改变服务建模策略；固定建模阶段不调用模型，意图和分析阶段仍调用模型。

```bash
python -m dataelf_server --config local-serve.yaml \
  --host 127.0.0.1 --port 8000 --state-dir .dataelf/serve
```

通过 `POST /api/v1/insight/jobs` 提交 `query`，轮询 `GET /api/v1/insight/jobs/{job_id}`，终态后读取 `GET /api/v1/insight/jobs/{job_id}/result`。提交的 HTTP 202 只代表接受任务。完整输入和返回格式见 [API 使用说明](../dataelf_server/API_USAGE.md)。

研究方向与严格检索条件分别传递，采集诊断区分空来源、日期不匹配、过滤后为空和分页不完整。意图请求仅对配置允许的瞬时 HTTP 故障有限重试，见 [检索可靠性说明](../dataelf_server/deployment/retrieval_reliability.md)。

`server.pi.tool_protocol` 默认 `model_compat`，只有模型注册声明支持时使用相应文本工具协议；显式 `native` 或 `glm_text` 优先。完整调用会经过当前阶段工具名及参数校验，截断、非法参数和不完整封装不执行。

服务对确认的完整响应工具封装错误提供独立的协议纠错额度：`server.pi.protocol_repair_attempts` 默认 2，设为 0 关闭。纠错沿用当前分析上下文，不重做采集、建图或已执行调用；额度持久化到工作区，重建适配器不重置。鉴权、传输故障、length 和取消不进入该纠错流程。耗尽时返回 `tool_protocol_repair_exhausted`，保留已有产物；原 HTTP envelope 和服务预算策略不变。

## Trajectory 与 Finance

两者继续使用各自的 legacy 入口、来源协议、预算、输出结构及审核。根级 ontology 开关不为它们启用 AI Index 来源 Skill、图工具或新分析 reviewer；使用根级关闭及 domain 自身 `modeling.enabled: false` 的原流程配置。

```bash
dataelf run --domain trajectory_analysis --workflow-policy legacy --no-modeling "分析真实轨迹"
dataelf run --domain finance --workflow-policy legacy --no-modeling "分析提供的财务资料"
```

本配置保留领域读取限制和校验，不将 `reward=0` 当作失败原因，也不将 Finance 结构审核当作财务事实核实。各 domain 的安装和输入要求见对应 README。
