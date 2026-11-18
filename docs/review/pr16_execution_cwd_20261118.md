# PR #16：分析工具工作目录修复

## 真实故障

在 `06f6921` 的 AI-ON-1 任务 `job_b6c937979bed` 中，图谱已经通过审核并发布。分析角色使用相对路径的原生 `write` 保存 `analysis/evidence_ledger.md`、`analysis/technical_comparison.md`；Pi 实际进程目录却是配置的 runtime 目录。作者随后通过 `bash` 显式进入任务 workspace 检查，找不到这些文件，发起全盘 `find /`，持续约 32 分钟未返回。

测试使用独立 runtime，文件没有污染用户主工作区。17:02 操作员停止该任务，保留两处目录与原日志；没有复制文件冒充正式提交，也没有修改历史批准。该次停止由确定性代码问题触发，不是新增总时间或 token 预算。

## 修复边界

审核角色启动器通过 `DiscoveryContext.execution.cwd` 显式传入任务 workspace。通用 Pi 执行器使用此目录启动进程，原生 write/read/edit/bash 的相对路径因此一致。配置的 `self.cwd` 继续用于 Pi 依赖发现、provider 注册和缓存；命令诊断同时记录 `cwd` 和 `runtime_cwd`。

未传入此选项的调用保持历史行为；legacy Pi 副本、Serve、Finance 和 Trajectory 未改。目录选项必须是 workspace 内已存在的目录，拒绝空值、错误类型、缺失目录、文件和 symlink 逃逸。此检查只保证启动目录，不宣称将任意 Bash 变成文件系统沙箱。

## 验证

工作区上级目录保存测试证据：

- `cwd-tests.log`：启动目录选择、runtime 配置保持、非法路径提前拒绝；实际 Pi + 假 provider 的 ontology OFF/ON 闭环执行相对路径 write/read/edit/bash，随后格式修复、审核退回、作者修订、重新批准。检查 workspace 文件内容、无 runtime 同名目录和每个角色的启动日志。
- `cwd-isolation.log`：领域依赖、Finance、Trajectory、Serve 固定模板和分析快照隔离回归。
- `wheel-cwd.log`、`installed-cwd/`：仓库外构建安装验证。

真实续测只通过正式 graph manifest 恢复入口复用已批准图谱与绑定来源，不重新建图；新分析的成本单列，原失败任务保留。

本次结果：专项 12 passed（含两组实际 Pi 闭环），兼容矩阵 123 passed；安装包目录专项 10 passed，与前项重叠不累加。仓库外导入路径和四个关键文件 hash 与源码一致。没有调用真实模型完成这些自测。
