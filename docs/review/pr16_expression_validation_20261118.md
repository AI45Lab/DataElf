# PR #16：深层表达式草稿校验复杂度修复

基于 `9ea7b02`，独立工作区修改；运行中的 ON2 续测保持原代码。

## 真实故障

ON1 `job_cdc47f3ffbd4` 建图修订时，`ontology_draft(action=patch)` 的 Python 子进程持续占用 CPU 约一小时。通过与 Python 3.13.5 匹配的官方 gdb 辅助脚本只读采样，确认栈在 `draft.edit_document` 第 60 行和 jsonschema 的 oneOf/anyOf/properties/items 递归中。并非模型上下文超限，也不是这一时段一直在等待模型推理。

原表达式 Schema 用 oneOf 枚举各操作。jsonschema 在已经发现某个操作 const 不匹配后，仍会收集该分支全部错误并递归校验 args；深层表达式因此在许多不可能匹配的分支中被反复遍历。原始失败请求只有一个 patch 操作，补丁对象最大 op 嵌套深度为 15（包含 patch 的 replace）。

操作员于 08:45 发送 SIGINT 保存现场，工具子进程已退出。原始草稿、来源、调用和执行栈保留；该任务未发布图谱，不能以草稿代替验收通过。

## 修复边界

- 对宿主草稿校验派生等价 Schema：每个表达式分支先检查必要的类型、必需键、允许键及 op 常量，只有可能匹配时才递归进入原分支。
- guard 是原分支已经要求的必要条件，保留原 oneOf 和完整分支约束，没有新增操作、默认值、自动修复或业务语义。
- 不改发给模型的 Schema，避免引入新的 provider 兼容性风险。
- 最多收集 20 个顶层错误供反馈；存在任何错误仍拒绝整个补丁。原草稿编辑保持原子性。
- 未增加模型时间/token预算，未修改 Serve、Finance、Trajectory 的业务流程。

## 验证与证据

工作区上级的 `on1-draft-python-stack.txt` 是真实执行栈；`on1-pending-patch.json` 是真实最后一条调用；`on1-patch-replay.json` 是原 base_hash 完全匹配的只读回放。

同一份真实草稿与补丁在修复后 **0.252 秒**校验通过。回放没有保存修改或发布图谱，前后原始草稿 hash 不变。新草稿 hash 为 `7b2f3f2a265d301ac6c916bd59769a911b8d4a0bfe222d5667d9cfa9d25c7ab9`。

`expression-unit.log`：97 项通过（1.92 秒），覆盖公开/宿主表达式接受集合一致、原 Schema 不被修改、深层表达式 union 访问次数随表达式节点线性增长，以及非法补丁仍被拒绝、输入不变。实际 Pi 深层草稿与残缺补丁回归、兼容回归及仓库外安装结果分别记录于 `expression-native.log`、`expression-regression.log` 和安装验证文件。

不能把 0.252 秒解读为端到端任务必然加速相同比例；模型生成、其他工具和审核仍需独立计时。本轮初期只凭模型请求记录的间隔无法区分模型输出与工具执行，真实栈补充证据后才确认这处本地瓶颈。

补充验收：实际 Pi 深层草稿与残缺补丁选择集 3 项通过（其余 162 项未选择，不计通过）；仓库外安装包重复 97 项表达式检查通过，两份修改模块 hash 与源码一致。

首次扩大回归在收集 `tests/test_ontology_config.py` 时失败：该文件依赖已经移除的 AI Index stage1/stage2 模块。在原 PR 基线 `7be0d50` 的只读副本中复现了同一 ModuleNotFoundError（`expression-baseline-stale-test.log`）；本次不为其恢复废弃运行时。后续明确选择当前通用 ontology、边界和旧入口隔离测试，单独记录结果。

最终选择集 `expression-regression-current.log`：256 passed，覆盖当前通用 ontology、表达式等价性、PR 边界、Finance、Trajectory 和 Serve 固定模板/运行入口。本轮未运行这些旧入口的真实任务。仓库外最终 wheel 表达式测试 97 passed（与上述包含重叠，不累加）。
