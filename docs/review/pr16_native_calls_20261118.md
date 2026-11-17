# PR #16：真实图谱分析原生调用防护修复

基于 `e84e289`。本轮未修改旧运行目录，避免真实任务热更新。

## 原因与真实影响

ON2 `job_3960eb7953f4` 完成图谱两次独立审核和一次作者修订，发布版本 `r-71ce1a07227d4ba39d27db9c22684a48`（2795 quads）。接回分析后，compat 扩展在 message_end 移除了 reasoning 内容；native_calls 仍按最终 content 下标寻找原始流的参数，造成下标偏移。

真实第 89 会话中，三条完整工具调用的原始 contentIndex 是 2/3/4；最终消息中的位置是 1/2/3。首条 analysis_inspect 的完整参数为 resource=contract，却被错判为残缺。原始 stopReason 为 toolUse，非 length，且 SDK 已产生三个 toolcall_end。恢复逻辑把误判当作可恢复传输问题，因此连续重开会话。操作员在第 92 会话发送 SIGINT 停止无效循环；没有放宽校验或改写批准。

此防护及下标假设已存在于本次 PR 修复基线；本次真实任务暴露了与 reasoning 过滤组合的缺口。OFF 路径未加载图谱的 message_end 过滤，因而先前 OFF 测试没有暴露它。

## 修复

在 toolcall_end 时，将原始 JSON 分片绑定到稳定的原生 tool-call ID；message_end 按 ID 校验。不得使用 SDK 宽松解析后的 arguments 替代原始 JSON，不从普通正文提取调用。

缺少完整流绑定、非法 JSON、数组参数，以及 length/error/aborted 仍拒绝整条消息的执行。已验证调用不会因 thinking/text 清理而错配。没有新增总预算或修改服务端模型参数。

## 验证

证据位于工作区上级目录：

- `native-id-unit.log`：9 个原始流防护用例，包括区块删除、三调用、残缺 JSON、截断、错误、取消、未知 ID、缺少结束事件、非对象和空参数（参数化 9 项）。
- `native-id-analysis-fixed.log`：实际 Pi + 假 provider，ontology OFF/ON 分别完成格式修复、审核退回、作者修订及精确批准。响应含独立 reasoning 和普通正文。
- `native-id-parallel.log`：ON 用例增加与真实失败同形态的 contract/status/sources 三条并行调用，完整闭环通过。与前项有重叠，不累计。
- `native-id-real-replay.json`：只读重放真实第 89 会话，三条调用均通过完整性校验；没有执行工具、安装产物或调用模型。
- `installed-native-id-verification.json`：仓库外 wheel 安装包 9 项相同防护验证及 TS 源码 hash 一致。
- `native-id-ontology.log`：共享防护的 15 项实际 Pi 建模回归：14 passed，1 项 malformed_trial_patch 触发本地 SPARQL 10 秒资源超时，原断言与运行限制未放宽；不改代码独立复跑该项通过（native-id-ontology-rerun.log）。不能据此排除共享环境下偶发工具资源超时。

新增测试开发中修正过两处夹具错误：把 commit 结果整体误作 GraphRef，以及遗漏审核角色已有的 graph_query/graph_explore 只读白名单。保留失败日志，未修改运行实现去迎合这些断言。

后续真实续测使用原已批准图谱，通过正式 --resume-graph-manifest 入口在独立任务目录复制并验证图谱及来源。原任务、原错误、源文件和批准记录保留不变；新任务的分析用量与先前建图/失败恢复分开统计。
