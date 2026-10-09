# 06 知识与 RAG

[学习首页](README.md) · [上一课：05 持久恢复与事件](05-durability-events.md) · [下一课：07 Memory](07-memory.md)

源码核查基线：`67264b3`，2026-10-09。本课中的源码/流程为静态核查；个人练习尚未代你执行，历史证据保留其日期/SHA。

## 这课要解决什么

把 RAG 学成 Run 的一条证据链：文档如何进索引，检索为何限定快照，结果怎样进入模型预算，引用能证明到什么程度。

## 1. 写入链与读取链分开

```mermaid
flowchart TB
    Upload["上传 / DocumentRevision"] --> Job["入库 job / 队列"]
    Job --> Parse["解析 / 分块 / 嵌入"]
    Parse --> Index["Qdrant / index upsert"]
    Index --> Ready["确认索引 / READY"]
    Ready --> Snapshot["KnowledgeSnapshot / 修订成员"]
    Snapshot --> Search["Run 绑定快照 / search_knowledge"]
    Search --> Hits["检索 / 融合 / 可选重排"]
    Hits --> Evidence["范围校验 / Evidence / 引用"]
    Evidence --> Budget["ContextBudgetPolicy / model"]
```

这是理解阶段图，实际 job 的阶段状态/事务以 worker 与 ingestion 实现为准；创建快照不等于复制所有原始文件进一个大文本。

## 2. 入库如何抵抗重复任务

入口：[KnowledgeService.upload_document](../../packages/knowledge/services.py)→[knowledge worker](../../apps/worker/tasks/knowledge.py)→[ingestion](../../packages/knowledge/ingestion.py)。

读取租约/lease_token、attempt_count、next_attempt_at 与 job claim。Celery 可重复投递；任务必须可重复且可对账，不能仅凭 acks_late 说入库只执行一次。索引点 ID 与修订/chunk 身份帮助重复写保持一致。

beat 的入库对账能发现中间状态，重新推进到可确认的 READY。历史索引后进程退出实验是特定窗口证据，不证明所有网络分区自动修复。

首次加载 BGE/重排模型会影响时延，Compose 配了预热与缓存；“工具没及时返回”不能一律解释为无知识结果。

## 3. KnowledgeSnapshot 到底冻结什么

`KnowledgeSnapshotService.create_current_snapshot` 把当前满足条件的修订集合物化；`resolve_snapshot` 装载明确身份或解析 LATEST。hash 包含 schema、知识库身份及 document/revision 成员。

你应分清 document（逻辑文档）、revision（具体内容修订）、chunk（修订分片）、snapshot（成员集合）。新上传一版不会自动改写旧 snapshot。旧实验使用的修订需留存。

[T12 preview_chunks](../../packages/knowledge/snapshots.py) 只读取冻结成员，默认 limit 10、最大 20，文本有截断标志；它是检查证据的入口，不是绕过内容权限的全文导出。

## 4. Dense / Hybrid / Rerank 分别解决什么

| 阶段 | 直观解释 | 代码观察点 |
| --- | --- | --- |
| Dense | 用语义向量找相似分片 | dense_embedder / dense_search |
| Sparse | 用稀疏特征提供另一种召回 | sparse_encoder / sparse_search |
| Fusion | 融合候选排序，如 RRF | fuse_reciprocal_rank |
| Rerank | 对候选再计算匹配分数 | reranker / candidate 与 final top-k |

打开 [HybridKnowledgeRetriever.retrieve_with_trace](../../packages/knowledge/retrieval.py)。先看 `_snapshot_scope`，再看检索阶段，最后看 `_load_chunks` 和 evidence 构造。候选来自索引不等于都可跨租户/快照使用。

[_snapshot_scope](../../packages/knowledge/retrieval.py) 把快照成员转换为检索范围；后续从数据库取真实 chunk 并验证身份。LangGraph 不负责替你做这些知识范围校验。

## 5. 检索到 → 准入 → 回答 → 引用，是四个问题

| 问题 | 该看什么 |
| --- | --- |
| 有关 chunk 是否找到了？ | recall / ranking / retrieval trace |
| 是否真的送到模型？ | ContextAdmissionResult / tool-result budget |
| 答案是否符合政策？ | 独立语义评估与 reference/evidence |
| 引用是否支持这个命题？ | revision/chunk 身份与 claim-to-source 核查 |

引用 ID 存在，只能先证明它来自某证据身份，不能自动证明回答中全部话都被支持。答案额外引入相邻政策也可能“有真实引用但负担/语义不佳”。

## 6. 用真实实验学习，不拿小样本夸结论

[正式 RAG 对照](../reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md)：8 个短政策、24 DEV 问题、两策略各三次，共 144 次真实检索生成。最终目标召回两组均 72/72；Dense MRR 0.9722、Hybrid 0.9931，Hybrid 时延更高。

这说明所测小语料上排序略有变化、最终目标召回未提高；不能宣称 Hybrid 在所有业务更好。抽检只覆盖一部分回答，引用存在不等于全批次零幻觉。

数据源和 runner 在 [benchmarks/evaluation](../../benchmarks/evaluation)，原始结果在 [evidence](../reviews/evidence/mi3-mi4-20261002)。复现前核对数据/schema/code/model/pricing 身份，不先换默认策略。

## 7. 学习步骤和自测

依次打开 `upload_document`、`ingestion`、`create_current_snapshot`、`retrieve_with_trace`、[search_knowledge builtin](../../packages/tools/builtins/search_knowledge.py)。画出 document→revision→chunk→snapshot→evidence。

纸上预测：只上传新文档、不更新固定 snapshot，旧 Run 能否看见它？答案要包含 snapshot 成员、LATEST 策略和实际解析时点，不能简单说“知识库有就能用”。

**面试追问：**Qdrant 的结果为何还查数据库？快照 hash 与文件 bytes hash 是否同一件事？检索 recall=1 能否说明任务成功率=1？

已有核查：[snapshot 测试](../../tests/integration/test_m3f_snapshot.py)、[检索策略单测](../../tests/unit/test_knowledge_m7e_strategy.py)。本课未运行。

**通过标准：**画完整写入/读取链，解释范围隔离、冻结与引用边界，而不只说“embedding+vector DB”。

---

[学习首页](README.md) · [上一课：05 持久恢复与事件](05-durability-events.md) · [下一课：07 Memory](07-memory.md)
