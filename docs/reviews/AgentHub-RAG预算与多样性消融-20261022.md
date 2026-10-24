# AgentHub RAG：预算分离与文档多样性消融

## 当前结论

两个能力已经实现，均为可选配置。完整 DEV 和真实五臂回归均已完成。
按预先声明的 DEV 规则冻结保留 baseline，回归不重新选型。功能与评测验收通过，不更改默认策略。

## 外部依据和落地范围

[RAGFlow Search Settings](https://ragflow.io/docs/search_settings) 区分候选数量与后续排序、展示的作用，
强调扩大候选同时增加开销。本轮把融合池 `candidate_top_k` 与独立重排预算 `rerank_top_k` 分开，
最终返回数量由 `final_top_k` 控制。

[Haystack DiversityRanker](https://docs.haystack.deepset.ai/docs/sentencetransformersdiversityranker)
把相关性和结果多样性作为单独的选择问题。本轮实现低成本的文档软配额，
并非复刻它的 embedding MMR：水合并通过快照校验后，优先保留每篇文档配额内的块，
不足预算时补回被延期的同文档块，保持融合顺序。

这些公开方法提供设计依据，并不保证在 AgentHub 或 SciFact 上必然获益。

## 代码行为与兼容

- Query 新增 `rerank_top_k: int | None`，None 继承融合池预算。
- Query 新增 `max_chunks_per_document: int | None`，None 禁用多样性选择。
- 只对 `HYBRID_RERANK` 的重排输入生效；DENSE、HYBRID 不改变输出策略。
- 来源 ID 来自现有 SQL 水合结果；缺失、越租户、越快照的候选不能占用配额或进入重排。
- 未修改已发布版本；旧 resolved 配置不自动添加新默认字段。
- Agent 发布、search_knowledge、playground 和安全的 selection trace 已接线。
- 不新增哈希测试，不改变 ContextBudgetPolicy、relevance floor 或历史保留语义。

使用和复现实例见 [BUDGET_ABLATION.md](../../benchmarks/public_rag/BUDGET_ABLATION.md)。

## 固定协议

[完整协议](../../benchmarks/public_rag/results/budget-diversity-20261022/protocol.json) 在本轮评分前写入。
SciFact 全语料 5,183 文档、9,189 生产切块；dense/sparse 各 30，RRF k=60；
BGE-M3 dense、tokenizer log-TF + Qdrant IDF、BGE reranker-v2-m3，CUDA batch8。

| 变体 | 融合池上限 | 重排上限 | 文档软配额 | 最终块上限 |
|---|---:|---:|---:|---:|
| baseline | 20 | 20 | 关闭 | 20 |
| diversity20 | 60 | 20 | 2 | 20 |
| budget40 | 60 | 40 | 关闭 | 20 |
| combined40 | 60 | 40 | 2 | 20 |
| budget60 | 60 | 60 | 关闭 | 20 |

DEV 用全部 809 个 official train query。每题只对全候选集合做一次交叉编码器评分，
再重放子集用于质量筛选，不把这种缓存路径当成真实时延。
回归用全部 300 个 official test query，逐题随机交错执行五组真实生产检索路径；
单并发、共享预热模型，时延排除首题所有组。测试集已经暴露，本轮只称回归，不称盲测。

文档指标先对最多 20 个最终原始块按 source document 去重，再应用指标截断。
候选集合覆盖、前五个原始块的 gold 覆盖分别报告，不能替换文档 Recall@5。
主指标 nDCG@10：按共享 gold 文档连通组配对 bootstrap 10,000 次，四次对照使用 98.75% 区间；
Recall/MRR 和时延区间为探索性统计。

## DEV 质量

| 变体 | nDCG@10 | Recall@5 | Recall@20 | ΔnDCG@10 的 98.75% 区间 |
|---|---:|---:|---:|---|
| baseline | 0.753314 | 0.811578 | 0.876102 | — |
| diversity20 | 0.753686 | 0.810960 | 0.876720 | [-0.001921, 0.003181] |
| budget40 | 0.748953 | 0.808797 | 0.891553 | [-0.013793, 0.005623] |
| combined40 | 0.748641 | 0.810033 | 0.892789 | [-0.015183, 0.006486] |
| budget60 | 0.745428 | 0.806943 | 0.890008 | [-0.018589, 0.003372] |

490 个 gold-overlap 组。多样性的小幅增益没有排除零；扩池让尾部召回更高，前排排序的点估计反而降低，
其区间也包含零，不能据此宣称确定性退化或改进。

全部候选并集实际平均 52.138 块、44.456 篇文档，oracle macro gold coverage 为 0.921323。
这是未排序候选覆盖，不是最终 Recall@20。将全部候选重排后只返回 20 块时，最终 Recall@20 为 0.890008。
可见损失不仅发生在融合截断，后续排序和输出截断同样影响结果。

筛选规则要求 nDCG@10 不低于 baseline 0.002 以上、Recall@5 不低于 baseline 0.005 以上，
并且 nDCG@10 或 Recall@20 至少提高 0.005。没有变体同时达标，
[冻结选择](../../benchmarks/public_rag/results/budget-diversity-20261022/frozen-selection.json) 保留 baseline。
该规则使用点估计做工程选择，不是统计等效性或非劣效性检验。

DEV 的事后损失分析进一步区分两类问题：

- 多样性改变了 179/809 题的候选输入，平均同文档重复块从 2.988 降为 2.734，
  但仅补回 1 个相关 query-document 对。去掉重复候选本身不等于补回缺失的相关文档。
- 40 块方案在重排输入中补回 36 个相关对，但最终首个相关文档的名次在 23 题改善、78 题变差。
  60 块补回 45 个相关对，对应 25 题改善、91 题变差。
- 这些计数使用同一份逐块分数重放，未读取 test 标签，属于解释性诊断，不是新增参数选择门槛。

可追溯计数和 query ID 见
[dev-loss-diagnostics.json](../../benchmarks/public_rag/results/budget-diversity-20261022/dev-loss-diagnostics.json)。

DEV 原始逐块排名、逐题成绩和置信区间见
[dev-quality.json](../../benchmarks/public_rag/results/budget-diversity-20261022/dev-quality.json)；
冷启动和每阶段原始记录保留于同目录 screen-superset 文件。

## 结果边界

本轮是检索实验，不是平台正式发布的 Experiment；没有回答生成、faithfulness、最终 token admission 结论。
实验生成模型 API 调用为 0；GPU 与原生子代理账单未知，不虚构美元或 token 费用。
DEV 复用分数可能受 FP16/batch padding 的小幅差异影响，因此还执行真实路径回归。
观察到的 SciFact 结果不能替代中文、多跳、跨文档或用户真实业务数据评测。

## 真实五臂回归：300 题

| 变体 | nDCG@10 | Recall@5 | Recall@20 | 平均时延 ms | P95 ms | 实际重排块均值 |
|---|---:|---:|---:|---:|---:|---:|
| baseline | 0.748634 | 0.801556 | 0.875111 | 476.12 | 674.34 | 20.000 |
| diversity20 | 0.748311 | 0.801556 | 0.875111 | 468.64 | 650.80 | 20.000 |
| budget40 | 0.751647 | 0.807667 | 0.898556 | 791.16 | 981.07 | 39.983 |
| combined40 | 0.751898 | 0.809333 | 0.901889 | 793.23 | 982.35 | 39.983 |
| budget60 | 0.746682 | 0.807000 | 0.905222 | 994.15 | 1246.49 | 52.360 |

主指标四组 ΔnDCG@10 的 98.75% 区间均包含零。回归中 40/60 块的 Recall@20 有正向探索性区间，
但不能借此推翻已冻结的 DEV 选择；成本分别约增加 66%/109%。多样性将同文档重复块从 879 降至 804，
最终 Recall@20 没有提升。其平均时延差 -7.48 ms 的 95% 配对区间为 [-21.70, 6.96] ms，不能宣称提速。

质量使用全部 300 题（247 个 gold-overlap 组）；时延使用剔除首题后的 299 个配对样本。
每组先运行次数为 baseline 59、diversity20 64、budget40 46、combined40 51、budget60 79；
完整顺序分层与实际各阶段耗时见 [regression-report.json](../../benchmarks/public_rag/results/budget-diversity-20261022/regression-report.json)。
计时期间有两次短时 CPU DEV 诊断（约 4.6/3.6 秒）、文档编辑和源码读取，无并发 GPU/DB 实验或回归测试。
没有根据耗时剔除任何题；因此时延只作本机探索性成本比较，不作生产 SLO 承诺。

四个持久作业均 exit_code=0：DEV 推理 809.703 秒、DEV 评分 68.063 秒、真实回归 1079.625 秒、回归评分 61.703 秒。
测量源码保留于 measured-source.zip，评分源码保留于 scoring-source.zip；测量后 publish.py 做了纯格式调整。
相关 API/候选/runner 主进程测试 38 项通过，快照/隔离/生命周期/session 回归 21 项通过，
子代理配置相关套件 75 项和评分 fixture 16 项通过（相关套件存在重叠，不相加宣称总量）。
Ruff check 与 git diff --check 通过，未新增哈希测试。

[本轮验收记录](../../benchmarks/public_rag/results/budget-diversity-20261022/acceptance.json) 已落盘。
用户追加持续优化要求后，下一轮转向 BGE 官方 learned sparse 机制验证，仍不原地改变旧索引。
