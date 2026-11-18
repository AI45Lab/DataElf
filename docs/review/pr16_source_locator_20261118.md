# PR #16：PDF 来源引用误拒绝

真实 ON1 续测 `job_d490bcaaf55f` 中，来源记录包含 `pdf_url=http://arxiv.org/pdf/2611.11995.pdf`。作者提交 `{"url":"http://arxiv.org/pdf/2611.11995.pdf"}` 时，域校验只收集 url、landing_page_url 等字段，遗漏 pdf_url，误判链接无法解析。带说明文字的复合字符串被拒绝仍是正确行为，二者不能混淆。

修复仅在 AI Index 领域：纳入原始 pdf_url；错误指明 external_support 的元素下标，并给出精确 locator 例子；作者中文 prompt 同步说明裸 ID、已登记完整 URL 或 path + JSON Pointer 的形式。没有抓取新证据、从正文猜 URL、将未知链接自动变成有效来源，也没有放宽 hash 或审核要求。

工作区上级目录的 `source-locator-real-replay.json` 使用同一真实来源只读重放：旧代码拒绝，修复接受，来源 hash 不变，零模型调用。`source-locators.log` 的 22 项测试通过，包含 10 项新增有效/无效定位对照。实际 Pi ON/OFF 闭环另见 `source-pi.log`，不与重叠测试数量相加。

正在运行的真实任务保持 06736f9，不热更新；此补丁在独立 worktree 开发。最终验收区分原任务自行修正引用、补丁的原输入重放与新代码真实任务，不能混称。

实际 Pi + 假 provider ON/OFF 两组均通过（113.76 秒）。真实任务仍在原代码运行，18:07 已自行将引用改为可解析形式并完成本地定位检查；未向其注入测试人员意见。补丁的旧输入回放单列，不宣称真实任务在新代码上完成。
