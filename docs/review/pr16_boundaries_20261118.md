# PR #16 评论修复与验收记录

> 2026-11-20 后续 review 已调整入口方案：删除 Finance/Trajectory 包装入口；domain 仅声明业务 hook，宿主负责 policy。下文保留 11-18 当时的修改与测试记录，当前规则见 [执行选择说明](../runtime/workflow_selection.md)。

基线：`7be0d50757db9abf261f898434ef71948a479763`。分支：`codex/pr16-review-boundaries`。最终运行代码提交：`9697749`（包含领域边界重整与六项实测修复）。

本轮只重整该 PR 的领域边界和相应兼容接口；不引入后续独立 benchmark 评测、主动上下文管理、Finance 新运行时等分支。
Finance、Trajectory 的 legacy 三元组入口按用户决定保留；serve 保留自有固定模板和执行流程。未修改用户真实配置，未推送，也未向远程评论区发消息。

## 评论逐项映射

| 组 | 原评论对象 | 处理与验证点 |
|---|---|---|
| 1 | workflow 中按 AI policy 过滤 graph_ref；StageResult.graph_ref；DiscoveryContext.graph_ref/graph_root；DiscoveryJob.graph_refs | 图谱结果只通过 StageResult.context、DiscoveryContext.domain_context 和 domain 产物传递。AI modeler 负责规范化引用，领域 flow 将任务索引扩展放在 job.context。core 不解释图谱结构。 |
| 2 | core 中写死 submissions / reviews/analysis | manifest.audit_dirs 声明审计目录，通用 finalizer 按声明标为 log；保留审计文件，不删除证据。 |
| 3 | discovery/flows/ai_index.py、_FLOWS 中硬编码 AI | flow 移入 domains/ai_index/workflow.py；domain.yaml 声明 entry、capabilities、profiles 和历史别名；公共 registry 加载声明。新增合成 domain 的注册测试不修改 core。 |
| 4 | ontology.capabilities、注册函数未使用、OntologyRequest 的 Literal、contracts 反向依赖 ontology | 执行注册归 discovery/policies.py；ontology 仅留兼容导出。新名称 cli_skill_review_v1；旧 AI 名称仅在 AI manifest 声明别名。OntologyRequest 接受已注册字符串并校验 scope。 |
| 5 | workflow_policy 为何必要；可选建模 | modeling_strategy 只选择 modeler，不能标识分析审核、恢复和资源策略。保留显式执行选择，历史任务缺字段仍为 legacy，恢复不得换路；modeler 仍可为 None。后续独立评测不在本轮范围。 |
| 6 | finance / trajectory entry 写死 legacy 三元组 | 按用户要求保留。它们尚未迁移到新审核流程，拒绝误选新路径属于兼容隔离，不是新能力声明方式。后续迁移另开 PR。 |
| 7 | analysis_review 读取 AI 配置、导入 SKILL_ROOT；整个 coordinator 是否专用 | 循环和快照组件保留通用；角色配置按 spec.domain 读取，来源、prompt、技能、报告语义通过 manifest-selected services 提供。AI 三件套字段和报告检查搬到 AI domain。 |
| 8 | Pi explorer 感知 ontology / policy | 分析角色仍能读取、查询和维护图谱。角色启动组件显式准备 extension、环境、stop_files 和工具 allowlist；Pi 进程执行器不导入 ontology、不判断 domain。 |
| 9 | 六个 TS extension 放 core 却写死 AI | 保留公共工具桥接实现，删除 AI domain/policy 字符串门禁；由宿主显式加载并传 reviewed-workflow 标记。来源工具名称来自当前角色真实 schema 清单，不按 ai_index_ 前缀筛选。 |
| 10 | legacy_ai_index.py、pipeline 的 EvidenceCatalog、resume 的 AI 限制、tools 前缀 | 配置迁移归 AI domain。公开 source/prompt 服务按 manifest 注入，无通用模块反向 import domain。resume 检查发布完整性，调用方 AI modeler 检查自身 domain。 |
| 11 | 整份复制 legacy/pi_explorer.py | 保留稳定进程协议，补充复制来源、适用入口、禁止隐式同步及删除条件。未把新审核能力回灌旧执行器。 |
| 12 | stage1/stage2 run/bundle 别名及 legacyBundleMeaning | 新 OntologyRunResult 只暴露一次发布的图谱版本与 bundle/contract 路径，清理对应 stage1/stage2 run/bundle 消费者；新 validator 验证 graph manifest v2。serve 自有旧协议不删。旧配置的单向迁移与既有 stage1Mode 监测标签仍保留，不代表执行旧两阶段。 |
| 13 | pipeline 风格、重复延迟 import | 统一格式，抽出请求/配置准备步骤，将重复纯 helper 导入移到模块级。Pi 导入仍延迟，避免配置层循环依赖；引擎和恢复行为由回归验证。 |
| 14 | development_contract.md 全量核对 | 已逐节阅读。修复本轮涉及的领域依赖、资源、prompt ownership 和结果边界；其余矛盾列于文末，不通过改规范掩盖现状。 |

重复的 graph_ref 和 registry 评论合并在对应行中。各项代码边界与离线回归已验证；真实任务已完成，运行闭环和内容质量分别见下文。

## 本轮额外发现

- CLI 曾直接比较旧 policy 常量；引入规范名称后，两种合法名称必须具有相同建模覆盖行为，已补入口测试。
- ON2 输入中的“同一项目”会被旧字符串匹配识别为“一项”输出上限。新 AI 解析只识别发现/洞察/结论的数量，不把实体数量当报告数量，也不把“至少”解释为最大值。legacy parser 不变。
- 历史本地测试存在旧模块路径、错误计时断言、已退役的服务输出上限和非法工具恢复预期；先保存相同环境的基线/修复对照，再更新测试适配。没有修改 serve / Finance / Trajectory 业务逻辑来迎合测试。

## 测试证据位置

所有测试配置和输出均在仓库外：
`/mnt/shared-storage-user/adpc-share/zhangjiayi1/.dataelf/development/pr16-review-boundaries/`

- `compare-baseline.log`：原 7be0d50，同一离线矩阵，434 passed / 18 failed。
- `compare-worktree.log`：本轮，同一离线矩阵，435 passed / 17 failed；无新增失败。
- `native4.log`：实际 Pi + 本地假 provider，建图和分析四角色、格式修正、语义返工与精确批准，2 passed。
- `final-core.log`：来源、图谱、审核、恢复、架构边界，82 passed。
- `final-cli.log`：包含旧别名的 CLI 入口和边界，19 passed，与前项有重叠。
- `isolation-final.log`：478 passed / 4 failed；4 项是 serve 过期断言。`baseline-native.log` 在原基线复现完全相同的 4 项失败，3 passed。
- `last-native.log`：修正断言后的 serve 原生协议、边界测试、建图和分析实际 Pi 闭环，29 passed。
- `final-matrix.log`：最终统一兼容矩阵，567 passed / 0 failed / 0 skipped；耗时 425 秒。与 `last-native.log` 有重叠，不累加。
- `dist-final/`、`installed-final/`、`wheel-final.log`：最终 wheel 构建安装；仓库外核对四份中文 prompt、来源 skill、TS 扩展、两种策略名称及删除旧模块。
- 最终 wheel 及真实三个 AI Index 任务另记，不累计重复测试数量。

## 真实任务结果

使用 `dataelf-regression` 的 AI-ON-1、AI-ON-2、AI-OFF-1 原始输入，窗口固定为 2026-11-11 至 2026-11-17；通过 `swproxy-model-env` 独立 PTY，GLM-5.3-a3，三组最初并行启动。每半小时监控，发现确定性代码故障后保留现场、修复并定向复测。没有增加总任务时间/token 预算、改题、放宽审核或人工代写模型产物。

| 场景 | 最终正式任务 | 实际运行版本 | 运行与审核结果 | 独立内容复核 |
|---|---|---|---|---|
| OFF 研究关联 | job_f59646052da0 | e84e289 | 3h22m03s；3次作者提交/3次审核，2轮有效返工；正式安装通过 | 127快照文件、58来源、7附件核对；3项窗口/推断问题仍存在 |
| ON2 项目生态 | job_bab0cea2fbd5 | 9ea7b02 | 原图谱2795 quads；正式续接后分析3h11m02s，2轮有效返工；安装通过 | 92快照文件、35来源、6附件核对；3项查询口径/范围外推问题仍存在 |
| ON1 研究关联 | job_d490bcaaf55f | 图谱06f6921，续接分析06736f9 | 图谱3939 quads；续接分析3h57m32s，4次提交/4次审核，3轮有效返工；安装通过 | 142快照文件、55来源、12个附件/正式文件核对；4类证据外推/图谱覆盖问题仍存在 |

ON 续接均使用正式 graph manifest 入口，复制并验证原已批准图谱及来源，建模新增调用为零；不是从空草稿重复建图。ON1 的字段映射、单日/全窗口分母、附件残留措辞和推文 ID 在真实返工中得到修正。最终三组都具有正式交付，但**不能记为内容质量全部通过，也不能宣称三组在同一个最终 HEAD 上完整重跑通过**。最终 PDF 引用补丁9697749只做原失败输入重放、本地实际Pi与安装测试；其余真实产物在最终代码下重新校验。

所有尝试（含停止的失败任务）共 **2041次请求、212,325,840已知token，194次请求用量未知**。累计输入包括重复上下文和缓存，不等于生成字数或费用；单独的环境纠正/连通探针另有原始记录。已落盘终态中未观察到 `length`；中断未落盘的回复不能据此认定没有截断。继承的 Pi 注册未启用服务端专用 thinking 兼容参数，`--thinking off` 并未保证服务端 reasoning 为零。ON1一次建图修订产生约42万输出token，其中约38万为reasoning，是耗时的重要观测因素；本轮没有中途改变模型参数来制造性能对比。

完整尝试成本、四角色/逐会话明细与原始报告索引：
[real-attempts.md](/mnt/shared-storage-user/adpc-share/zhangjiayi1/.dataelf/development/pr16-review-boundaries/real-attempts.md)。

独立内容复核（不改原报告/批准）：

- [OFF复核](/mnt/shared-storage-user/adpc-share/zhangjiayi1/dataelf-acceptance/pr16-ai-index-20261118-032247/independent-audit/review.md)
- [ON2复核](/mnt/shared-storage-user/adpc-share/zhangjiayi1/dataelf-acceptance/pr16-ai-index-20261118-060811/independent-audit/review.md)
- [ON1复核](/mnt/shared-storage-user/adpc-share/zhangjiayi1/dataelf-acceptance/pr16-ai-index-20261118-171154/independent-audit/review.md)

Serve、Trajectory、Finance 本轮仅离线隔离验证，没有启动它们的真实任务；三个业务目录相对7be0d50没有源码改动。未验证持续图谱维护、跨域新架构接入或生产成功率。

## 真实任务暴露的六项修复

| 提交 | 故障 | 修复与证据 |
|---|---|---|
| e84e289 | 已知非阻塞问题的更新被当成非法 issue 集合，反复拒绝审核提交 | 必需阻塞项仍须覆盖，允许已知非阻塞项更新；拒绝未知、重复和未解决阻塞。OFF真实返工通过。见 pr16_runtime_feedback_20261118.md。 |
| 9ea7b02 | reasoning清理改变content下标，完整原生工具调用被误判残缺，ON2进入92个恢复会话 | 按稳定tool-call ID绑定原始分片，仍拒绝不完整JSON与length/error/aborted。ON2图谱续接通过。见 pr16_native_calls_20261118.md。 |
| 06f6921 | 递归oneOf校验遍历不可能的表达式分支，草稿工具长时间CPU阻塞 | 宿主先做等价判别，保留公开Schema，限制错误收集；同一真实patch重放0.252秒。ON1从空草稿发布成功。见 pr16_expression_validation_20261118.md。 |
| 9410fc5 | inline与saved issue重复，12项仅6个ID，问题记录可能被覆盖 | 严格审核拒绝重复ID，提示每项只用一种提交形式；真实最终批准重验通过。见 pr16_review_identity_20261118.md。 |
| 06736f9 | 原生相对路径写到runtime，Bash进入workspace后找不到文件并全盘查找阻塞 | 角色显式传workspace执行目录，runtime继续供配置/依赖使用；旧入口默认行为不变。ON1续接8个角色会话目录一致。见 pr16_execution_cwd_20261118.md。 |
| 9697749 | 原始paper pdf_url未进入登记引用集，真实PDF链接误拒绝 | AI领域补齐pdf_url，定位错误带元素下标，中文prompt给出精确引用示例。原失败输入旧拒绝/新接受，来源未变。见 pr16_source_locator_20261118.md。 |

最终目录专项12项（含Pi ON/OFF）、隔离123项通过；PDF定位22项、Pi ON/OFF两组通过；仓库外最终包20项与10文件hash/策略别名通过。这些批次与先前567项兼容矩阵有重叠，不累加为新的去重总数。安装与目录验证包括真实Pi进程，但provider为可控本地服务，不冒充真实模型质量测试。

全仓库测试并非“全绿”：历史 `tests/test_ontology_config.py` 仍导入已经删除的AI stage1/stage2模块，在原基线7be0d50复现相同收集错误。本轮没有恢复废弃运行时，也没有通过跳过后宣称全库通过；验收明确列出已运行的当前功能矩阵。另一次SPARQL专项在共享环境触发原有10秒工具超时，不改代码独立复跑通过，原日志保留。

## development_contract.md 待维护内容

本轮不直接重写规范。建议后续由维护者确认：

1. §4.3 “ontology 是 AI 私有实现”应更新为：可选建模由 domain 发起，RDF 引擎是可复用组件；domain 不必使用它。
2. §7 单一固定阶段序列应补充 manifest 声明执行入口及保持历史任务选择的规则。
3. §8.1 “不把 Python tools 转为 Pi tool”“AI 不提供正式 custom tools”与现有正式工具桥接冲突。当前保留已验收桥接，不在本次边界修复中重写工具通信协议。
4. “至少两个真实 domain 后才抽象”与本轮用户指定“通用审核组件、暂时仅服务 AI”存在范围差异。本轮只有 AI 实接加合成 domain 注册验证，不宣称第二个真实 domain 已通过。
5. 配置中已有根级 ontology 开关、workflow 选择；应明确可选能力配置和 domain 配置适配的职责，避免读者把它们当必须阶段。
6. 分离旧 Pi 执行器是用户要求的临时稳定性边界，不应成为未来每个 domain 复制执行器的常规方式。

后续新增 feature 应拆分 PR；本轮只处理该历史 PR 的 review，不合入后续 feature 分支。
