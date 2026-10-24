# AgentHub RAG 召回率提升机制调研与立项

- 基线：`96587c3`（交付链修复已合入分支）。目标：回答"大规模施工后能有效优化机制、提高召回率的立项方向"，基于对优秀 GitHub 项目与公开文献的调研。
- 方法：Web 检索核实各项目机制与公开数字（2026-10-24），结合本仓库五轮审阅的实测数据（候选池召回、hop 分层、漏斗、已确认收益清单）做适配性判断。部分检索超时的条目已注明"数字未刷新"，引用以仓库既有归因文档与官方仓库为准。
- 结论先行：**单跳召回不是瓶颈（候选池召回 0.992）**。真正的召回缺口有四个：**多跳组织（4-hop 29.5%）、PDF/表格语料的解析保真、固定窗口缺文档语境的消歧、语料级主题问题（完全无支持）**。据此给出 10 个立项（P-E0/P-C1/P-A1 为前三优先），全部符合仓库"预登记门槛+冻结重放+负结果保留"纪律，不重开已 REJECT 的方向。

---

## 一、当前召回的真实缺口（用我们自己的数据定位）

| 缺口 | 证据（本仓库实测） | 说明 |
| --- | --- | --- |
| 单跳候选池召回 | MuSiQue 200 题候选池召回 **0.992**（修复报告重放） | 单跳检索召回已近满，继续调单跳参数收益天花板极低 |
| 交付后有效召回 | 0.783（记录级）/0.743（全文级）；gold 臂 1.0 | 已由本轮交付链修复大幅收窄；剩余属集合选择/预算——互补集合（+16.15pp 已确认）仍是 opt-in |
| 多跳组织 | 修复后 2-hop 44.6% / 3-hop 40.5% / 4-hop **29.5%** | hop 越多损失越大；单条 query 单发检索的结构性缺陷（R-08 NOT_TESTED） |
| 解析保真 | QASPER 结构化全文低分部分源于 pypdf 解析（归因文档已证）；生产 parser 仅 3 种媒体类型 | PDF 表格/阅读顺序/OCR 缺失——**上游丢了下游全链路无法恢复**（R-12） |
| 窗口语境消歧 | MAML 型边界事实案例；"the city"式指代在孤立窗口中不可检索 | 固定窗口 1200/200 的已知缺陷，切块方案作为整体未过推广门（R-11 REJECTED 于原条件） |
| 语料级主题问题 | 现有链路完全无支持（无全局摘要/图结构） | "这批文档的主要主题/分歧是什么"类问题无机制 |

## 二、调研项目清单

### A. 摄入时上下文增强（对症：窗口语境消歧）

**[Anthropic Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval)**（官方实现：[claude-cookbooks/contextual-embeddings](https://github.com/anthropics/claude-cookbooks/blob/main/capabilities/contextual-embeddings/guide.ipynb)）
- 机制：为每个 chunk 用 LLM 生成 50-100 token 的"在全文中的位置说明"前缀，再嵌入+BM25 索引——chunk 带着文档语境被检索。
- 公开数字：top-20 检索失败率 5.7% → 上下文嵌入 3.7%（−35%）→ +上下文 BM25 2.9%（−49%）→ +重排 **1.9%（−67%）**。
- 适配点：与固定窗口并存（不动切块，只改嵌入输入）；前缀属于新 revision 的内容（chunk_id 哈希随之变化，符合不可变身份契约）；摄入是 Celery 管线，天然可异步+缓存+增量。
- 成本：每 chunk 一次 LLM 调用（可用便宜模型生成前缀）；索引重建一次。
- 风险：中文语料效果需自测；前缀质量依赖生成模型。
- **立项 P-A1**：摄入管线新增 contextual 前缀步骤（revision 级、可缓存、失败可跳过回退原 chunk），冻结 SciFact/FiQA + MuSiQue 重放测召回，门槛：候选池/最终召回 ≥ +3pp 且 CI 下界 >0。

### B. Late Chunking（对症：窗口语境消歧，与 A 同族）

**[jina-ai/late-chunking](https://github.com/jina-ai/late-chunking)**（论文 [arXiv:2409.04701](https://arxiv.org/abs/2409.04701)）
- 机制：整篇文档先过 8k-token 长上下文嵌入模型，切块后再分别池化——每个 chunk 向量天然携带全文语境，零 LLM 调用。
- 公开数字（nDCG@10，naive→late）：SciFact 64.2→66.1、NFCorpus 23.5→**30.0**、FiQA 33.3→33.8——一致但幅度小。
- 适配点/冲突：**需要换嵌入模型**（jina-embeddings-v2 系列）→ 属于决策 #2"替换 BGE-M3"的 NOT_TESTED 范畴，须按归因文档纪律做隔离索引的公平比较；与 P-A1 互斥（同为语境增强，一个在数据侧一个在模型侧）。
- **立项 P-A2**（P-A1 之后视结果决定是否做）：隔离索引公平比较，门槛同上；不与 P-A1 同时上。

### C. 文档理解解析（对症：解析保真，上游召回上限）

**[infiniflow/ragflow](https://github.com/infiniflow/ragflow)**（92.8k★，[DeepDoc README](https://github.com/infiniflow/ragflow/blob/main/deepdoc/README.md)、[配置文档](https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/configuration.md)）
- 机制：DeepDoc 布局识别 10 类组件（正文/标题/图/图注/表/表注/页眉/页脚/引用/公式）、表格结构 5 标签（列/行/表头/投影行头/跨格）并**重组为句子的表格内容**、OCR、12 种模板化切块（Q&A/手册/表格/论文/法规/书籍…按行/按节/按问答对）。
- 适配点：直接对应 R-12 与 QASPER PDF 低分的根因；表格重组输出可映射为本仓库 ParsedBlock（带 page/位置 locator，兼容现有 chunk 身份机制）。
- 成本/风险：引入视觉模型推理（OCR/布局）；这是**最大的施工项**，但对 PDF 语料是"先于一切检索算法"的投入（审阅结论原文）。
- **立项 P-B1**（分阶段）：阶段一仅表格+阅读顺序（不做 OCR），选表格密集语料（如 SciFact 补充集或自构合同/报表集）测解析保真与下游召回；阶段二再评估 OCR 与模板切块。第三方框架按规则 4 隔离在 adapter 后。

### D. 查询分解与多查询融合（对症：多跳组织——当前最大缺口）

**[RAG-Fusion / LlamaIndex QueryFusionRetriever](https://developers.llamaindex.ai/python/framework/integrations/retrievers/reciprocal_rerank_fusion/)**（原论文 [arXiv:2402.03367](https://arxiv.org/abs/2402.03367)；[MMLF, NAACL 2025](https://aclanthology.org/anthology-files/anthology-files/pdf/naacl/2025.naacl-findings.367.pdf) 报告子查询+扩展+晚融合平均 **+4~8% Recall@1k/nDCG@10**）
- 机制：LLM 生成 N 个查询变体（或子问题）分别检索，RRF（k=60）融合——**与本仓库现有 RRF 完全同构**，接入成本低（融合入口已存在）。
- 适配点：多跳题（"A 的创始人的公司投了谁"）分解为子查询分别检索，正是 4-hop 29.5% 缺口的对症解；与 R-08"一次有界补检索"互补（分解是检索前、补检索是检索后）；R-06 池新者优先已为多轮检索扫清预算障碍。
- 成本：每查询 +1 次 LLM 调用 + N 次检索。
- **立项 P-C1**（中优先、高杠杆）：受控分解→多查询→现有 RRF 融合，MuSiQue hop 分层为主指标，门槛：3/4-hop 召回与 F1 各 ≥ +5pp 且 CI 下界 >0；禁止对所有问题强制分解（按题型触发）。

### E. Small-to-Big / 句子窗口 / 自动合并（对症：窗口语境+交付完整性，谨慎）

**[LlamaIndex AutoMergingRetriever](https://developers.llamaindex.ai/python/framework/integrations/retrievers/auto_merging_retriever/)**（阈值 0.5 递归合并兄弟节点）、[SentenceWindowNodeParser](https://docs.llamaindex.ai/en/v0.10.34/examples/node_postprocessor/MetadataReplacementDemo/)、[LangChain ParentDocumentRetriever](https://reference.langchain.com/python/langchain-classic/retrievers/parent_document_retriever/ParentDocumentRetriever)
- 机制：细粒度索引匹配、粗粒度视图交付（句子窗口/父子层级/兄弟合并）。
- **与已 REJECT 方向的边界（重要）**：本仓库已否决的是"检索后恢复父段"（段落恢复两候选未过 +10pp 门槛）与"邻块拼接冒充父子"（归因文档明令）。Auto-merging 的前提是**构建时建立层级身份与范围视图**——机制不同、前提不同，但同族风险（交付变大挤预算）。
- **立项 P-C2**（低优先、一次试点即止）：以"层级 chunk 身份 + 独立范围视图（不回写原 chunk）"为前提做一次冻结试点；未达门槛即关闭，不扫参数（吸取段落恢复教训）。

### F. 实体图检索（对症：语料级主题/关系问题）

**[HKUDS/LightRAG](https://github.com/HKUDS/LightRAG)**（约 40.3k★）与 **[microsoft/graphrag](https://github.com/microsoft/graphrag)**（约 36.4k★）
- 机制：GraphRAG 用 Leiden 算法建社区层级+逐层 LLM 摘要，全局问题 map-reduce 社区报告；LightRAG 扁平实体-关系图+双级关键词检索，无层级。
- 公开成本数据（[LightRAG 论文](https://aclanthology.org/2025.findings-emnlp.568.pdf)：法律集 GraphRAG 检索约 61 万 token vs LightRAG <100；[AWS 基准](https://aws.amazon.com/blogs/opensource/unified-knowledge-graph-rag-and-lightrag-on-one-stack/)：每千次查询 GraphRAG local $5.23 / LightRAG mix $38.67 / GraphRAG global $66.22；摄入成本两系统均由实体/关系抽取主导）。
- 适配点：企业知识库"新政策主题/多文档分歧"类问题是现有机制零覆盖的真需求；快照模型可承载（图=快照级独立投影）；实体抽取是 Celery 摄入步骤的自然延伸。
- **立项 P-D1**（大施工、后置）：从 LightRAG 型轻量实体图开始（不做社区层级），冻结 10-20 篇小语料试点；关系抽取的曝光与成本账本必须预登记。GraphRAG 层级摘要暂不立项（成本/收益比劣后）。

### G. 递归摘要树（对症：长文档多抽象层问题）

**[parthsarthi03/raptor](https://github.com/parthsarthi03/raptor)**（ICLR 2024，[arXiv:2401.18059](https://arxiv.org/abs/2401.18059)）
- 机制：递归"嵌入→聚类→摘要"构建多层树，检索可命中不同抽象层。
- 公开数字：QuALITY 基准较此前最佳 **+20pp**（与 GPT-4 组合）。
- 适配点：树节点=快照级独立投影（不回写原 chunk，符合契约）；与 P-D1 图结构互补（树管抽象层、图管关系）。
- **立项 P-D2**（后置候选）：仅在 P-A1/P-C1 结果显示"摘要层证据缺失"为主要损失时启动。

### H. 多向量迟交互（对症：匹配精度，工程量大）

**ColBERT/PLAID 迟交互**（[stanford-futuredata/ColBERT](https://github.com/stanford-futuredata/ColBERT)）：每 token 一向量、MaxSim 迟交互，BEIR 上对单向量 dense 有稳定优势；PLAID 提供检索加速。**本轮检索超时未能刷新数字**，按已知文献与官方仓库标注。Qdrant 支持多向量，但索引体积约 ×100、全链路需重建——列为长期备选 **P-E1**，仅当 P-A 系与 P-C 系收益不足时评估。

### I. 重排器/嵌入模型替换（既有纪律维持）

**Qwen3-Embedding/Reranker**：归因文档已有明确接入指引（查询/文档不同输入处理、reranker yes/no 末位 logit 与指定模板、隔离索引、固定候选池）——决策 #2/#4 维持 NOT_TESTED。列为 **P-E2**：任何替换必须按上述纪律公平比较，公开榜单不进成绩单。

### J. 已确认收益的最后一公里（零研究成本）

**Onyx 选择器接入**：FiQA 648 题独立确认 nDCG@10 **+0.0398（CI [+0.029,++0.051]）**，adapter 已在仓库（`onyx_selection_adapter.py`），仅生产未接入（均增 744ms）。**立项 P-E0**：做成版本级 opt-in 配置即可，无需新实验。

## 三、与仓库纪律的兼容性检查

1. **不重开已 REJECT 方向**：learned sparse、DBSF、父文档重排、段长/提示参数扫描——以上立项均未触及；P-C2 与已否决的"检索后父段恢复"前提不同，但设"一次试点即止"条款。
2. **不可变身份契约**：P-A1 前缀/P-C2 范围视图/P-D1 图/P-D2 树均以"新 revision 或独立投影、不回写原 chunk"为设计前提。
3. **预登记+冻结+门槛**：每个立项先登记协议（数据、指标、门槛、成本上限）再执行；负结果保留（研究纪律的既有资产）。
4. **依赖单向**：DeepDoc/RAPTOR/LightRAG 等第三方按规则 4 隔离于 adapter 后，稳定契约先行。
5. **成本账本**：摄入时 LLM 调用（P-A1/P-D1/P-D2）全部入账，与快照哈希绑定（规则 18/25）。

## 四、优先级与施工量分级

| 优先 | 立项 | 施工量 | 预期（引用公开数字+本仓数据） | 对准缺口 |
| --- | --- | --- | --- | --- |
| 1 | **P-E0** Onyx 选择器 opt-in 接入 | 小（配置化） | 已确认 +0.0398 nDCG，零研究风险 | 排序质量 |
| 2 | **P-C1** 查询分解+多查询融合 | 中（1 个 LLM 调用 + 现有 RRF 入口） | 多跳 +4~8% 量级（MMLF），hop 分层为直接指标 | 多跳组织（最大缺口） |
| 3 | **P-A1** Contextual 前缀 | 中（摄入管线步骤+缓存+重建索引） | 检索失败率 −35~49%（Anthropic 口径） | 窗口语境消歧 |
| 4 | **P-B1** DeepDoc 型解析（阶段一：表格+阅读顺序） | 大 | PDF 语料召回上限解锁（无公开可比数字，须自建金标） | 解析保真 |
| 5 | **P-D1** 轻量实体图（LightRAG 型） | 大 | 主题/关系类问题从 0 到有；成本预警已知 | 语料级问题 |
| 后备 | P-A2 late chunking / P-E2 模型替换 / P-C2 层级试点 / P-D2 RAPTOR / P-E1 ColBERT | 各异 | 视前排结果 | — |

**执行顺序建议**：P-E0 立刻可做（纯工程）；P-C1 与 P-A1 并行做受控实验（各自冻结重放+MuSiQue/FiQA）；两者出结果后再决定 P-B1/P-D1 的大施工排期——避免在交付层刚修复、测量刚校准的当口同时铺开多个大工程。

## 五、来源

- [Anthropic: Introducing Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval) · [claude-cookbooks 实现](https://github.com/anthropics/claude-cookbooks/blob/main/capabilities/contextual-embeddings/guide.ipynb)
- [jina-ai/late-chunking](https://github.com/jina-ai/late-chunking) · [arXiv:2409.04701](https://arxiv.org/abs/2409.04701) · [Jina 博客](https://jina.ai/news/late-chunking-in-long-context-embedding-models/)
- [infiniflow/ragflow](https://github.com/infiniflow/ragflow) · [DeepDoc README](https://github.com/infiniflow/ragflow/blob/main/deepdoc/README.md) · [配置文档](https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/configuration.md)
- [RAG-Fusion 论文](https://arxiv.org/abs/2402.03367) · [LlamaIndex QueryFusionRetriever](https://developers.llamaindex.ai/python/framework/integrations/retrievers/reciprocal_rerank_fusion/) · [MMLF NAACL 2025](https://aclanthology.org/anthology-files/anthology-files/pdf/naacl/2025.naacl-findings.367.pdf)
- [AutoMergingRetriever](https://developers.llamaindex.ai/python/framework/integrations/retrievers/auto_merging_retriever/) · [SentenceWindow 示例](https://docs.llamaindex.ai/en/v0.10.34/examples/node_postprocessor/MetadataReplacementDemo/) · [ParentDocumentRetriever](https://reference.langchain.com/python/langchain-classic/retrievers/parent_document_retriever/ParentDocumentRetriever)
- [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) · [microsoft/graphrag](https://github.com/microsoft/graphrag) · [LightRAG 论文](https://aclanthology.org/2025.findings-emnlp.568.pdf) · [AWS 成本基准](https://aws.amazon.com/blogs/opensource/unified-knowledge-graph-rag-and-lightrag-on-one-stack/)
- [parthsarthi03/raptor](https://github.com/parthsarthi03/raptor) · [arXiv:2401.18059](https://arxiv.org/abs/2401.18059)
- [stanford-futuredata/ColBERT](https://github.com/stanford-futuredata/ColBERT)（数字未刷新，见文中标注）
- 本仓库既有研究：[全链路性能归因与决策](AgentHub-RAG全链路性能归因与决策-20261024.md)（Qwen3 接入指引、Haystack/Onyx/RAGChecker 引用、已 REJECT 清单）、[审阅问题总汇](AgentHub-RAG审阅问题总汇报告-42215c8.md)（缺口数据）、[修改进度与收益复核](AgentHub-RAG修改进度与收益复核-42215c8.md)（修复后 hop 分层）

## 声明

调研报告，不含代码改动；公开数字均为各项目自报或论文口径，进入本仓库成绩单前须按预登记纪律自测；LightRAG/GraphRAG 星数为检索时点近似值。
