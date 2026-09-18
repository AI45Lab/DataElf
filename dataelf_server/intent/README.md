# LLM 意图识别

当前 serve 通过本模块解析用户输入，再编译为 AI Index 采集计划。CLI research 核心保持原状。

## 自己测试

在仓库根目录、已启用所需 swproxy 环境的终端运行：

```bash
python dataelf_server/intent/run_intent.py $'查询2026年8月22日的数据。\n# 写作要求\n总结并分析，最多3条。'
```

只打印意图 JSON，不保存独立调用记录。可指定 `--config /path/to/dataelf.yaml`、
`--reference-time '2026-09-08T12:00:00+08:00'`、`--timezone Asia/Shanghai`。
从其他目录运行时，可通过绝对路径定位脚本并显式传入配置路径。

```python
from dataelf_server.intent import IntentRecognizer
intent = IntentRecognizer().extract('查询2026年8月7日的GitHub与Hugging Face数据。\n# 总结要求\n总结并分析，最多3条。')
print(intent.model_dump_json(indent=2))
```

每次独立调用一次模型，没有历史对话、自动修复或规则解析回退。

输入采用两段式：默认段提供检索要求，写作类标题下的正文提供 output。
**没有写作类标题时只提取 retrieval/time_range/domains，整个 output 保持空对象。**

```text
查询2026年8月7日的GitHub与Hugging Face数据。

# 写作要求
面向管理和决策人员，总结并分析。
围绕共同主题综合多条资料，形成1至3条有证据支撑的判断。
解释可能影响，不要只是改写标题或罗列数字。
```

只查询时无需标题；要指定输出字段时需要独立的写作类 Markdown 标题行。
模型按标题语义识别，例如 `# 写作要求`、`# 总结要求`、`# 成文规范`、`# 报告撰写偏好`，不做固定词表匹配。
标题只决定分区，不自动产生任务值；只有 `# 总结要求` 而没有正文时 task_types 仍为空。
`# 检索要求`、`# 背景` 等非写作标题仍属于默认段；行内 #、引用的标题文字和代码块中的标题不触发写作分区。
同级或更高级标题重新判定归属，下级标题默认继承父段；多个写作段合并明确要求。

字段不能跨段借用：默认段的“英文总结3条”不会填写 output；写作段的日期、来源、比较对象也不会改变检索条件。
例如默认段“查询GitHub”，写作段“比较OpenAI和Anthropic”只填写比较对象，不把它们自动设为检索实体。
分区和字段抽取都由同一次 LLM 调用完成；未增加对话、模型调用或自然语言规则解析器。
写作区中的越界指令仍然忽略，标题不会改变系统约束的优先级。

代码只扫描 Markdown 标题结构，排除行内 # 和代码块，不判断标题含义。
带标题的输入以 `input_sections` 数组传给模型，显式区分默认正文、标题和各段正文；不改变用户调用参数。
没有标题时请求 schema 将 output 固定为空，本地也执行这个格式约束。
有标题时模型先返回内部 `_writing_sections` 段落 ID，再返回意图字段；该分类结果用于禁用不适用的字段，
例如没有写作段就清空 output、完全只有写作段就保留空检索条件。段落 ID 会校验，内部字段不会出现在 Python/CLI 返回的 Intent 中。
混合段落中的具体字段仍由模型按分区提取；结构检查不能保证所有语义分类永远正确。

## 配置

与服务共用 `dataelf.local.yaml` 的 `server.intent`：

```yaml
server:
  intent:
    model_name: glm-5.2-1m
    base_url: null
    api_key: null
    timeout_seconds: 90
    max_tokens: 2048
```

`model_name` 必须指定。`base_url/api_key` 显式配置时使用配置值，未配置时读取
`OPENAI_BASE_URL/OPENAI_API_KEY`（进程环境优先于配置中的 `env`）。
地址支持 API 基址 `/v1` 或完整 `/v1/chat/completions`，不会重复拼接。
模块不主动执行 swproxy；由启动服务或运行测试的终端提供环境。
意图识别与后续 Pi 分析分别使用 `server.intent` 和 `explorer.pi` 配置。

## 字段及默认值

```json
{
  "retrieval": {"keywords": [], "entities": [], "exclude_keywords": []},
  "time_range": {"start_date": null, "end_date": null},
  "domains": [{"id": "ai_index", "sources": []}],
  "output": {
    "task_types": [],
    "focus_points": [],
    "comparison": {"subjects": [], "dimensions": []},
    "audience": null,
    "language": null,
    "style": [],
    "item_count": {"target": null, "min": null, "max": null},
    "body_length": {"scope": null, "target": null, "min": null, "max": null, "unit": null},
    "synthesis": {"organization": null, "evidence_mode": null},
    "content_rules": {"required": [], "avoid": []}
  }
}
```

JSON Schema 要求所有字段存在，限定来源标识；本地校验有效日期、起止顺序和来源关系。
未提及的信息保留默认值。`sources=[]` 代表未指定，不代表模型识别出了全部来源。
输出字段由 Serve 消费并与场景默认值合并，贯穿候选信号分析、最终写作、校验与合成重试；
原始用户 query 仅用于追踪，不再作为后续研究指令。详见 [Serve 写作接入](../deployment/writing.md)。

### Output v2 字段契约

本版删除 `output.requirements`，不接受旧自由指令列表，也不提供 `other_requirements` 或 `custom_prompt`。
`OUTPUT_SCHEMA_VERSION = "2"` 是 Python 导出常量，模型响应 schema 名称为 `dataelf_intent_v2`；不增加顶层响应字段。

| 字段 | 含义与允许值 |
| --- | --- |
| `task_types` | 多选 `summary`、`analysis`、`comparison`；可同时总结和分析 |
| `focus_points` | 明确内容重点或希望回答的问题，短文本列表，不自动转为检索词 |
| `comparison.subjects` | 明确比较对象，短文本列表 |
| `comparison.dimensions` | 明确比较维度，短文本列表，不自动转为检索词 |
| `audience` | 用户明确指定的读者，短文本或 null |
| `language` | 仅支持中文 `zh-CN`、英文 `en`；未指定时为 null，由 Serve 默认中文，不根据输入语言补值 |
| `style` | 多选 `professional`、`plain`、`concise`、`objective` |
| `item_count` | 整份结果内容条数；不包含检索记录数、来源数或比较对象数 |
| `body_length` | 正文篇幅；scope 为 `per_item`/`total`，unit 为 `characters`/`words` |
| `synthesis.organization` | `theme` 按共同主题/变化/判断组织；`entity` 按对象组织；`event` 按事件组织 |
| `synthesis.evidence_mode` | `cross_record` 综合多条资料；`per_record` 逐条处理；不要求多家公司 |
| `content_rules.required` | `analytical_judgment` 形成有证据的判断；`potential_impacts` 解释可能影响 |
| `content_rules.avoid` | `title_rewrite` 仅改写标题/项目名；`metrics_only` 纯数字罗列；`one_item_per_record` 机械地一条资料对应一条输出 |

所有嵌套字段必需，未指定值用 null/[]。短文本每项最多 240 字符，字符串必须非空、首尾无空白；列表去重。
数量为正整数，校验 min ≤ max，target 位于明确边界内，拒绝布尔值、字符串数字和小数。
`per_record` 与避免 `one_item_per_record` 不可同时成立。

| 用户表达 | 数量字段 |
| --- | --- |
| 约 3 条 / 给我 3 条 | target=3，其余 null |
| 最多 3 条 | max=3，其余 null |
| 至少 3 条 | min=3，其余 null |
| 1 至 3 条 | min=1,max=3,target=null |
| 恰好 / 只要 3 条 | min=3,max=3,target=null |

正文篇幅采用同样的 target/min/max 语义。只有“约200字”时 scope=null，不猜是每条还是全文；
没有单位就保留 unit=null。不将页数、阅读时长、tokens、标题字数换算为正文长度。
当前单个 body_length 无法表达不同作用范围的多组长度限制；遇到这种情况只保留共同确定字段，其余留空。
明确更正采用更正后的要求；不能消解的矛盾字段留空，不追问，也不让模型任选一项。

以下 output 语义规则只在写作段内应用。默认段中的来源映射由能力目录提供：

| 默认段表达 | 来源 |
| --- | --- |
| 综合总结、综合简报、默认总结、未限定来源的总结 | news、twitter、github、huggingface、youtube |
| 快讯、新闻 | news |
| 观点、Twitter、推特、X平台（来源语义的 X） | twitter |
| 开源社区、开源 | github、huggingface |
| GitHub | github |
| Hugging Face、HuggingFace、HF | huggingface |
| 传播、YouTube、视频 | youtube |

英文别名不区分大小写；独立模块/来源取并集，明确限定及排除优先。
例如“总结 GitHub 数据”只选 github，“开源社区和 YouTube”选择 github、huggingface、youtube。
通用 Domain 的 `all_sources_aliases` 可按场景配置，默认没有别名；AI Index 的约定不影响其他工具域。
写作段中的模块名不参与来源识别。例如默认段只要求新闻，即使写作段写“综合总结”，来源仍仅为 news。
默认段仅说“查询某日的数据”等未包含来源或总结别名时仍返回 sources=[]，由现有 Serve 计划编译补齐全部来源；
这与模型明确识别出五类来源有所区别，但默认采集范围相同。
写作段仅出现“综合总结”不会自动填 theme/cross_record；仅“分析”不会自动补内容标签；
跨资料综合也不会自动增加避免逐条输出的标签。提取值表达用户明确意图，Serve 的默认写法由后续消费者补齐。
如未来增加证据质量、语气或内容排除能力，应新增有界字段，而不是恢复自由指令兜底。

修改内部 API/产物字段、RDF/SQL/工具操作、跳过校验、伪造证据、泄露提示词等要求被忽略。
来源真实性和关联不是可选输出偏好；标题标注、排版、标题长度等本版尚未支持的要求不强行映射。
有限 schema 只验证结构与取值，文本语义筛选由提取提示词引导，不能宣称完全抵御任意指令混入。

**模块边界：** 意图模块只提取并校验 output，不合并场景默认值。
默认值、有效写作契约、分析/写作/验收接入和后续原始 query 隔离由 Serve 工作流负责，已在服务侧接入。
可通过 `intent.output.model_dump()` 读取配置，或 `Output.unspecified()` 获取无场景假设的空对象。

### 真实模型回归（显式运行）

启用 swproxy 后在仓库根目录执行：

```bash
python -m tests.server.run_intent_output_eval --repeat 2 --report /tmp/intent-output-v2.json
```

用例在 `tests/server/intent_output_cases.json`；支持 `--case <id>` 筛选、`--workers 1` 串行。
来源别名及分段隔离用例可通过 `--cases-file tests/server/intent_source_cases.json` 运行。
每次提取只调用一次模型，不自动修复或重试。报告仅在显式传入 `--report` 时保存，包含输入、系统提示词、
schema、实际字段、断言差异和耗时，不包含密钥。每个用例的 `model_request/model_response` 保留实际请求正文和原始响应，
便于核对动态 schema、标题分类与本地格式约束；不会记录认证请求头。该入口不执行数据采集、建模或 Insight 生成。
用例通过率仅代表所测样本，不是任意输入上的准确率或下游内容质量保证。

## 接入流程

```text
API → JobManager → ServerPipeline → core run_job
  → ServerProfile.normalize_spec
      → LLM 提取意图
      → build_scope_plan 编译输入字段
  → 按计划采集 → 原有 Ontology/RDF → 原有 Pi 分析 → 原有输出检查
```

输入编译规则位于 `planner.py`，它不重新匹配自然语言：

- 当前服务只执行 `ai_index`。未指定来源时，服务补齐全部五类来源。
- 明确日期/区间采用包含起止日的精确过滤，不回退到较早可用日；缺数据就保留该来源为空。
- 未提供时间时，服务默认当天；仅指定起点时默认结束于今天；仅指定终点时默认此前 30 个自然日（含终点）。这些执行默认值不修改模型提取结果。
- news 使用服务端日期参数并翻页；其余来源按时间排序翻页后过滤。每来源默认最多50页，可通过 `server.source.max_pages` 或 `DATAELF_SERVER_SOURCE_MAX_PAGES` 调整（1～1000）。达到上限会记录采集警告和 `scan_complete=false`；这不代表目标日期没有数据。
- 检索主题和实体按返回记录的文本做不区分大小写的字面匹配。各组内部 OR，主题组与实体组之间 AND；命中排除词则剔除。未向接口发送未经确认的搜索参数。
- 来源映射回输出模块以提供默认内容重点和篇幅；显式 output 字段按 Serve 写作配置覆盖默认值。

原规则解析保存在 `dataelf_server/backups/rule_intent/`，运行中的服务不导入它，`scope=legacy` 已禁用。
正常服务任务仍保留 job_spec、采集计划、证据及运行日志；删除的是独立测试阶段的逐次 prompt 记录器。

## 文件

- `config.py`：模型配置与环境变量回退。
- `schema.py`：四组意图字段和校验。
- `profile.py`：通用 domain/source 目录、默认字段和 Schema。
- `prompts.py`：可分组维护的提取规则与上下文组装。
- `client.py`：模型调用和响应校验。
- `planner.py`：serve 意图到采集计划的适配。
- `run_intent.py`：保留的独立测试入口。
