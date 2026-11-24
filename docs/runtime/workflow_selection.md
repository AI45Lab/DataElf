# 执行场景、宿主策略与 domain 业务入口

本说明对应 PR #16 的入口隔离实现。Domain 负责业务能力和数据入口，宿主负责运行机制。接入普通 domain 不需要了解 legacy、新旧 Pi 执行器或 core 的文件布局。

## 三个不同维度

- `workflow_profile` 是运行场景/宿主边界：`research` 使用 CLI 研究组件；`server` 使用服务侧显式提供的 plugin、explorer 和资源。
- `workflow_policy` 是宿主选择并保存的执行协议，控制审核交接、恢复、运行能力与时间策略。它不是 domain 的配置，也不是建模策略。
- `modeling_strategy` 是 domain 自己解释的建模策略。建模可关闭；开启或关闭不会自动切换整个任务的执行协议。

Server 不作为一个 policy，是因为服务宿主及其组件装配与执行协议是两个维度。本版 server 只接受 legacy，不意味着这两个字段等义，也不意味着任意 domain 都具备部署为服务的组件。

## 当前合法组合

| profile | policy | 条件与路径 |
| --- | --- | --- |
| research | legacy | 普通 domain plugin；进入兼容 workflow 和 LegacyPiExplorer |
| research | cli_skill_review_v1 | domain 提供 reviewed_analysis 业务入口；宿主启用审核/恢复协议 |
| server | legacy | 仅服务宿主，必须显式注入服务 plugin/explorer；使用 ServerPiProcess |
| server | cli_skill_review_v1 | 拒绝 |
| 任意 | 未注册策略或不支持的组合 | 拒绝，不静默 fallback |

`ai_index_cli_skill_review_v1` 仅是历史任务/配置的兼容别名，与 canonical policy 使用相同范围校验，不按该字符串中的 domain 名分派。不要为新 domain 复制一个只有名称不同的 policy。

## 选择与恢复

新任务显式选择的 policy 优先；research 新任务未显式选择时可采用宿主配置；默认配置为 legacy。Server 不继承 research 默认策略。恢复时保存的选择优先，禁止改变原任务 domain/profile/policy。历史记录缺少 policy 时补为 legacy，而不是采用今天的配置默认值，以免旧任务在另一套协议下继续。

`JobSpec.workflow_policy` 是宿主的运行记录；plugin 不应改写它或通过 legacy 三元组选择执行器。Finance 和 Trajectory 无需包装 entry 或 legacy_entry 声明，直接使用兼容 fallback。

## 新 domain 怎么接入

旧 manifest 若包含 `legacy_entry`，加载时会提示迁移。普通 domain 可直接删除该字段；如有自定义执行行为，应先迁移该行为。

普通接入只需 plugin、数据源、能力、资源、产物和领域校验，沿用现有 DomainPlugin 协议。无需 register_policy，也无需声明 core 路径。

确有独立业务编排时，可以提供命名业务入口。例如：

```yaml
workflows:
  reviewed_analysis:
    entry: dataelf.domains.example.workflow:execute
```

该入口实现业务编排，调用可复用组件；它没有 profiles、aliases、unbounded 或 recovery 参数。宿主的 cli_skill_review_v1 将执行协议绑定到 reviewed_analysis。构造 JobSpec 时只校验宿主的策略范围，无需预加载 manifest。任务初始化时，通过本次 registry 加载的 plugin（或显式注入的 plugin）的 manifest 解析业务入口；缺少入口会在数据准备和建模之前报错。声明能力不自动开启新协议；宿主仍需显式选择。当前只有 AI Index 提供该业务入口，Finance/Trajectory 保持旧业务流程。

`DomainWorkflow` 拒绝把运行策略字段混入 manifest。运行协议有实质差异时，宿主开发者才使用 register_policy/register_workflow 注册新的协议；数据源、提示词、模型、建模策略不同不构成新增 policy 的理由。

## 三套 Pi 执行器与移除条件

- discovery/pi_cli_explorer.py：当前审核研究执行器，支持角色交接回执、任务 cwd/资源 cwd 分离和宿主时间策略。仅在替代实现覆盖交接、取消、恢复、安装包和真实 ON/OFF 任务后迁移。
- discovery/legacy/pi_explorer.py：源自 78f2ef4 的 CLI 兼容副本。所有使用方完成迁移、历史任务恢复方案明确、对应 domain 真实验收通过且无剩余工厂/导入后，才删除。
- dataelf_server/runtime/pi_explorer.py：Serve 自有实现，由服务层提供资源，默认不加载 CLI npm 包。只有替代服务覆盖固定模板、HTTP 提交/结果、取消、超时、恢复及实际本地部署验收，且所有调用方完成迁移后，才删除。没有必须合并到 CLI 的要求。

代码相似不等于行为兼容，AI Index 的成功不能替代其他 domain 或 Serve 的迁移验收。模块顶部注释同步保留这些条件。
