# PR #16 评论修复与验收记录

基线：`7be0d50757db9abf261f898434ef71948a479763`。分支：`codex/pr16-review-boundaries`。运行代码提交：`43f080b`。

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
| 12 | stage1/stage2 run/bundle 别名及 legacyBundleMeaning | 新 OntologyRunResult 只暴露一次发布的图谱版本与 bundle/contract 路径，清理所有对应消费者；新 validator 验证 graph manifest v2。serve 自有旧协议不删。 |
| 13 | pipeline 风格、重复延迟 import | 统一格式，抽出请求/配置准备步骤，将重复纯 helper 导入移到模块级。Pi 导入仍延迟，避免配置层循环依赖；引擎和恢复行为由回归验证。 |
| 14 | development_contract.md 全量核对 | 已逐节阅读。修复本轮涉及的领域依赖、资源、prompt ownership 和结果边界；其余矛盾列于文末，不通过改规范掩盖现状。 |

重复的 graph_ref 和 registry 评论合并在对应行中。以上说明尚不代表真实模型验收完成。

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

## 真实任务计划

使用 dataelf-regression 的 AI-ON-1、AI-ON-2、AI-OFF-1 原始任务输入，日期为启动日前七个完整自然日。通过 swproxy 独立 PTY 并行启动，三个独立工作区，不修改用户配置、不增加总预算。每 30 分钟或任务结束时刷新状态和四角色报告。发现代码缺陷后保留失败证据，修复并定向重测；模型或来源不足不通过放宽审核伪装成功。

Serve、Trajectory、Finance 本轮仅离线隔离验证，不扩展真实测试范围。

## development_contract.md 待维护内容

本轮不直接重写规范。建议后续由维护者确认：

1. §4.3 “ontology 是 AI 私有实现”应更新为：可选建模由 domain 发起，RDF 引擎是可复用组件；domain 不必使用它。
2. §7 单一固定阶段序列应补充 manifest 声明执行入口及保持历史任务选择的规则。
3. §8.1 “不把 Python tools 转为 Pi tool”“AI 不提供正式 custom tools”与现有正式工具桥接冲突。当前保留已验收桥接，不在本次边界修复中重写工具通信协议。
4. “至少两个真实 domain 后才抽象”与本轮用户指定“通用审核组件、暂时仅服务 AI”存在范围差异。本轮只有 AI 实接加合成 domain 注册验证，不宣称第二个真实 domain 已通过。
5. 配置中已有根级 ontology 开关、workflow 选择；应明确可选能力配置和 domain 配置适配的职责，避免读者把它们当必须阶段。
6. 分离旧 Pi 执行器是用户要求的临时稳定性边界，不应成为未来每个 domain 复制执行器的常规方式。

后续新增 feature 应拆分 PR；本轮只处理该历史 PR 的 review，不合入后续 feature 分支。
