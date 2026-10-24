# AgentHub RAG：SciFact 首轮检索性能优化

状态：**PASS — 首轮里程碑完成；本集合上检索质量保持一致，时延降低。**

实验始于 2026-10-21，验收完成于 2026-10-22（Asia/Shanghai）。

## 本轮范围与改动

本轮只优化检索候选块的数据库加载，不展开生成、多跳或图检索。
`HybridKnowledgeRetriever._load_chunks` 原先在每题 SQL 中展开快照的全部 5,183 个 revision ID；
现在以 `KnowledgeSnapshotItem` 连接候选 chunk，并限定 snapshot、workspace、knowledge base、document、revision。
Qdrant 原有全量 revision 过滤保留。检索、融合、重排、相关性阈值和发布语义不变。

原计划预留 rrf_k=10，DEV 基线出来后没有执行该参数实验，转为上述 SQL 优化。
这次选择基于 DEV 的时延分解，修订发生在最终 test 性能评估之前。
原始方案和修订保留在[实验协议](../../benchmarks/public_rag/scifact-protocol-20261021.md)。

## 实验方法

| 控制项 | 实际配置 |
| --- | --- |
| 数据 | 官方 BEIR/SciFact，完整 5,183 篇语料；train 809 题，test 300 题 |
| 分块 | 标题 + 两换行 + 摘要；生产 chunker，1,200 字符 / 200 重叠，共 9,189 chunks |
| 模型 | BGE-M3 dense；BGE-reranker-v2-m3；本地既有离线缓存 |
| Sparse | tokenizer log-TF + Qdrant IDF，不是学习型 BGE sparse 或 BM25 |
| 预算 | Dense/Sparse 各 30 chunks，RRF=60，候选和最终输出各 20 chunks，GPU batch=8 |
| 设备 | RTX 3060 Laptop GPU，Torch 2.14.0+cu130，单并发 |
| 隔离 | 独立库 `agenthub_rag_scifact_20261021` 与 collection `rag_scifact_20261021` |
| A/B | 同进程共享模型；每题确定性随机选择两个实现的先后顺序；seed=20261021 |

原始 trace 保留 30 个 Dense/Sparse 命中。策略质量投影统一截取前 20 chunks，然后按上游文档 ID 去重并计算文档排名。
Dense/Hybrid 是完整链路相应阶段的质量投影，不是独立端到端时延实验。20 chunks 平均不足 20 篇独立文档，重复率另列。

Recall 为命中正例文档数 / 该题全部正例数；MRR 取首个正例倒数；nDCG gain 为 `2^rel - 1`，折损为 `log2(rank+1)`，按理想排序归一化。
主质量指标为 nDCG@10，另列 Recall@5/10/20、MRR@5/10 和 nDCG@5/20。无正例/未标注题跳过，有 gold 但无结果为 0；缺失和逐题结果保留。

主要性能估计为同题 optimized-baseline 的热端到端均值差，按共享正例文档连通组做 10,000 次 bootstrap，报告百分位 95% CI。
首题推理单列、从热时延排除；另列 p50/p95。质量 CI 的次要指标仅作探索性描述，不作多重显著性承诺。

## DEV 定位证据

首次旧实现完整 DEV：nDCG@10=0.753314，Recall@5=0.811578、Recall@10=0.857973、Recall@20=0.876102、MRR@10=0.726304。
热端到端均值 464.068 ms、p50 429.621 ms、p95 657.864 ms。Dense/Sparse/Fusion/Rerank 阶段均值分别为 53.689/29.368/0.158/274.680 ms，
剩余约 106.174 ms 包含 DB 快照/候选读取等开销，不能全部归因于被修改的候选查询；成对实验进一步直接计时这两个 DB 步骤。

Fusion 总体 nDCG@10=0.713480，略高于 Dense 的 0.710425；有 19 题融合后的 Recall@20 低于 Dense，重排使 30 题 Recall@5 下降。
这些质量损失保留为诊断，不借本轮 SQL 优化宣称准确率提升。

开发集成对运行与短时数据库回归测试有重叠，DEV 时延仅作探索性参考。最终 test 成对运行时不并行运行测试或其他由本任务启动的 GPU/DB 工作。

完整 DEV 成对运行退出码 0。809 题四阶段 chunk 排名完全一致，质量主次指标差值均为 0。
质量 bootstrap 共 490 个正例来源连通组；热时延排除首题后为 808 题 / 489 组。

| DEV 探索性时延 | 旧 SQL | 快照成员连接 |
| --- | ---: | ---: |
| 端到端均值 | 451.459 ms | 408.630 ms |
| 热 p50 | 419.201 ms | 381.229 ms |
| 热 p95 | 630.548 ms | 594.553 ms |
| 候选块加载均值 | 47.404 ms | 5.761 ms |

端到端成对均值差 -42.829 ms，95% CI [-51.011, -34.545] ms；
候选块加载差 -41.642 ms，95% CI [-44.017, -39.565] ms。被修改步骤的降耗与完整链路差值接近。
详细证据见 [DEV 质量](../../benchmarks/public_rag/results/scifact-20261021/dev-quality.json)和
[DEV 成对性能](../../benchmarks/public_rag/results/scifact-20261021/dev-performance.json)。

## 最终结果

完整 test 300 题、有效评分 300 题，无缺失排名、无未标注/无正例跳过。两个实现四阶段的全部 chunk 排序与分数完全一致；8 项质量指标点估计差均为 0，已计算的 4 项成对质量 95% CI 均为 [0, 0]。质量按 247 个正例来源组抽样，热时延排除首题后为 299 题 / 246 组。

| 最终 test 指标 | 旧 SQL | 快照成员连接 |
| --- | ---: | ---: |
| nDCG@10 | 0.748634 | 0.748634 |
| Recall@5 | 0.801556 | 0.801556 |
| Recall@10 | 0.861778 | 0.861778 |
| Recall@20 | 0.875111 | 0.875111 |
| MRR@5 | 0.709611 | 0.709611 |
| MRR@10 | 0.717460 | 0.717460 |
| 端到端均值 | 488.223 ms | 439.650 ms |
| 热 p50 | 466.828 ms | 423.931 ms |
| 热 p95 | 658.609 ms | 600.312 ms |
| 候选块加载均值 | 51.754 ms | 7.329 ms |

端到端成对均值差 **-48.573 ms**，95% CI **[-60.356, -37.384] ms**；均值降低 **9.95%**，p95 点估计降低 **8.85%**。候选块加载差 -44.426 ms，95% CI [-48.534, -40.653] ms。

执行顺序分层：baseline_first: n=137，平均差 -49.900 ms；optimized_first: n=162，平均差 -47.450 ms。
按运行时间四等分的平均差分别为 -31.650/-71.539/-49.791/-41.086 ms。

结果支持固定质量和预算下，本集合在本机上的检索加载提速；不宣称准确率提高。最终 test 通过既定验收条件，本轮停止，不继续调参。

[最终质量与逐题诊断](../../benchmarks/public_rag/results/scifact-20261021/test-quality.json)、[成对性能与分层诊断](../../benchmarks/public_rag/results/scifact-20261021/test-performance.json)、[冻结选择](../../benchmarks/public_rag/results/scifact-20261021/frozen-selection.json)、[机器可读验收](../../benchmarks/public_rag/results/scifact-20261021/acceptance.json)。

最终 test 各策略的等预算文档级质量：

| 阶段投影 | Recall@5 | Recall@10 | Recall@20 | MRR@10 | nDCG@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| dense | 0.739611 | 0.796778 | 0.835222 | 0.615989 | 0.653778 |
| fusion | 0.764667 | 0.844444 | 0.875111 | 0.643033 | 0.689169 |
| rerank | 0.801556 | 0.861778 | 0.875111 | 0.717460 | 0.748634 |

本轮四个长期作业实际退出码均为 0；质量与时延后处理也均为 0。

| 作业 | 退出码 | 墙钟秒数 |
| --- | ---: | ---: |
| prepare-run-1 | 0 | 155.453 |
| dev60-run-1 | 0 | 400.593 |
| dev-paired-run-1 | 0 | 719.454 |
| test-paired-run-1 | 0 | 300.875 |

## 验证与审计

- 数据适配 9 项、评分 6 项、报告诊断 9 项、冻结访问 2 项、成对时延计算 2 项，分别通过针对性测试。
- 相关生产回归 16 项通过：真实 PostgreSQL/Qdrant、快照外候选、跨工作区、已退役历史修订、策略、重排失败、相关性阈值与替代文档。
- 原生 DeepSeek 承担适配、评分、复核和 SQL 实现；主代理集成、修正并运行真实实验。没有将实验 API 用于开发委派。
- 未新增哈希算法测试或重复完整性盘点；保留并复用既有生产快照校验，每个成对进程启动时解析一次。
- 早期适配/复核曾读取 test 结构与总量，未使用 test 检索分数或逐题成绩调参；该事实已补记 `SCHEMA_ONLY_TEST_READ_REPORTED_POSTHOC`，不宣称严格盲测。
- 最终 runner 在读取 test 标签前持久记录暴露和完整冻结配置；后处理评分/时延分析也记录标签访问。
- 实验模型 API 调用 0 次。原生子代理平台费用、GPU/电力及服务成本未知；没有生成答案或 LLM judge 费用估计。

## 证据入口与复现边界

- [原始 trace、冻结配置和结果目录](../../benchmarks/public_rag/results/scifact-20261021/)
- [冻结时的 15 文件源码快照](../../benchmarks/public_rag/results/scifact-20261021/source-20261021.zip)
- [生产 SQL 补丁](../../benchmarks/public_rag/results/scifact-20261021/production-sql-change.patch)
- [运行命令与模块入口](../../benchmarks/public_rag/README.md)

原始 prepare、首次 DEV、成对 DEV 和最终 test 的 stdout/stderr、状态与实际退出码保存在本地 `.scratch/rag-opt-20261021/`。
初始 DEV 使用人工种子上下文；正式成对运行前为独立种子补充 owner 组织成员记录，随后由 TenantService 获取真实权限上下文并解析同一快照。
环境关键字段已显式固定，原始准备环境、初次 DEV 元数据与独立 collection 点数一致后才补充状态控制字段，不重建索引或修改排序。

本结果衡量文档检索而非证据片段蕴含、答案正确率或事实一致性；不与不同分块/预算的 BEIR 榜单直接排名。
时延是检索服务调用的端到端时间，不含 HTTP、生成模型或完整 Agent 回合。进程模型加载与首次推理分开记录；操作系统文件缓存已热，不代表磁盘冷启动。
单会话 CI 反映查询/来源混合的不确定性，不覆盖跨硬件、重复会话、系统饱和吞吐或生产收益。数据库初始完整 scope 读取和 Qdrant 大过滤仍存在。
源码包不包含模型权重；复现需保留本机所用的同一离线模型缓存版本。

最终收尾对三个文件执行 Ruff 格式化，没有语义改动；冻结源码包保留实测版本。Lint、13 个相关文件的格式检查和 diff 空白检查通过。
