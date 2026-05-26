# DataElf Skill-Based 重构背景、需求与实施计划

## 1. 背景

DataElf 当前的工具体系以 Python `BaseTool` 子类为核心。主 agent 根据用户请求生成 Pipeline DSL，runtime 再通过 `run_tool()` 调用具体工具。

现在架构方向调整为：用户可见、可贡献、可沉淀的能力单元统一改成兼容 [AgentSkills](https://agentskills.io/specification) 的 `skill`。也就是说，DataElf 对外不再强调“写一个 BaseTool 工具”，而是强调“贡献一个 skill package”。

但 `BaseTool` 不需要立刻从内部消失。现有工具可以先作为内置 skill 的 runtime backend 继续存在，尤其是 `security_audit` 这类还在持续开发的工具，避免影响实习生正在做的漏斗策略等逻辑。

## 2. 需求原则

### 2.1 用户侧能力单元统一为 Skill

用户看到和贡献的是 skill：

- `SKILL.md`
- `references/`
- `scripts/`
- `assets/`

现有工具也要包装成 skill，例如：

- `security_audit`
- `data_scoring`
- `data_select`
- `enzyme_acquire`
- `protein_analyzer`
- `skillrl_skill_extraction`

### 2.2 不强制 DataElf 专用 runtime metadata

不要求所有 skill frontmatter 写类似：

```yaml
metadata:
  dataelf:
    runtime:
      type: python_function
      entrypoint: scripts/analyze.py:run
```

原因是这会把通用 AgentSkill 强行绑定到 DataElf 的函数式工具接口上，不够自然，也不适合复杂 skill 内部根据场景选择不同执行路径。

### 2.3 主 Agent 负责选择和编排，Skill 负责领域执行

目标分层：

```text
用户自然语言任务
  ↓
DataElf 主 agent
  - 意图识别
  - skill 检索 / 选择
  - 通用 clarification
  - 任务规划
  - 数据边界决定
  ↓
Execution Plan / Trace Plan
  - load dataset
  - invoke skill
  - save result
  - record artifacts
  ↓
Skill Runtime
  - 读取 SKILL.md
  - 需要时读取 references/
  - 需要时运行 scripts/
  - 执行领域逻辑
  - 返回 result / artifacts / metrics
```

DataElf 负责：

- `load_dataset`
- `save_result`
- artifact registry
- trace
- approval / policy / resource budget
- skill selection / orchestration

Skill 负责：

- 对输入数据执行领域逻辑
- 使用自己的 scripts / references / assets
- 必要时调用模型、算法、工具
- 返回结构化结果、artifacts、metrics

### 2.4 Clarification 分两层

第一层是通用 clarification，放在主 agent / DataElf orchestrator。它负责所有任务都可能遇到的通用缺失信息，例如：

- 用户没有说明要处理哪个 dataset。
- 用户请求里的目标、范围、输出位置、预算等信息不完整。
- 用户表达存在歧义，需要先确认任务意图。
- 配置、权限、资源策略要求必须向用户确认。

第二层是 skill-specific clarification。它来自具体 skill 的 `SKILL.md`、planner view 或 references，负责某个领域能力特有的缺失信息，例如某个 skill 需要的策略、算法、checker、模型、阈值、运行模式等。

执行交互上，仍然由主 agent 统一向用户提问，而不是每个 skill 自己直接和用户对话。skill 只提供领域 hints 和默认建议，主 agent 决定是否需要问、怎么问、以及如何把用户回答转成后续 execution plan 的参数。

下面只是 `security_audit` skill 的一个例子，不代表所有 skill 都必须这样实现：

```text
主 agent 检索到 security_audit skill
  ↓
读取 skill clarification hints
  ↓
主 agent 向用户询问 checker / scope / default policy
```

其他 skill 可以有完全不同的 skill-specific clarification。例如 `data_select` 可能需要询问 selection budget，`protein_analyzer` 可能需要询问是否启用网络型 enrichment，`skillrl_skill_extraction` 可能需要询问 trajectory 来源。

### 2.5 Trace 分三层

DataElf 必须继续保持可复现、可审计、可优化。

Trace 分为：

- Level 1：DataElf Plan Trace
- Level 2：Skill Runtime Trace
- Level 3：Skill Internal Trace

## 3. 分阶段实施计划

### Step 0：基线确认

目标：确认当前主流程和测试状态，避免后续不知道是哪一步改坏。

工作内容：

- 确认当前 `elf run`、`pilot`、`submit` 的关键路径。
- 确认当前 `ToolRegistry`、`RuntimeExecutor`、`agent/prompt_builder.py`、`cli/common.py` 的调用关系。
- 记录当前内置工具列表。
- 明确：不修改 `SecurityAuditTool.run()` 签名，不影响实习生正在做的 security audit 内部逻辑。

验证：

- 跑现有核心测试。
- 手动跑一个轻量 `elf run`。
- 记录当前 job/log/artifact 输出形态。

### Step 1：把现有工具包装成 AgentSkills 目录

目标：先引入 skill 资产层，不改变现有执行路径。

工作内容：

新增顶层 `skills/` 目录：

```text
skills/
  security_audit/
    SKILL.md
  data_scoring/
    SKILL.md
  data_select/
    SKILL.md
  enzyme_acquire/
    SKILL.md
  protein_analyzer/
    SKILL.md
  skillrl_skill_extraction/
    SKILL.md
```

`SKILL.md` 内容从现有 `docs/tools/*_en.md` 和 tool schema 中整理。

每个 skill 至少包含：

- name
- description
- usage instructions
- input expectations
- output expectations
- clarification hints
- examples
- optional allowed-tools

内置工具可以在 DataElf 代码里有私有 binding：

```text
skill_name -> existing BaseTool backend
```

但这个 binding 不作为用户必须遵守的 skill 规范。

验证：

- 所有 `SKILL.md` 可被解析。
- 现有主流程不变。
- 不改 security audit 领域逻辑。

### Step 2：新增 SkillRegistry 和 Progressive Disclosure

目标：主 agent 后续从 SkillRegistry 获取能力信息，而不是直接读 BaseTool schema。

工作内容：

新增 `SkillRegistry`：

- 扫描 `skills/*/SKILL.md`
- 解析 AgentSkills frontmatter
- 生成轻量 planner view
- 支持按需加载完整 `SKILL.md`
- 支持按需读取 references / scripts / assets manifest

planner 默认只看到：

- skill name
- description
- short usage summary
- allowed tools
- clarification hint summary

不把完整 `SKILL.md` 和 references 全量拼进 prompt。

验证：

- 单测覆盖 skill discovery。
- 单测确认 planner view 不包含完整 `SKILL.md` 正文。
- 单测确认可以按需加载完整 skill instructions。
- 现有 `elf run` 仍能跑通。

### Step 3：主 Agent Prompt 从 Tools 切到 Skills

目标：用户可见能力切换为 skill。

工作内容：

- Prompt 中的 “Available Tools” 改成 “Available Skills”。
- 数据来源改为 `SkillRegistry.list_planner_views()`。
- prompt 明确主 agent 应先选择 skill，再规划执行。
- skill-specific clarification hints 来自 selected skill。
- 这一阶段可以临时保持旧 Pipeline DSL 执行，但语义上开始迁移到 `invoke_skill`。

验证：

- prompt 中出现 skills，而不是 BaseTool 作为用户侧概念。
- 不拼完整 skill 文档。
- `security_audit` clarification 仍能正常工作。
- 现有 security audit 执行结果不变。

### Step 4：引入结构化 Execution Plan，替代 Python Pipeline DSL

目标：把模型输出从 Python DSL 改成结构化 plan，更适合 skill-native orchestration。

目标 plan 示例：

```json
{
  "version": "dataelf_execution_plan_v1",
  "steps": [
    {
      "id": "load_data",
      "op": "load_dataset",
      "dataset": "security_audit_samples",
      "output": "data"
    },
    {
      "id": "run_audit",
      "op": "invoke_skill",
      "skill": "security_audit",
      "input": {
        "data": "$data",
        "checker_names": ["BiasLLMJudge"]
      },
      "output": "audit_result"
    },
    {
      "id": "save",
      "op": "save_result",
      "input": "$audit_result"
    }
  ]
}
```

工作内容：

- 新增 `ExecutionPlan` schema / validator。
- Runtime 支持受控 op：
  - `load_dataset`
  - `invoke_skill`
  - `save_result`
  - `write_file`
  - `write_db`
  - `log`
- `invoke_skill` 调用 `SkillRuntime`。
- job 文件记录 structured plan，便于复现和审计。

验证：

- plan validation 单测。
- 非法 op / 非法变量引用 / 非法 skill name 单测。
- security audit 手动跑通。
- job 产物中能看到 structured plan trace。

### Step 5：实现 SkillRuntime

目标：让 `invoke_skill` 成为统一执行入口。

工作内容：

新增 `SkillRuntime` 抽象。

内置 skill 使用 `BuiltInSkillRuntime`：

- 内部调用现有 `BaseTool.run(context, **kwargs)`。
- 不改 `security_audit`、`data_scoring` 等工具核心逻辑。
- `BaseTool` 只作为 internal backend。

外部 AgentSkill 使用 `AgentSkillRuntime`：

- 加载 selected skill 的 `SKILL.md`。
- 根据需要读取 references / scripts / assets。
- 在 policy 允许下运行脚本、读取文件、调用模型或算法。
- 不要求固定 entrypoint。
- 如果 skill 缺少可执行说明或必要资源，返回明确 validation error。

Skill 返回统一 envelope：

```json
{
  "result": {},
  "metadata": {},
  "artifacts": {},
  "metrics": {},
  "trace": {}
}
```

验证：

- 内置 `security_audit` 结果与旧路径一致。
- 新增 fixture external skill，可被发现、选择、执行、保存结果。
- 不修改 `SecurityAuditTool.run()` 签名。

### Step 6：补齐三层 Trace

目标：保证 skill-native 后仍然可复现、可审计、可优化。

工作内容：

Level 1：DataElf Plan Trace

- selected skills
- clarification transcript
- execution plan
- dataset refs
- result refs
- policy decisions

Level 2：Skill Runtime Trace

- loaded skill files
- loaded references
- allowed tools
- invoked scripts
- runtime inputs / outputs

Level 3：Skill Internal Trace

- skill 内部 LLM calls
- script stdout / stderr
- artifacts
- metrics
- errors

复用已有 LLM trace collector，但区分：

- `scope=core`
- `scope=skill`
- `skill_name`
- `skill_component`

验证：

- 每次 job 至少有 Level 1 trace。
- 调用内置 skill 有 Level 2 trace。
- LLM-based checker 有 Level 3 LLM trace。
- core LLM call 和 skill LLM call 不混淆。

### Step 7：配置迁移与用户扩展入口

目标：让用户真正以 skill 形式贡献能力。

工作内容：

- 用户侧配置从 `tools:` 迁移为 `skills:`。
- 支持配置 skill 搜索路径：
  - 内置 `skills/`
  - 用户自定义 skill dirs
- 新增命令：
  - `elf skills list`
  - `elf skills inspect <name>`
  - `elf skills validate <path>`

外部 skill 接入规则：

- 必须符合 AgentSkills package 结构。
- 必须有 `SKILL.md`。
- 推荐声明 `allowed-tools`。
- 推荐提供 scripts 或足够明确的执行说明。
- 不要求 DataElf 专用 runtime metadata。

验证：

- 内置 skills 能被列出。
- 用户 fixture skill 能通过 validate。
- 配置只启用部分 skills 时，planner 只能看到被启用的 skills。

### Step 8：清理旧 Tool-Facing 架构

目标：最终彻底切到 skill-facing，而不是长期双轨。

工作内容：

- 从主 agent prompt 中移除 Tool DSL / Tool Specification。
- 从用户文档中移除“贡献 BaseTool 工具”的主路径。
- `BaseTool` 保留为 internal backend compatibility layer。
- 旧 `run_tool()` 不再作为用户侧 DSL 能力。
- 最终入口是 `invoke_skill`。
- 旧 `docs/tool_development_guide.md` 改为 internal backend 说明或归档。

验证：

- 新文档只引导用户贡献 AgentSkills。
- 主流程不依赖 planner 可见的 BaseTool schema。
- 核心命令仍可运行：
  - `elf run`
  - `elf pilot`
  - `elf submit`
- security audit 相关测试通过。

## 4. 测试计划

每一步之后至少跑：

- Skill registry / parsing 单测
- Execution plan validation 单测
- Runtime invoke skill 单测
- Security audit smoke test
- Config filtering 测试
- LLM trace scope 测试

关键手动验证命令：

```bash
elf run "run security_audit on security_audit_samples with a custom checker set" -p test-security --wait --verbose
```

预期：

- planner 选择 `security_audit` skill。
- clarification 能读取 security audit 的 skill-specific hints。
- execution plan 包含 `load_dataset -> invoke_skill -> save_result`。
- security audit 仍走现有内部执行逻辑。
- job logs、artifacts、trace 都能复现本次执行。

## 5. 冲突规避

- 不直接修改 `tools/security_audit/tool.py` 的领域逻辑。
- 不改 `SecurityAuditTool.run(self, context, **kwargs)` 签名。
- 不移动实习生可能正在改的 funnel strategy 代码。
- skill migration 优先新增 `skills/security_audit/` 和 registry/runtime bridge。
- 如果必须碰 security audit，只改最外层 adapter / binding，不改 checker、executor、strategy 逻辑。

## 6. 最终验收标准

完成后，DataElf 应满足：

- 用户贡献能力时，主路径是提交 AgentSkills-compatible skill package。
- 主 agent 通过 SkillRegistry 发现、选择、读取 skill。
- 执行层通过 structured Execution Plan 编排：
  - `load_dataset`
  - `invoke_skill`
  - `save_result`
- 内置旧工具已经包装为 skills。
- `BaseTool` 不再是用户侧概念，只是内置 skill 的 runtime backend。
- 外部 skill 不被强制要求写 DataElf 专用 `metadata.dataelf.runtime.entrypoint`。
- Trace 覆盖 plan、skill runtime、skill internal 三层。
- `run`、`pilot`、`submit` 主流程仍然可用。
