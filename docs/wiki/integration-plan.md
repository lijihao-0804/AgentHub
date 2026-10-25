# Wiki 与 RAG 并轨制度及后续计划

独立阶段的 `codex/knowledge-wiki` 从 main `23f1a38` 建立，验收提交为 `51cb17d`。
2026-10-25 用户另行授权并轨：以最新本地 RAG `fb62b57` 为第一父提交建立
`codex/rag-wiki-integration`，合入 Wiki，再将通过验证的结果快进回 RAG 线。
详细基线、实测范围和未完成项见 [并轨验收](integration-acceptance.md)。
原 RAG 工作区的未提交学习文档不属于本次合并内容，保持原状；main 不变。

## 并轨规则

1. 每次先获取远程分支，再比较本地和远程的祖先关系；本地领先的研究提交同样属于最新基线，不能用旧远程头覆盖。
2. 从最新 RAG 建立集成分支，保留双父合并历史；不改写两条研究线历史，不使用 force push。
3. 在集成分支处理共享入口、迁移及稳定接口的适配；Wiki 不复制或替换 Dense/Sparse/Rerank/Context Selection 算法。
4. 合并提交之前再次核对 RAG 头；若发生推进，先合入新提交并按实际变化补测。通过后才快进 RAG，保留原工作区无关未提交文件。
5. 必须验证 Wiki 生命周期与当前原始检索的组合、来源/租户/快照边界，以及旧发布、Run、Experiment 冻结语义。
6. 合并不隐式启用实验性 EvidenceSet、自动冲突模型复核或未冻结的原生 Agent 工具；旧实验和失败记录保留。

## 接口现状

| 边界 | Wiki 分支采用 | 最新 RAG 实际接口 | 集成处理 |
| --- | --- | --- | --- |
| 原始来源 | main 的 Document/Revision/Chunk/KnowledgeSnapshot | Evidence/Context Selection 的消费方式 | 保留原始快照schema/hash；任何 evidence 都要能返回原始chunk/revision/locator |
| 原始检索 | `KnowledgeRetriever.retrieve(context, RetrievalQuery)` | 同一契约保留；新增 rerank_top_k、max_chunks_per_document、context_budget_bytes、evidence_projection，EvidenceSet 独立可选 | Wiki API 直接组合当前 HybridKnowledgeRetriever；保留默认策略，新可选参数不写入 Wiki 持久模型 |
| 派生知识 | WikiCompilation、WikiPage、revision extraction cache | RAG 原始候选与固定证据 | Wiki 是派生层；可作为额外候选，不冒充 raw chunk |
| Context | 复用 ContextBudgetPolicy；证据标为 untrusted | 默认 ApproxTokenEstimator 按语言估算，统一窗口预算；保留显式 Utf8ByteTokenEstimator | 自动复用当前policy；选到 Wiki 不等于已进入模型窗口 |
| Run 与 Experiment | 本轮保留既有结构，独立 Wiki API 显式 binding | 既有冻结spec、dataset、knowledge、pricing | 统一增加版本化 WikiBinding 并纳入 canonical hash；不能把 raw snapshot hash 当 Wiki hash |
| 来源撤销 | Wiki-only tombstone＋冻结排除集；原模块DELETED也阻断Wiki，raw fallback再过滤 | 原始 RAG 历史读取策略仍由原模块负责 | 集成时另行统一全局撤销策略；不把Wiki维护操作当全局删除入口 |
| API / worker / migration | 新路由、新task/beat项、0032/0033迁移 | 最新 RAG 迁移仍到0031，无编号冲突 | 组合注册已自动合并；全新 PostgreSQL 数据库迁移到0033通过，不改已部署历史 |

以上接口已对照 `fb62b57` 实际源码；研究模块的未来变化仍须经过适配与验收。

## 当前合并与后续原生接入

本次完成下面第1、6项，以及第7项中已有功能的回归。第2–5项涉及新的原生 Agent/Experiment
产品能力，仍是后续里程碑；现有 Wiki API 显式 binding 和混合查询可直接使用。

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
