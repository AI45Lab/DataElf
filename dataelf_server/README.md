# DataElf Server

DataElf Server 是 DataElf 的可选异步 Insight 服务，接收自然语言查询，返回带来源链接的洞察。服务使用当前 DataElf 配置与研究核心，CLI 继续使用原有 research 工作方式。

文档延续原 DataElfAPI 的组织方式：部署人员看部署说明，调用方看 API_USAGE。

- [部署说明](deployment/README.md)：运行要求、安装、配置、模型、启动、停止、验收及任务记录。
- [API 外部调用说明](API_USAGE.md)：自然语言写法、提交任务、查询进度、获取结果、重试及错误处理。
- [内容与写作配置](deployment/writing.md)：默认值、覆盖规则和校验边界。
- [意图识别模块](intent/README.md)：字段定义与独立解析入口。

## 快速启动

以下命令在 DataElf 仓库根目录执行；先完成依赖和模型、数据源配置：

```bash
bash dataelf_server/deployment/start.sh --config dataelf.local.yaml
```

默认地址为 `http://127.0.0.1:8000`。要指定写作要求，请把它放在 query 的独立 `# 写作要求` 标题之后：

```text
查询2026年8月7日的GitHub与Hugging Face数据。

# 写作要求
面向管理和决策人员，总结并分析。
围绕共同主题综合多条资料，形成1至3条判断，解释可能影响。
不要只改写项目名称或罗列数字，每项保留支撑判断的真实来源。
```

没有写作类标题时使用服务默认写法，默认中文。当前只支持 `scope_v2`；原规则解析和 `legacy` 已停用。详细调用命令见 [API_USAGE.md](API_USAGE.md)。
