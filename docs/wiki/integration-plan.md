# Wiki 与 RAG 研究线的集成准备

本轮只交付 `codex/knowledge-wiki`，从 main `23f1a38` 独立建立，未合并 main 或 RAG 分支。
原工作区 `codex/rag-aliyun-regression-20261024` 的未提交研究内容保持原状。

## 接口现状

| 边界 | Wiki 分支采用 | RAG 研究线可能演进 | 集成处理 |
| --- | --- | --- | --- |
| 原始来源 | main 的 Document/Revision/Chunk/KnowledgeSnapshot | Evidence/Context Selection 的消费方式 | 保留原始快照schema/hash；任何 evidence 都要能返回原始chunk/revision/locator |
| 原始检索 | `KnowledgeRetriever.retrieve(context, RetrievalQuery)` | EvidenceSet、EvidencePlan、候选/上下文选择和证据交付 | 在 `raw_retriever` 适配；不把策略枚举和选片字段复制进 Wiki 持久模型 |
| 派生知识 | WikiCompilation、WikiPage、revision extraction cache | RAG 原始候选与固定证据 | Wiki 是派生层；可作为额外候选，不冒充 raw chunk |
| Context | 复用 ContextBudgetPolicy；证据标为 untrusted | 最终上下文选择/压缩 | 最终准入仍由统一policy；选到 Wiki 不等于已进入模型窗口 |
| Run 与 Experiment | 本轮保留既有结构，独立 Wiki API 显式 binding | 既有冻结spec、dataset、knowledge、pricing | 统一增加版本化 WikiBinding 并纳入 canonical hash；不能把 raw snapshot hash 当 Wiki hash |
| 来源撤销 | Wiki-only tombstone＋冻结排除集；原模块DELETED也阻断Wiki，raw fallback再过滤 | 原始 RAG 历史读取策略仍由原模块负责 | 集成时另行统一全局撤销策略；不把Wiki维护操作当全局删除入口 |
| API / worker / migration | 新路由、新task/beat项、0032/0033迁移 | 研究线可能也修改同组合入口或新增迁移 | 手工组合注册；迁移编号有冲突时在集成分支统一重排/创建Alembic合并revision，不改已部署历史 |

这里描述的是边界差异，不宣称读过尚不存在的最终研究线契约。Wiki 不导入未合并模块。

## 集成顺序（本轮不执行）

1. 在专门集成分支核对两条线的最终提交与迁移 head；先合并 additive Wiki 模块。
2. 统一证据 envelope：明确 RAW / WIKI_DERIVED、源revision、原始quote、publication ID/hash、路由与不足状态。
3. 在 AgentVersion 发布时冻结 WikiBinding；LATEST 只在发布/Experiment variant解析一次。
4. Run 创建/恢复与正式 Experiment 验证同时固定 Wiki与原始快照，拒绝无法解析/不匹配/已撤销绑定。
5. 新的原生 Wiki/Hybrid 工具必须通过既有 ToolRevision、effect/risk/approval、权限、审计和预算机制。
6. 让 RAG 分支的检索实现接到 raw_retriever；保持 Wiki生命周期模型独立。复杂图检索在有对照收益后另立范围。
7. 在集成分支执行相关 API、发布、Run、Experiment、来源撤销和故障回归，再做同配置真实对照。

## 必须保留的质量边界

Wiki-only 消融不会偷偷补检；AUTO fact/synthesis 才按路由契约补原始证据。
本轮真实小实验的 Dense+MiniLM 是明确配置的基线，不代表生产默认 BGE-M3+Sparse+Rerank 的效果。
不得把 DEV 小样本的收益外推为生产优势，也不得把检索结构可用计为语义答案正确。
不覆盖旧实验结果，不消耗正式 holdout，不放松来源/hash/权限/预算验证。
