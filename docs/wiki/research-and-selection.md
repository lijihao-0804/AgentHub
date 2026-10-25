# LLM Wiki 调研与 AgentHub 选型

调研日期：2026-10-25。代码基线 main `23f1a38`。本报告区分个人 Wiki 的交互范式和企业来源治理。

## 结论

选择自有薄编译层＋现有 PostgreSQL/Celery/模型网关，以准确引文为发布内容，以概念/实体页面和关系为持久组织。
不直接嵌入任何一个项目的全套运行时。它们的模型客户端、存储身份与租户/版本语义不等于 AgentHub 的契约。
阶段目标是可维护、有原始版本证据、可显式发布和可查询的 Wiki；复杂图推理和自由生成综合摘要留给有对照收益的后续工作。

## Wiki 比 RAG 增加什么

[Karpathy 原提案](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
提出由模型在资料进入时维护持久、相互链接的知识，而不是每次问答重新拼装。
值得借鉴的是 Raw/Wiki/Schema 分层及 ingest/query/lint 工作流。它是设计提案，不提供租户隔离、发布事务或实验冻结保证。
AgentHub 将“模型写 Wiki”收窄为“模型提候选、业务逻辑发布”；原始证据保留权威，派生内容保留不确定性。

## 代码研究

Microsoft llmwiki、RAGFlow、LightRAG 的具体源码走查与限制见 [upstream-research.md](upstream-research.md)。

GraphRAG 检查到 main commit
[`c600ff80b77b9b62c47d539a3ac9569e95d8949b`](https://github.com/microsoft/graphrag/commit/c600ff80b77b9b62c47d539a3ac9569e95d8949b)。
[extract_graph.py](https://github.com/microsoft/graphrag/blob/c600ff80b77b9b62c47d539a3ac9569e95d8949b/packages/graphrag/graphrag/index/workflows/extract_graph.py)
把 TextUnit 提取与描述聚合分开，并保留 raw graph 的可选输出。
[增量比较代码](https://github.com/microsoft/graphrag/blob/c600ff80b77b9b62c47d539a3ac9569e95d8949b/packages/graphrag/graphrag/index/update/incremental_index.py)
在所读 `get_delta_docs` 中按 title 识别新增/移除；不能据此认定同名内容修改已经具备 revision 语义。
[community report 更新](https://github.com/microsoft/graphrag/blob/c600ff80b77b9b62c47d539a3ac9569e95d8949b/packages/graphrag/graphrag/index/workflows/update_community_reports.py)
维护 previous/delta/output，适合借鉴不可变输入与增量结果分层。
[官方查询说明](https://microsoft.github.io/graphrag/query/overview/)
区分局部实体、全局社区和 DRIFT。社区综述可支持全局问题，但索引、聚类与多次模型总结的成本需要评测支持。
本轮不复用其 DataFrame 流水线，不复制全套图构建。

## 工程选择

| 选择 | 适用价值 | 本轮取舍 |
| --- | --- | --- |
| Markdown 文件为主库 | 单人、手工整理、Obsidian/Git | 仅作为派生阅读投影，避免多租户文件事务与DB双写 |
| PostgreSQL 结构化派生知识 | 权限、事务、历史版本、来源join、任务恢复 | 采用；JSONB claims/relations受strict schema约束 |
| Wiki 向量索引 | 大量页面的语义召回 | 暂缓；当前标题/词匹配不足则走原始RAG，避免未经评测的重复embedding |
| 图数据库/社区报告 | 深层关系、多跳/全局主题 | 暂缓；先保留关系契约，不为少量邻接增加数据库 |
| 自由生成摘要反复改写 | 阅读流畅与高层综合 | 暂缓；第一版可核验的摘编＋冲突候选更便于来源撤销和验收 |

## 现有代码审计与适配

- `packages/knowledge` 已有可重放的 Snapshot membership/hash、Chunk locator、混合检索与文档生命周期；Wiki 原样复用。
- `packages/agent_runtime` 的 frozen spec / ContextBudgetPolicy / evidence 边界必须保持；本轮不改变历史 AgentVersion 解析。
- `packages/model_gateway` 已有冻结模型链、能力声明、凭据加密与 usage；编译经此入口。
  main 默认 DeepSeek 档案标明原生 JSON Schema 不支持，因此支持显式 PROMPT_JSON，本地严格验证照常生效。
- `packages/evaluation` 已有正式实验固定 DatasetVersion、pricing、build、spec、knowledge 的约束；本轮小实验明确为 DEV 探针，
  不冒充正式 Experiment，不使用 holdout，不改旧数据。集成时必须新增 WikiBinding 冻结而非复用 raw snapshot 冒名替代。
- API 复用现有 workspace membership 与错误 envelope；worker 复用 Celery 加 durable reconciliation；web 无强制新界面，先交付可调用API。
- 基线缺少 RAG 研究分支的新 Evidence/Context Selection 模块；本分支只依赖 main 的 KnowledgeRetriever 稳定协议。

维护与版本契约、数据流和合并规则见 [ADR-012](../adr/ADR-012-versioned-derived-wiki.md)。
