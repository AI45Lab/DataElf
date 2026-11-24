# AI Index 来源资源

运行时来源 Skill 位于 `skills/ai-index-search/`，包含 `SKILL.md`、查询脚本、API 参考和 agent 元数据。`evidence.SKILL_ROOT` 及角色提示均引用该目录，安装包收录这些资源；运行时不依赖全局 Skill 或开发归档。

来源登记、可配置基础地址、超时、HTTP 状态分类和请求体修复由 domain 内部适配负责。其他 domain 的资源仍由各自目录管理，公共层只加载当前选中的资源。
