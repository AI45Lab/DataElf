---
name: ai-index-search
description: 使用 AI Index 正式 OpenAPI 查询论文、学者、机构、融资、新闻和开源模型生态，解释筛选、分页、来源口径及查询结果。只提供来源方法，不定义研究步骤或交付协议。
---

# AI Index Search

通过 AI Index 正式 OpenAPI 查询结构化 AI 数据。生产域名固定为 `https://index.shlab.org.cn/`，只调用 `/api/v2/openapi/*`，不调用站内页面接口或管理接口。

## 何时触发

以下意图应主动使用本 Skill，而不必等用户点名 `$ai-index-search`：

- 查找或排行 AI 学者、专家、作者、团队成员；
- 查论文、热门论文、高引用论文、最新论文、会议或期刊成果；
- 搜 AI 公司、大学、实验室和研究机构，比较学术、资本、开源、媒体、人才指标；
- 查融资轮次、投资方、对外投资；
- 查近期 AI 新闻、人物、产品、融资或政策事件；
- 查 GitHub 项目、Hugging Face 模型/数据集/Space、Twitter/X 帖子、YouTube 视频、Hacker News 讨论；
- 用户提到 AI Index、AI 指数、`index.shlab.org.cn`，或希望用 AI Index 数据验证判断；
- 复合问题，例如“查 OpenAI 机构与新闻，再找近一周计算机视觉热门论文和学者”。

不适用于实时股价、财报、专利全文、企业私有数据、通用网页搜索，也不要声称数据覆盖整个互联网或实时无延迟。

## 本任务中的查询与凭据

通过 `ai_index_query(command, params, idempotency_key?, refresh?)` 查询，命令和参数沿用下表及参考文档。脚本位于本 Skill 根目录的 `scripts/ai_index_openapi.sh`，路径相对本 Skill 解析，不相对任务目录。宿主内直接执行该脚本也会经过同一来源登记入口。

凭据通过进程环境注入，鉴权头为 `X-AI-Index-Key`，不放在工具参数、报告、命令行、日志或聊天中。`check` 只检查本地 Key 存在及格式为 `ak_` 加 16 位 ASCII 字母数字，不能证明服务已鉴权。

响应分别记录 HTTP 错误、业务非零 code、非 JSON、网络失败、成功空结果和成功有数据。只有 `code == 0` 的成功响应可以登记为建模来源；失败不回退到假数据。空结果仅说明本次条件下未检索到。

每次新查询产生独立批次。传输重试使用同一幂等键；主动刷新使用新键，不覆盖旧响应。查询返回来源 ID、hash 和读取入口；`ai_index_read` 分页读取保存响应的字节，API 的 `page/size` 则向来源取下一页，二者不同。大结果摘要不代表读完完整原文。

按用户任务自主决定查询顺序和范围。API 每页通常最多 50，Hugging Face 最多 20；它们是单次接口限制，不是整个研究的采集上限。学者接口没有显式排序参数。每个接口字段及数据口径见 [references/api-reference.md](references/api-reference.md)。

## 命令与能力

脚本统一格式：

```bash
./scripts/ai_index_openapi.sh COMMAND 'JSON对象'
```

| 命令 | 能力 | 常用筛选 |
|---|---|---|
| `scholar-search` | 学者/专家 | `name`, `institutions`, `domains`, `sub_domains`, `journal_name`, `conferences`, `award_list`, `country_code` |
| `paper-search` | 论文 | `title`, `institutions`, `domains`, `sub_domains`, `min_publish_date`, `min_citation_count`, `sort_type` |
| `institution-search` | 公司/大学/实验室 | `keyword`, `domains`, `country_code`, `has_funding`, `sort_type` |
| `institution-funding` | 单一机构融资和对外投资 | 路径参数 `institution_id` |
| `news-search` | 新闻、人物、产品、融资、政策 | `institutions`, `title`, `person`, `date_start`, `date_end`, `filter_type` |
| `github-search` | GitHub 仓库 | 机构、日期、领域、人物、产品，`sort_type=heat/time` |
| `huggingface-search` | 模型/数据集/Space | 同上，另有 `asset_type=all/model/dataset/space` |
| `twitter-search` | Twitter/X 内容 | 机构、日期、领域、人物、产品，`sort_type=publish_time/view` |
| `youtube-search` | YouTube 内容和提炼观点 | 机构、日期、领域、人物、产品，`sort_type=publish_time/view` |
| `hackernews-search` | Hacker News 讨论 | 机构、日期、领域、人物、产品，`sort_type=heat/time` |

生态与新闻的 `filter_type` 可用于聚焦 `all`、`institution`、`domain`、`person`、`product`、`financing`、`policy`。具体字段和响应说明见 [references/api-reference.md](references/api-reference.md)。

## 示例

### 查机构与融资

```bash
./scripts/ai_index_openapi.sh institution-search \
  '{"keyword":"OpenAI","page":1,"size":5}'

./scripts/ai_index_openapi.sh institution-funding '从上一步取得的institution_id'
```

### 查近一周计算机视觉热门论文

把日期替换为当前窗口起点：

```bash
./scripts/ai_index_openapi.sh paper-search \
  '{"domains":["Computer Vision"],"min_publish_date":"2026-08-28","sort_type":"heat","page":1,"size":20}'
```

“近一周最热”表示按 `heat` 排序且限定最早发表日期，不等同于一周内新增引用最多。

### 查计算机视觉学者

```bash
./scripts/ai_index_openapi.sh scholar-search \
  '{"domains":["Computer Vision"],"page":1,"size":20}'
```

学者搜索当前没有显式排序参数；按 API 返回顺序呈现，不能自行包装成官方 Top 排名。

### 查机构近期多源动态

```bash
./scripts/ai_index_openapi.sh news-search \
  '{"institutions":["OpenAI"],"date_start":"2026-08-28","date_end":"2026-09-03","page":1,"size":20}'

./scripts/ai_index_openapi.sh github-search \
  '{"institutions":["OpenAI"],"sort_type":"heat","page":1,"size":10}'

./scripts/ai_index_openapi.sh huggingface-search \
  '{"institutions":["OpenAI"],"asset_type":"model","sort_type":"heat","page":1,"size":10}'
```

## 结果表达

- 开头写明“基于 AI Index OpenAPI”、查询时间和筛选口径。
- 表格只放可比较字段；正文解释数据缺失和样本边界。
- 每条新闻、论文或生态内容尽量保留源 URL。
- 没结果就明确写“在本次条件下未检索到”，不要补写未经接口支持的事实。
- `401`：Key 缺失、格式错误或无效；`403`：未激活、禁用或过期；`429`：超过 Key 的 QPS，退避后重试。
