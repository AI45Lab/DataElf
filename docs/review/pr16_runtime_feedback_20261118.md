# PR #16：真实审核反馈协议修复

基于 `42521af`，使用独立工作区修复，原 ON 任务继续读取原工作区代码，避免热修改。

## 确认的错误

AI-OFF-1（job_4deef5afe4e4）第二次分析审核确认作者已经修改，提交 3 个历史阻塞项和 2 个非阻塞建议的解决记录。`check_decision` 原先把更新 ID 集合严格等同于历史阻塞 ID 集合，因额外两个已知建议而拒绝合法批准。模型连续五次尝试提交，均未形成正式批准；测试操作员发送 SIGINT 保存现场。这不是预算终止或用户取消研究任务。

修复要求历史阻塞项全部得到唯一且有证据的更新；允许更新已知非阻塞项。重复 ID、未知 ID、缺少历史阻塞项、未解决阻塞项、错误提交绑定仍拒绝。非阻塞建议不会因为被更新而自动升级为阻塞问题。

## 验证

- 7 个协议用例覆盖 3+2 记录、仅更新必须项、缺项/重复/未知/未解决/错误绑定，以及保留未解决但非阻塞建议。
- 协议加分析回归 19 passed；含原实际 Pi 返工闭环。
- 新的实际 Pi 闭环额外包含非阻塞建议及撤回记录，1 passed（67.92 秒）。
- 对真实模型原始提交只读回放：原校验拒绝，修复校验接受 5 条更新；请求 hash 完全一致，源提交清单不变。没有替模型写入批准，没有执行安装，没有调用真实模型。
- 完整修复矩阵 581 passed / 0 failed / 0 skipped（500.18 秒）。仓库外安装包校验通过。真实 OFF 需要在修复版本重测，不将只读回放计为端到端通过。

证据位于工作区上级：`review-validation-before.json`、`review-validation-fixed.json`、`optional-update.log`、`optional-native.log`、`fixed-matrix.log`。

## 同时收尾的边界

移除 shared analysis extension 中不可达的 AI 专属 fallback prompt；实际 system.md 组合路径不变。局部报告反馈及续接旧交付的目录权限改为使用 domain 声明的 writable_dirs。AI 声明值与原白名单一致；合成 domain 验证 results/research 自定义目录。修正恢复的历史测试 fixture：显式绑定 domain、按正式分页协议读取检查记录。独立副本的 30 项补测已通过，包含 15 个通用 ontology 实际 Pi/假 provider 场景。

未修改 Serve、Finance、Trajectory 的业务入口或流程；未修改用户配置或测试 skill。
