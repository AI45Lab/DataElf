# Serve 检索可靠性

意图提取契约 v3 区分 `research_topics` 与 `retrieval`。默认段的“关注、研究、
分析某生态”等方向进入 `research_topics`，经 Scope plan 和写作契约传入分析，
不作为完整短语硬过滤；分析仍须选取相关证据、说明缺口，不可假定所有采集记录都相关。

严格条件继续保留：`retrieval.keywords` 是明确要求记录包含的字符串，
`entities` 是明确对象限制，`exclude_keywords` 是排除条件；新增
`title_keywords` 只匹配标题。正向组内 OR、组间 AND，排除条件一票否决。
匹配不区分大小写，不自动翻译、拆字、扩词或改变日期/来源。

旧意图和 Scope plan 可继续读取，缺失的研究主题和标题条件按空列表处理。
旧 `keywords/entities` 保持原有严格语义，不因升级自动转为研究主题。
新模型请求的 schema 显式要求返回新字段。原始请求和意图只存入本 job 审计资料。

## 采集诊断与空结果

每个来源的 `diagnostics` 保存 `records_returned`、`records_with_valid_date`、
`records_in_date_range`、`records_after_retrieval`、`records_retained`，依次表示
返回、日期有效、日期范围内、严格条件匹配及最终去重保留记录数。
同时保存 `requests_attempted`、`stop_reason`、`scan_complete`、`reason` 和
`observed_reason`。计数仅代表实际扫描数据；分页上限不证明历史范围无数据。

停止原因包括 `source_exhausted`、`before_date_window`、`page_limit` 和
`source_request_failed`。日期下界停止延续接口按时间降序返回的扫描约定。
`scan_complete` 描述扫描是否按该约定完成，不保证缺失日期的记录属于窗口外。
`requests_attempted` 统计执行器的分页调用，不能冒充来源客户端内部重试后的
精确 HTTP 尝试数；没有底层尝试日志时，额外 HTTP 重试次数应标记为未知。
原始页、成功来源及失败来源的部分计数都会保留；前序失败而未执行的来源标为 `not_requested`。

HTTP envelope 与 error 字段结构保持不变。新增机器可读 `error.reason`：
`source_empty`、`invalid_or_missing_dates`、`no_date_match_in_scanned_records`、
`strict_conditions_no_match`、`scan_incomplete`、`no_matching_records`。
各来源具体计数可见 `error.details.source_diagnostics`；不包含凭据或原始正文。
请求失败继续沿用来源错误分类；无诊断的旧结果保留旧 `no_source_data` 兼容处理。
重试、修订严格条件、检查无效日期、提高扫描上限是不同的处理方向，不统一要求改日期。

## 意图调用的有限瞬时重试

`server.intent.max_attempts` 默认 3，最大 3；只重试 HTTP 429、502、503、504。
401、403、参数错误、其他 HTTP 状态、传输故障及无效模型输出不盲目重试。
合法的 `Retry-After` 秒数或 HTTP 日期优先，否则采用带抖动的指数退避，
起始秒数由 `retry_backoff_seconds` 控制（默认 0.5）。

请求、退避与验证共享原有 `timeout_seconds` 阶段总期限，每次 socket 超时只获得
当时剩余时间。合法退避长于剩余期限时直接结束，不缩短服务器要求后提前重试。
等待和请求过程中均检查取消；取消后停止该阶段、丢弃迟到响应，不再采集或创建新 job。
尚未返回响应的连接操作受原剩余 socket 超时约束；已开始读取的 HTTP socket 会关闭。

同一工作区 `logs/intent_requests.json` 保存本地关联 ID、模型、阶段预算、尝试数、
各尝试 HTTP 状态/耗时/剩余时间、服务请求 ID、有限且脱敏的错误摘要及可得 usage。
不保存密钥、请求头、完整请求或无界响应正文。错误正文最多读取 4096 字节，
错误摘要最多 360 字符；成功响应最多 1 MiB，超限按无效响应处理。
重试不会重新创建任务，也不会重复已完成的采集或建图。客户端重试不能修复服务端故障；
最终仍可能因持续 503、限流、期限不足或无效输出失败。

## 工具与输出额度补充

原生模式下仅收到正文工具标签且没有原生调用时，不执行正文，诊断码为 `PI_NATIVE_TOOL_CHANNEL_INCOMPATIBLE`。公开 envelope 保持不变，错误分类为 `model_error`、reason 为 `native_tool_call_missing`、action 为 `contact_administrator`。协议在请求前选定，不由失败自动切换。

服务默认 `server.pi.tool_protocol=model_compat`：仅当所选模型注册明确声明 `api=openai-completions` 且 `compat.thinkingFormat=zai` 时使用 `glm_text`，其余使用 `native`。这是旧服务配置行为的服务内恢复，不按模型名称猜测，也不修改共享注册。显式 `native` 或 `glm_text` 优先；`glm_text` 要求 OpenAI-compatible API。每个会话的 `logs/pi_protocol.json`（重试使用自己的前缀）记录配置、最终选择与依据。

服务专用 `server.glm_text.v2` 支持单个完整 `<tool_call>`，参数由 `<arg_key>…</arg_key>` 和 `<arg_value>…</arg_value>` 表达；协议明确支持的 `</arg_val>` 缩写终止符作为明确兼容语法加入。没有终止符、未知拼写、混合正文、截断、取消、未提供的工具、非法参数和重复批次仍拒绝。原生调用优先，存在原生调用时不再执行正文版本。CLI、Trajectory 和 Finance 不加载此协议。

`server.intent.max_tokens` 和 `server.pi.max_output_tokens` 默认未指定。仅显式正整数传给模型；默认最终 HTTP 请求不发送任何输出额度字段，模型服务本身仍可能有限制。该策略只作用于服务自己的请求，不改动共享模型注册和其他 domain。
