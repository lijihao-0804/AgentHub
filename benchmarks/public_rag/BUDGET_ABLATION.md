# 融合池、重排预算与来源多样性消融

本轮只比较 `results/budget-diversity-20261022/protocol.json` 中预先声明的五组配置。
复用完整 SciFact 语料和既有快照，不重建索引，不改变 dense/sparse 各 30、RRF k=60、模型或切块。
SciFact test 已经暴露，作为回归集；本轮没有新的盲测结论。

## 生产行为

`candidate_top_k` 控制融合池；`rerank_top_k` 控制送入交叉编码器的最大块数；
`final_top_k` 控制返回块数。`rerank_top_k=null` 继承融合池预算，保持旧配置行为。
`max_chunks_per_document=null` 禁用多样性选择，正整数开启文档软配额。

多样性选择在数据库验证 workspace、knowledge base、snapshot 成员后进行，使用实际数据库来源，
优先保留每篇文档配额内的块，候选不足时按原顺序补回其他块，最后恢复原融合顺序。
配额是软约束，不会因知识库只有一篇文档而故意丢弃其多段证据，也不是语义向量 MMR。
只影响 `HYBRID_RERANK` 的重排输入；`DENSE`、`HYBRID` 行为不变。

新参数可从 Agent 发布的 retrieval_config 和知识库 playground API 设置。
旧的已发布版本、默认参数不自动启用新策略。实际重排输入在无文本的 `selection` trace 中可见。

示例（参数示例，不代表本轮胜出配置）：

```json
{
  "dense_top_k": 30,
  "sparse_top_k": 30,
  "candidate_top_k": 60,
  "rerank_top_k": 20,
  "max_chunks_per_document": 2,
  "final_top_k": 6
}
```

## 运行与评分

```powershell
.venv/Scripts/python.exe -m benchmarks.public_rag.budget_ablation screen --protocol benchmarks/public_rag/results/budget-diversity-20261022/protocol.json --state .scratch/rag-opt-20261021/scifact-state.json --output benchmarks/public_rag/results/budget-diversity-20261022
.venv/Scripts/python.exe -m benchmarks.public_rag.budget_report screen --input benchmarks/public_rag/results/budget-diversity-20261022 --output benchmarks/public_rag/results/budget-diversity-20261022/dev-quality.json --protocol benchmarks/public_rag/results/budget-diversity-20261022/protocol.json
.venv/Scripts/python.exe -m benchmarks.public_rag.budget_ablation regression --protocol benchmarks/public_rag/results/budget-diversity-20261022/protocol.json --state .scratch/rag-opt-20261021/scifact-state.json --output benchmarks/public_rag/results/budget-diversity-20261022 --frozen benchmarks/public_rag/results/budget-diversity-20261022/frozen-selection.json
.venv/Scripts/python.exe -m benchmarks.public_rag.budget_report regression --input benchmarks/public_rag/results/budget-diversity-20261022 --output benchmarks/public_rag/results/budget-diversity-20261022/regression-report.json --protocol benchmarks/public_rag/results/budget-diversity-20261022/protocol.json --frozen benchmarks/public_rag/results/budget-diversity-20261022/frozen-selection.json
```

长任务使用项目 `.scratch/rag-budget-20261022/` 下的 job spec，通过本机 `job_runner.py` 前台执行并保存日志。
执行前需完成显式数据库迁移和已有 SciFact prepare；runner 不初始化 schema。
文件存在时拒绝覆盖历史运行，不进行额外哈希测试。

DEV 的 superset 每块只重排一次，使用独立 query/text 交叉编码器分数重放子集。
此路径只筛选质量，不能估算真实子集时延；FP16 与 batch padding 还可能产生小幅分数差异。
回归阶段逐题随机执行五组真实检索路径，模型共享预热、并发为 1，首题所有组均剔除于时延统计。
同一轮真实运行的 baseline 是配对对照，不跨天拿历史耗时作对照。

所有组最终返回最多 20 个原始块，再按 source document 去重计算 Recall/MRR/nDCG。
不能将“前五个原始块的 gold 覆盖”冒称为去重后的文档 Recall@5。
候选池覆盖也不能冒称为最终 Recall@20。报告保留实际选入重排的块数和文档数。
主指标 nDCG@10 对四次 baseline 比较使用 98.75% Bonferroni 区间，按共享 gold 文档连通组进行
10,000 次配对 bootstrap；其它指标和时延区间均为探索性统计。

这不是平台正式发布的 Experiment，没有回答生成、faithfulness、最终 token admission 或 API 美元成本结论。
