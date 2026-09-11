# DataElf Server 使用说明

提交自然语言指令后，服务会查询相关资料，生成带有来源链接的总结和分析。本文分为意图识别和 API 使用两部分。

## 一、意图识别

请求内容分为两部分：前面说明查什么，`# 写作要求` 后说明怎么写。

### 1. 通用要求

写明查询的时间、来源，以及需要关注的主题或对象。

可以直接指定 GitHub、Hugging Face、Twitter/X、YouTube，也可以使用模块名：快讯对应新闻，观点对应 Twitter/X，开源社区对应 GitHub 和 Hugging Face，传播对应 YouTube，综合总结覆盖全部来源。

多个来源或模块可以组合使用；未指定来源时，默认查询全部来源。

### 2. 写作要求

另起一行填写 `# 写作要求`，在下面说明总结、分析或比较的任务，以及目标读者、分析重点、语言、篇幅、条数等要求。没有特别要求时，可以省略这一部分，使用默认写法。

**日期、来源和检索主题写在标题前；标题后只写内容分析与表达要求。**

### 示例一：综合总结

```text
查询2026年8月22日的数据，生成综合总结。

# 写作要求
面向管理和决策人员，总结并分析当天值得关注的变化。
围绕共同主题综合多条信息，归纳1至3个关注点。
重点分析技术、产品和产业动向及其可能影响。
不要只是改写标题或罗列数字。
```

### 示例二：快讯

```text
查找2026年8月20日至2026年8月22日与OpenAI有关的快讯。

# 写作要求
用中文总结，按事件组织内容。
最多输出5条，每条正文约100字。
表达保持客观、简洁，重点说明发生了什么变化。
```

### 示例三：观点

```text
查询2026年8月3日的观点模块数据，来源为Twitter/X。

# 写作要求
面向技术负责人，综合多条观点形成分析判断。
重点讨论企业采用AI的障碍及其可能影响。
最多形成3条结论，每条正文控制在120至180字。
不要机械地把每条推文改写成一条总结。
```

### 示例四：开源社区

```text
查看2026年8月7日的开源社区数据，包括GitHub与Hugging Face。

# 写作要求
比较检索结果中项目和模型的使用门槛、部署成本与生态支持。
面向开发者，用英文撰写，保持专业、简洁。
最多输出3条，每条正文不超过100个英文单词。
避免只罗列项目名称或热度数字。
```

### 示例五：传播

```text
获取2026年8月3日传播模块的YouTube视频数据。

# 写作要求
逐条概括视频内容，面向普通读者，用通俗中文表达。
最多输出3条，每条正文约150字。
重点说明视频讨论的核心问题，不要只是改写视频标题。
```

## 二、API 使用

### 1. 提交任务

将自然语言指令放入 `query`。服务会自动识别来源并完成查询和分析。

```bash
curl -sS -X POST 'http://s-20260908202228-j7jhb.ailab-evobox.pjh-service.org.cn/api/v1/insight/jobs' \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "查看2026年8月7日的开源社区数据，包括GitHub与Hugging Face。\n\n# 写作要求\n面向开发者，总结并分析，最多输出3条。"
  }'
```

JSON 字符串中的 `\n` 表示换行。可以将上面的指令替换为第一部分的任意示例。

例如，“开源社区”会选择 GitHub 和 Hugging Face；“综合总结”会选择新闻、Twitter/X、GitHub、Hugging Face 和 YouTube。

未指定条数时最多返回 10 条有效 Insight；明确指定条数时按写作要求处理，实际数量以可支持的有效内容为准。

提交成功返回 HTTP 202，从响应的 `data.job_id` 取得任务 ID。202 表示任务已接受，结果需要随后查询：

```json
{
  "code": 0,
  "msg": "accepted",
  "trace_id": "trace_xxx",
  "data": {
    "job_id": "job_xxx",
    "status": "queued",
    "created_at": "2026-09-11T08:00:00Z"
  }
}
```

### 2. 查询任务进度

将 `<job_id>` 替换为提交接口返回的任务 ID：

```bash
curl -sS 'http://s-20260908202228-j7jhb.ailab-evobox.pjh-service.org.cn/api/v1/insight/jobs/<job_id>'
```

任务状态为 `queued`、`running`、`completed` 或 `failed`。服务默认最多同时处理 5 个任务，其他任务按提交顺序排队。

响应中的 `created_at` 是创建时间，`started_at` 是后台实际开始处理的时间；排队时 `started_at` 为 `null`。时间使用 UTC。

```json
{
  "code": 0,
  "msg": "success",
  "trace_id": "trace_xxx",
  "data": {
    "job_id": "job_xxx",
    "status": "running",
    "stage": "fetching_ai_index",
    "progress": 15,
    "created_at": "2026-09-11T08:00:00Z",
    "started_at": "2026-09-11T08:02:00Z",
    "error": null
  }
}
```


### 3. 获取 Insight

```bash
curl -sS 'http://s-20260908202228-j7jhb.ailab-evobox.pjh-service.org.cn/api/v1/insight/jobs/<job_id>/result'
```

成功时返回 HTTP 200，Insight 位于 `data.insights`。任务尚未完成返回 HTTP 409；任务不存在返回 404；任务失败且无可用结果返回 422。

每条 Insight 包含 Insight ID、标题、正文和来源标题/链接。所有请求使用相同的返回结构：

```json
{
  "code": 0,
  "msg": "success",
  "trace_id": "trace_xxx",
  "data": {
    "job_id": "job_xxx",
    "created_at": "2026-09-11T08:00:00Z",
    "started_at": "2026-09-11T08:02:00Z",
    "insights": [
      {
        "insight_id": "ins_001",
        "title": "洞察标题",
        "content": "完整的洞察正文",
        "sources": [
          {
            "title": "来源文章或帖子的标题",
            "url": "https://example.com/source"
          }
        ]
      }
    ]
  }
}
```

### 4. 重试失败任务

只有 `failed` 任务可以重试：

```bash
curl -sS -X POST 'http://s-20260908202228-j7jhb.ailab-evobox.pjh-service.org.cn/api/v1/insight/jobs/<job_id>/retry'
```

成功接受重试返回 HTTP 202，响应结构与提交任务相同。其他状态返回 HTTP 409；任务不存在返回 404。

重试会沿用原 `job_id` 和原始请求参数，并生成新的 `trace_id`。`created_at` 会重置，`started_at` 在实际开始处理后设置。相对日期按重试执行时重新计算。

需要修改指令时，请重新提交任务。

### 5. 错误返回

所有接口使用 `{code, msg, trace_id, data}` 返回结构。接口错误的顶层 `code` 与 HTTP 状态码一致，例如 `404`、`409`、`422`；接口成功时为 `0`。

后台任务失败时，状态查询本身仍返回 HTTP 200，`data.status` 为 `failed`，具体原因位于 `data.error`：

```json
{
  "category": "source_error",
  "reason": "connection_failed",
  "message": "暂时无法连接数据源，请稍后重试。",
  "retryable": true,
  "action": "retry",
  "stage": "fetching_ai_index",
  "details": null
}
```

任务错误的 `category` 可能为 `intent_error`、`source_error`、`model_error`、`analysis_error`、`artifact_error` 或 `service_error`；请求格式错误、任务不存在等接口错误使用 `request_error`。

调用方应根据 `category`、`reason`、`retryable` 和 `action` 处理，不要解析可能调整措辞的 `message`。反馈问题时请提供 `job_id` 和 `trace_id`。
