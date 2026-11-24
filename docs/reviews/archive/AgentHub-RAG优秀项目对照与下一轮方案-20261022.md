# AgentHub：优秀 RAG 项目对照与下一轮优化方案

> 后续状态（2026-10-23）：下文为历史调研及当时建议，部分实验已有负结果，不能作为当前待执行清单。最新为 [RAGFlow、Onyx、Dify 实际源码架构对照与改造决策](AgentHub-RAG源码架构对照与改造决策-20261023.md)，优先级已根据真实消费者阶段损失修正；不继续扫参数。

日期：2026-10-22。状态：调研与 DEV 离线诊断完成；下文是实验建议，没有新增优化成绩或开启生产改造。

## 结论

下一轮优先做候选池质量消融，随后验证真正的学习型稀疏检索；长文证据完整性再引入父子分块。
图检索安排到跨文档、多跳问题上单独评估。项目选择依据是可核查的官方实现及与 AgentHub 的问题匹配，不按 star 数或宣传分数判定优劣。

现有基线已经有 Dense + Sparse + RRF + Cross-Encoder，因此直接引入另一整套 RAG 框架的必要性不足。
值得借鉴的是候选预算、融合、多样性、分块/证据组织和评测分解；第三方实现仍应通过现有 adapter/contract 接入。

## 官方项目与可借鉴部分

| 项目 | 核查到的设计 | 对 AgentHub 的价值与适用边界 |
| --- | --- | --- |
| [RAGFlow](https://github.com/infiniflow/ragflow) | [Search Settings](https://ragflow.io/docs/search_settings) 分开配置混合检索权重、重排候选池和重排模型；候选池大小有覆盖与时延权衡。README 另描述模板分块和文档解析。 | 优先学习候选池与最终上下文预算分离。复杂 PDF 的解析优化需要真实 PDF/扫描件任务，不能用已经提供结构化摘要的 SciFact 证明收益。 |
| [Haystack](https://github.com/deepset-ai/haystack) | [DocumentJoiner](https://docs.haystack.deepset.ai/docs/3.1/documentjoiner) 支持多路结果融合与 RRF；[DiversityRanker](https://docs.haystack.deepset.ai/docs/sentencetransformersdiversityranker) 处理结果多样性。 | 把“多路召回、融合、候选选择、重排”拆开做消融。多样性不是硬性每文档只留一块，长文问答可能需要同文档多段证据。 |
| [LlamaIndex](https://github.com/run-llama/llama_index) | [AutoMergingRetriever](https://developers.llamaindex.ai/python/framework/integrations/retrievers/auto_merging_retriever/) 先检索叶子节点，再按父子关系和覆盖阈值合并较完整的上下文。 | 适合 QASPER 等长文任务：小块检索、大块供回答。需在同一历史 revision 内扩展，并经过 ContextBudgetPolicy；先选中不等于实际准入。 |
| [LightRAG](https://github.com/HKUDS/LightRAG) | README 与 QueryParam 区分 local、global、naive、mix；mix 组合图实体/关系与原始文本向量检索，支持重排。 | 值得在 MuSiQue 多跳任务上研究。图索引的抽取、更新、费用、租户隔离和历史快照都要计入，不能由单跳 SciFact 分数推出图检索有收益。 |
| [FlagEmbedding / BGE-M3](https://github.com/FlagOpen/FlagEmbedding/tree/master/research/BGE_M3)（检索组件） | [官方模型卡](https://huggingface.co/BAAI/bge-m3) 展示同时输出 dense、learned lexical weights 和 ColBERT；示例通过 `return_sparse=True` 得到 `lexical_weights`。 | AgentHub 当前 sparse.py 只使用 tokenizer 词频权重，不能当成已经启用了 BGE-M3 学习型 sparse。它是一个明确可验证的下一阶段替代项，而不是已证明有效的升级。 |

源码核查入口：

- RAGFlow [reranker.go，固定 commit](https://github.com/infiniflow/ragflow/blob/e2401e54947db7eb5e542b6de26755d89886f783/internal/service/nlp/reranker.go)。当前主线是 Go 代码，不能把旧 Python 路径当作当前实现。
- Haystack [document_joiner.py](https://github.com/deepset-ai/haystack/blob/main/haystack/components/joiners/document_joiner.py)。
- LlamaIndex [auto_merging_retriever.py](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/retrievers/auto_merging_retriever.py)。
- LightRAG [base.py / QueryParam](https://github.com/HKUDS/LightRAG/blob/main/lightrag/base.py)。其中 hybrid 指图 local/global 的组合，与 AgentHub 的 dense/lexical hybrid 含义不同。

官方项目的性能宣传和其他数据上的分数没有转记为 AgentHub 收益；上表后的优先级是结合本项目数据作出的判断。

## 当前 DEV 实际损失在哪里

原生 DeepSeek 子代理只读取官方 train qrels、既有 DEV trace 和 DEV 评分结果，未运行模型或访问 test。
809 个问题包含 919 个正例“问题—文档”对。按每题正例比例做宏平均，按上游 document ID 去重。

| 池或阶段 | 宏平均正例文档覆盖 | 解释 |
| --- | ---: | --- |
| Dense 前 30 chunks | 0.886815 | 30 是 chunk 预算，不是 30 篇文档 |
| Sparse 前 30 chunks | 0.830985 | 当前 tokenizer log-TF + Qdrant IDF |
| Dense30 ∪ Sparse30 无序并集 | 0.921323 | 可用候选的 oracle 覆盖上界，没有排序，不能称 Recall@20 |
| Fusion 前 20 chunks | 0.876102 | 平均 17.012 篇独立文档；平均 2.988 个块来自重复文档 |
| 重排后文档级 Recall@5 | 0.811578 | 从全部 20 个重排候选先按文档去重，再取前 5 篇；来源为既有规范评分结果 |

Fusion20 命中 800/919 个正例对，漏掉 119 对。其中：

- **45 对 / 45 个问题**的 gold 已在两路召回并集中，却没有进入 Fusion20；扩池或调整候选选择具有可测的恢复空间。
- **74 对 / 71 个问题**在两路召回中都不存在，单纯扩大融合候选池不能找回；需改召回、索引或查询表示。
- 其中 **58 个问题**在两路并集中没有任何正例文档。
- Fusion20 对 698 题覆盖所有 gold；无序并集对 738 题覆盖所有 gold。扩池理论上可使 40 个受影响问题恢复全覆盖。

0.921323 与 0.876102 的差约 4.52 个百分点，只是固定两路候选集合的覆盖空间，不是预期准确率提升。
真正的重排、最终上下文准入与生成都可能继续损失证据。文档标签也不能证明命中的具体 chunk 足以支持答案。

核对后的[DEV 诊断数据](../../benchmarks/public_rag/results/scifact-20261021/dev-opportunity-reviewed-20261022.json)保留逐题 ID 和分母。
其中原始前 5 chunks 的覆盖率 0.803852 单独标记；规范的文档级 Recall@5 为 0.811578，二者不混用。

## 下一轮建议：先做一个候选质量里程碑

先保留现有模型、语料、索引和 SQL 优化，把以下变体分开比较：

| 变体 | 改动 | 回答的问题 |
| --- | --- | --- |
| A：基线 | dense/sparse 各 30，fusion20，rerank20 | 当前质量与成本 |
| B：等预算候选选择 | 重排仍为 20；在融合候选上试验有限的同文档块配额或近重复抑制，保留必要多段证据 | 同样 20 次重排能否覆盖更多有用文档 |
| C：扩候选池 | 仅改变候选/重排预算，绘制 20/40/60 的质量—时延曲线；最终上下文预算固定 | 45 个可恢复正例的潜力是否值得额外重排费用 |

B 和 C 不同时叠加后宣称单项收益。用 DEV 做有限选择，冻结后再确认；质量不提高、时延代价过大或证据丢失都保留为负结果。
首要质量指标为 nDCG@10，辅以候选覆盖、Recall@5/10/20、MRR；同时报告真实重排对数、有效上下文 token、时延和峰值显存。
成对比较以问题为单位，相关问题按正例来源分组；重复运行用于估计运行波动，不增加独立问题数。

SciFact test 已经在上一轮公开读取过成绩，之后只能作为已暴露的回归集合，不能再次称为全新盲测。
下一轮确认需要预先指定尚未用于调参的独立评估集合，或建立明确记录暴露的新评估批次。QASPER 的证据问答和跨论文混合语料检索要分别说明标签假设，不能直接冒充官方 BEIR 任务。

## 后续顺序与技术边界

1. **学习型 sparse 消融。** 对照现有 tokenizer sparse 与 BGE-M3 learned sparse，其他条件不变；独立索引保存编码语义。当前 Qdrant adapter 固定启用 IDF，学习型权重不能未验证就套用同一 modifier。验证权重缓存、共享 encoder、显存与推理成本，避免为 dense/sparse 重复加载大模型。
2. **长文证据完整性。** 在 QASPER 等任务上比较当前字符块与段落/父子块，测证据覆盖和最终答案。父块扩展受 revision、租户、引用与模型上下文预算约束。
3. **完整 RAG 评分。** 固定已授权的 `deepseek-v4.1-flash-1017`、生成参数与 token 预算；评判实际准入上下文和生成答案。结合可复核的答案/证据指标、无答案识别及经过样例校准的 judge；LLM judge 不当作人工真值。官方严格 schema 不可用等既有能力守卫保留。
4. **多跳/图检索。** 单独在 MuSiQue 等多跳任务上比较直接检索、查询分解和图辅助检索，连同建图与增量维护成本一并评估；不在单跳语料上预设 GraphRAG 更好。

另有一个可单独排队的速度方向：参考 Haystack 的[并行多路检索](https://haystack.deepset.ai/release-notes/2.29.0)，比较当前顺序执行的 dense/sparse 分支与受控并行。
它应独立于上述质量变体，检查 SDK/模型并发行为、错误与取消处理，并以排序保持一致和成对时延为验收条件。
将来若 dense/learned sparse 共用一次 encoder 前向，则应优先共享计算，而不是为并行重复推理。

本次没有修改生产检索逻辑、安装其他框架、重建索引或调用实验模型 API，也没有新增哈希测试或完整性盘点。已有源码改动与前一轮实测证据保留。
