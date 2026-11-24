# AgentHub RAG：Qdrant DBSF 固定机制筛查

根据实际 [Qdrant 源码](https://github.com/qdrant/qdrant/blob/222b2e44ab1c3f17e8246396ead6558e45041df7/lib/segment/src/common/score_fusion.rs)
移植样本方差、均值 ±3σ 归一化与相同 chunk 得分相加，保持每路权重 1，不裁剪、不扫参数。
Python 使用 float64，fusion 同分按 chunk ID 排序；不声称 Rust f32 位级复现。
与 Haystack 同名方法的差异见[源码对照](AgentHub-RAG分数融合源码核对-20261022.md)。

[协议](../../benchmarks/public_rag/results/project-dbsf-20261022/protocol.json)在评分前固定。
使用全部 809 题既有完整候选并集、真实两路分数和已计算的每个候选 BGE 分数，
固定 dense/sparse 30、候选/重排/输出 20。逐题验证 RRF 重建、来源映射与并集全覆盖。
这是缓存质量筛查，不能作为实际时延、API 性能或生产成本证据。

| 指标 | baseline | DBSF |
|---|---:|---:|
| fusion nDCG@10 | 0.713479983 | 0.725329052 |
| 最终 nDCG@10（主） | 0.753313815 | 0.753390744 |
| 最终 Recall@5 | 0.811578080 | 0.809517923 |
| 最终 Recall@20 | 0.876102184 | 0.874042027 |
| 最终 MRR@10 | 0.726304275 | 0.727269606 |

最终主指标差 `+0.000076928`，490 个 gold 连通组配对 bootstrap 10,000 次的
95% 区间 `[-0.002152253, +0.002316748]`，跨零；最小收益 +0.005 与区间门槛均未通过。
两项 Recall 损失上限通过，冻结 baseline。融合排序的局部改善不能代替端到端排序改善。
DEV 已多轮使用，所有这些结论属于探索性筛查。

适配器 29 项与缓存运行器 5 项针对性测试通过，Ruff 通过。
筛查任务 exit_code=0，耗时 16.657 秒；未增加 GPU、test/NFCorpus 或生成模型调用。
原默认不变，不进一步搜索权重、σ 或 TopK。没有给失败的缓存方案追加真实检索时延实验。
实际 trace、结果和验收位于 [结果目录](../../benchmarks/public_rag/results/project-dbsf-20261022/acceptance.json)。
