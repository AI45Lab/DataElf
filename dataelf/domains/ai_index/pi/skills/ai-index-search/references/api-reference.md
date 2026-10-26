# AI Index OpenAPI 参考

## 协议

- 生产基础地址： `https://index.shlab.org.cn/api/v2`
- 鉴权头： `X-AI-Index-Key: <key>`
- POST 内容类型： `application/json`
- 成功响应：核对根字段 `code == 0`，再读取 `data`
- 分页：`page` 从 1 开始；`size` 通常为 1–50，Hugging Face 为 1–20
- Key 没有正数自定义 QPS 时默认 QPS 为 10，令牌桶突发容量为 QPS 的两倍。

使用 `X-AI-Index-Key`，不要改成 `Authorization: Bearer` 或 `X-API-Key`。

## 研究来源接口

### POST `/openapi/scholar/search`

请求字段：

- `name`: 姓名部分匹配。
- `institutions[]`: 机构名部分匹配。
- `domains[]`, `sub_domains[]`.
- `journal_name[]`, `conferences[]`, `award_list[]`: 匹配完整及缩写的会议、期刊或奖项名称。
- `country_code[]`: 国家代码精确匹配。
- `page`, `size`.

主要响应字段： `scholar_id`, `display_name`, `institution`, `institution_id(s)`, `institutions`, `paper_count`, `first_author_paper_count`, `corresponding_paper_count`, `cited_by_count`, `heat`, `domains`, `sub_domains`, 会议、奖项、主页和存在时的邮箱。

### POST `/openapi/paper/search`

请求字段： `title`, `institutions[]`, `domains[]`, `sub_domains[]`, `journal_name[]`, `conferences[]`, `award_list[]`, `min_publish_date`, `min_citation_count`, `sort_type`, `page`, `size`.

`sort_type`: `heat`, `citation_count`, or `publish_time`.

主要响应字段： `paper_id`, `title`, 作者及其角色、机构 ID 和名称， `pub_date`, `heat`, `cited_by_count`, 领域标签、会议或期刊、奖项、摘要、落地页及 PDF URL。

### POST `/openapi/institutions/search`

请求字段： `keyword`, `domains[]`, `sub_domains[]`, `journal_name[]`, `conferences[]`, `award_list[]`, `country_code[]`, `has_funding`, `sort_type`, `page`, `size`.

- `has_funding`: `has_funding` or `no_funding`.
- `sort_type`: `index`, `paperCount`, `scholarCount`, or `funding`.

响应包含 `institution_id`, 名称、简介、领域、论文和作者数量、人员规模展示及 `index_radar_display`: `academic_impact`, `capital_signal`, `innovation_power`, `media_momentum`, `talent_momentum`, `total_score`, 展示值归一化到 0–100。

### GET `/openapi/institutions/{institution_id}/funding-profile`

融资路径使用准确机构 ID；可通过机构查询取得。响应包含：

- `summary`: 融资总额及次数、投资方数、对外投资次数。
- `funding`: 财务摘要、融资轮次和投资者。
- `invested`: 机构的对外投资。

金额包含 `currency`, 原始 `value`，以及存在时的 `value_usd`。缺失是数据缺口，只有接口明确返回零才能当作零。

## 新闻接口

### POST `/openapi/news/search`

请求字段： `news_ids[]`, `institution_ids[]`, `institutions[]`, `title`, `person`, `filter_type`, `domains[]`, `persons[]`, `products[]`, `date_start`, `date_end`, `page`, `size`.

日期格式为 `YYYY-MM-DD`。响应可包含机构关联、发布者、标题、URL、日期、正文、提取的人物、领域、产品，以及存在时的融资信息和政策分析。

## 生态接口

所有生态查询接收 `institution_ids[]`, `institutions[]`, `date_start`, `date_end`, `filter_type`, `domains[]`, `persons[]`, `products[]`, `page`, and `size`.

| 接口与命令 | 附加筛选和排序 | 主要响应信号 |
|---|---|---|
| `/openapi/ecosystem/gh/search` / `github-search` | `sort_type=heat/time` | 仓库标题、所有者、stars、forks、watches、主题、语言、简介、URL 和汇总数 |
| `/openapi/ecosystem/hf/search` / `huggingface-search` | `asset_type=all/model/dataset/space`, `sort_type=heat/time` | 下载、关注、点赞、发布者、标签、资产类型、URL，以及模型、数据集和 Space 汇总 |
| `/openapi/ecosystem/xx/search` / `twitter-search` | `sort_type=publish_time/view` | 帖子正文、博主、浏览、点赞、分享、评论、媒体 URL、arXiv ID 和人才流动提取 |
| `/openapi/ecosystem/y2b/search` / `youtube-search` | `sort_type=publish_time/view`；默认 size 为 10 | 视频和博主指标、字幕或简介、主题、结论、技术概念和 URL |
| `/openapi/ecosystem/hn/search` / `hackernews-search` | `sort_type=heat/time` | 帖子标题、摘要、分数、评论、互动热度和 URL |

记录也可能包含机构、组织、领域、人物、产品、融资及政策的机器提取关联。表述为“AI Index 关联或提取为”，不直接当作独立核实的事实。

## 查询示例

查询机构 ID，再按机构筛选相关内容：

```json
{"keyword":"Anthropic","page":1,"size":5}
```

```json
{"institution_ids":["resolved-id"],"date_start":"2026-08-01","date_end":"2026-08-31","page":1,"size":20}
```

按领域筛选融资新闻：

```json
{"filter_type":"financing","domains":["Large Language Models"],"date_start":"2026-08-01","date_end":"2026-08-31","page":1,"size":20}
```

按热度查询 Hugging Face 模型：

```json
{"asset_type":"model","sort_type":"heat","page":1,"size":20}
```

## 错误处理

- `401 missing_api_key`: 由宿主向进程环境提供 `AI_INDEX_API_KEY`。
- `401 invalid_api_key_format`: 格式须为 `ak_` 加 16 位 ASCII 字母数字。
- `401 invalid_api_key`: Key 格式正确但服务无法识别。
- `403 api_key_not_active`, `api_key_disabled`, `api_key_expired`: 在以下地址管理 Key： `https://index.shlab.org.cn/api-key`.
- `429 qps_limit_exceeded`: 按带抖动的指数退避处理，不大规模并发查询。
- 非 JSON 或 5xx：记录失败并保留 trace ID，不编造结果。
