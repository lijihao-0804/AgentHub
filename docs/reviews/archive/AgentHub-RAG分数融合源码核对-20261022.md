# AgentHub RAG：分数融合的项目源码核对

本记录是父文档实验运行期间的独立源码研究。没有运行新的融合评分、访问新数据标签或并发 GPU/DB 实验。

Haystack main 在读取时对应提交 `6cf9c4efd573e93e16067eb4d3f755b485f3b3f2`，已按该版本再次核对实现。

## 为什么值得作为后续机制

已有 DEV 结果显示：learned sparse 的单路质量改善没有转化为融合后的净收益；
扩大候选池补回相关文档，也没有稳定改善前列排序。因此可以检验融合机制保留分数强弱的作用，
而不是继续扫描 TopK 或增加模型规模。应在当前里程碑结束后另立协议，一次只改一个机制。

## 实际代码差异

| 项目实际实现 | 已核对的行为 | 移植时需要防止的误解 |
|---|---|---|
| [Haystack DocumentJoiner](https://github.com/deepset-ai/haystack/blob/main/haystack/components/joiners/document_joiner.py) 与[测试](https://github.com/deepset-ai/haystack/blob/main/test/components/joiners/test_document_joiner.py) | `distribution_based_rank_fusion` 按每路均值与三倍总体标准差归一化，最终调用 `_concatenate`，相同 ID 取最高分。常量列表保留原分数。测试覆盖常量、单文档和空分数。 | 名称中的 fusion 不等于两路相加，也不是 RRF；不能凭文档标题推测其行为。这里读取的是 main，当作源码研究，正式复现仍需冻结具体版本。 |
| [Qdrant score_fusion.rs](https://github.com/qdrant/qdrant/blob/222b2e44ab1c3f17e8246396ead6558e45041df7/lib/segment/src/common/score_fusion.rs) | `ScoreFusion::dbsf()` 使用 `Aggregation::Sum`、`Normalization::Distr`、每路默认权重 1。Welford 计算样本方差，按均值 ±3σ 归一化，再将相同 point 的得分相加。单元素或零方差列表给 0.5。文件内性质测试核对样本方差。 | 与 Haystack 的最大值合并、总体方差和常量处理均有区别；不能混为同一算法。归一化公式没有额外裁剪，不能擅自添加 `[0,1]` clip。 |
| [Dify WeightRerankRunner](https://github.com/langgenius/dify/blob/7275495aa79fb7035425051609669ee4f27b56ce/api/core/rag/rerank/weight_rerank.py) 与[实际工厂](https://github.com/langgenius/dify/blob/7275495aa79fb7035425051609669ee4f27b56ce/api/core/rag/rerank/rerank_factory.py) | 支持先去重，再按向量与关键词分数加权。关键词函数注释称 BM25，但实现是候选集 IDF 加 TF-IDF 余弦；向量分数可能复用 metadata。 | 不能把注释当成 BM25 实现，也不能随意把任何 metadata.score 当成 dense 分数。 |
| [RAGFlow search.py](https://github.com/infiniflow/ragflow/blob/392ec99651da78f6a8e1a2f60fe4818a95a8651d/rag/nlp/search.py) 与[query.py](https://github.com/infiniflow/ragflow/blob/392ec99651da78f6a8e1a2f60fe4818a95a8651d/rag/nlp/query.py) | `rerank_by_model` 组合词项覆盖率与重排模型分数；token similarity 包含单词与相邻组合词权重。真实 `retrieval` 调用会覆盖函数形式参数的默认权重。 | 不能只读取函数签名就宣称部署默认；移植要核对实际调用链、分数范围和 tokenizer。 |

## 有边界的后续方案

优先考虑固定的 Qdrant DBSF 机制：相同 dense/sparse top-30、相同原索引、相同 top-20 神经重排，
只替换 RRF 的候选融合公式，不调整任何权重。先用之前完整候选并集的神经分数缓存筛查质量，
明确缓存筛查不提供时延证据；只有通过固定质量门槛才执行真实配对重检索。

这只是待立项方案，不是已完成的优化结果。不要将此表中优秀项目自己的效果声明移作 AgentHub 分数。
