# SciFact 的 20 块候选池能找回多少标注来源

在 553 道确认题上，独立字词 BM25 补位使 20 个候选块的文档召回率从 **86.46% 提高到 87.72%**。原先命中的 539 个查询–金标文档对全部保留，新找到 8 对，其中 5 对的文档在原稠密、稀疏两路前 30 条中都不存在。只用原两路候选补位找回 3 对，增幅低于预设门槛。[确认汇总](confirmation.json)

**原 BM25 补位方案未通过交付验收。** 用真实 BGE 分数重排后，能在前 5 块完整看到官方证据句的查询–文档对从 342 降至 339。改变候选构成时，同一篇论文中最有用的段落可以被替换。下文单独记录了这种情形及一次保留段落的后续试验。[重排报告](rerank-report.json) · [完整官方证据句审计](rationale-visibility.json)

两种补位法都保留融合前 8 块及前 20 块中每篇论文首次出现的块，只替换靠后的重复来源块，每题最多换 4 块。查询、原语料和送重排的 20 块预算相同。两路补位读 30+30 条候选；BM25 补位额外读独立索引的前 20 条。后者需要索引、查询计算和在线时延预算。[冻结协议](protocol.json)

## 开发与确认

原抽样按预先登记的盐和查询 ID 排序，从 SciFact 官方 TRAIN 的 809 题取 128 题开发、4 题预热。另有 124 道题通过一个或多个共同金标文档与这 132 题相连，一并排除。确认使用剩余全部 553 题、367 个来源连通分量。切分程序只用正例分量确定成员；方案、门槛和确认题顺序都在查看本轮确认表现之前冻结。[顺序指纹与分母](cohort-freeze.json) · [开发结果锁定的两项提名](selection.json)

原 128 题有 13 题完全没有标注论文进入候选池，涉及 14 个正例对。其中 7 题已有来源进入至少一路前 30，随后被 RRF 截断；另 6 题的来源在两路前 30 均缺失。第 753 题的同一文档在不同检索路线中命中不同段落，RRF 以块为单位计算，两个段的贡献分开。13 题中的 11 题只有引用文档、没有官方证据句。[原 13 题逐题、逐块与反事实核验](FORENSICS.md) · [源名次与证据位置数据](forensics-cases.json)

| 原 128 题比较的方案 | 来源宏召回 | 新增／丢失金标对 | 从全失恢复／从命中变全失 | 融合前 5 原始块召回变化 |
|---|---:|---:|---:|---:|
| 生产 RRF，k=60 | 89.0625% | — | — | — |
| 原双路补位，最多 4 块 | 89.4531% | 1／0 | 1／0 | 0.0000 个百分点 |
| 每篇论文先取一个块 | 89.4531% | 1／0 | 1／0 | +0.3906 个百分点 |
| 强调头部的 RRF，单基 k=1 | 89.0625% | 0／0 | 0／0 | +0.5859 个百分点 |
| Qdrant DBSF | 89.4531% | 1／0 | 1／0 | +1.1719 个百分点 |
| 三路等预算 RRF，每路 20 | 89.2578% | 2／1 | 1／1 | −1.9531 个百分点 |
| 独立 BM25 补位，最多 4 块 | 89.2578% | 1／0 | 0／0 | 0.0000 个百分点 |

每个家族只提名一个方案。开发要求新增至少 1 个金标对、全部原正例保留，且融合前 5 块的宏召回降幅不超过 0.5 个百分点。同分时依次看来源宏召回增益、恢复全失查询数、预先固定的方案顺序。双路三种方案并列，固定顺序使保留原有来源的补位法进入确认。三路 RRF 虽新增 2 对，也失去了 1 对，故止于开发。[全部开发指标](development.json)

| 确认指标 | 原双路 RRF | 原双路补位 | 独立 BM25 补位 |
|---|---:|---:|---:|
| 金标来源宏召回 | 86.4557% | 86.9078% | 87.7215% |
| 差值及 97.5% 区间，百分点 | — | +0.4521 [0, +1.1546] | +1.2658 [+0.3591, +2.4299] |
| 命中金标对／全部 | 539／625 | 542／625 | 547／625 |
| 原已命中金标对保留／新增／丢失 | — | 539／3／0 | 539／8／0 |
| 至少命中一篇标注来源的题数 | 485／553 | 487／553 | 491／553 |
| 完全找齐标注来源的题数 | 470／553 | 473／553 | 478／553 |
| 由完全未命中恢复 | — | 2／68 | 6／68 |
| 仍完全未命中 | 68 | 66 | 62 |
| 多金标题的命中来源对数 | 99／118 | 100／118 | 101／118 |
| 原两路并集以外的新金标对 | — | 0 | 5 |

宏召回先对每个查询计算“找回的不同正例论文数／该查询的正例论文数”，再按查询等权平均。46 道多金标题共 118 个正例，BM25 额外补齐 2 个正例，找齐所有来源的多金标题从 30 道增至 32 道。两个方案都保留了基线命中任意正例的全部 485 道题。每个区间使用 10,000 次按共享正例文档分组的配对重采样。为同时判断两个预注册家族，各用 97.5% 区间，最低实际增幅为 0.5 个百分点，区间下限须严格大于零。[区间、逐类分母与五项门槛](confirmation.json)

## 历史证据与适用范围

十月的父文重排、检索预算、学习式稀疏、DBSF 和 Onyx 试验早已评估全部 809 道公开 TRAIN。本轮的确认与原 128 题按来源隔离，但仍使用有项目开发历史的数据。官方 test 标签和平台正式 HOLDOUT 均未用于本轮。SciFact 文档 qrels 能检验来源是否在候选内；同论文另一个块能否支撑答案，还需核验实际交付文本。该确认只判定候选层质量。[既往预算试验](../budget-diversity-20261022/protocol.json) · [既往父文重排](../parent-context-20261022/protocol.json) · [既往稀疏检索](../learned-sparse-20261022/protocol.json)

BM25 使用相同快照全部 5,183 篇论文的 9,189 个块，Unicode NFKC、casefold 和字母数字分词。倒排表按文档块计算词频、篇幅与全库文档频率。历史 `chunk_sources` 保留原切块写入顺序；4,095 篇文献可从旧审计文件用原修订号逐块复核哈希，剩余 1,088 篇依据有指纹的快照顺序及逐篇块数重建。词法通道的全文检索会改变总成本，本表没有在线延迟、GPU 开销或答案正确率的结论。[取数与重建代码](../../../candidate_recall_data.py) · [BM25 与候选策略](../../../candidate_recall_methods.py)

项目原始实现分别提供不同的可检验选择。Qdrant 使用列表内的均值与样本标准差实现 DBSF，并提供从第 0 位起算的头部 RRF。OpenSearch 说明了多列表 RRF 及可调常数。Vespa 的分组查询能按父字段返回代表结果，本实验把该思路改为离线单来源先取一块。BM25 的词频饱和、长度归一参数及 IDF 采用 Lucene 的公开默认定义，查询分析器和内存索引为本实验实现。[Qdrant DBSF 源码](https://github.com/qdrant/qdrant/blob/222b2e44ab1c3f17e8246396ead6558e45041df7/lib/segment/src/common/score_fusion.rs) · [Qdrant RRF](https://qdrant.tech/documentation/search/hybrid-queries/) · [OpenSearch 排名融合](https://docs.opensearch.org/latest/search-plugins/search-pipelines/score-ranker-processor/) · [Vespa 分组](https://docs.vespa.ai/en/querying/grouping.html) · [Lucene BM25](https://lucene.apache.org/core/7_6_0/core/org/apache/lucene/search/similarities/BM25Similarity.html)

## 真实 BGE 重排与证据句

在查看本轮重排结果之前，单独冻结了两家族、128／553 题、缓存核对规则和非劣门槛。确认题 BM25 臂的 11,060 个输入对中，10,077 对直接使用原基线 logits，501 对来自同题同块的历史双路并集，482 对为本次真实 GPU 补分。连同开发集，共补齐 585 个此前未评分的对。对冻结顺序中的 80 题再次按实际两臂的完整 20 块和原顺序分别调用 BGE，两臂的前 5 块集合在 80／80 题分别一致。基线 1,600 个可比 logits 全等；BM25 臂可比较的 1,541 对中 1,517 对全等，最大绝对差 0.00390625。包括该审计在内，实际前向共处理 3,726 对，监督进程耗时 97.141 秒，退出码 0。[下游冻结协议](rerank-protocol.json) · [运行记录](rerank-execution.json)

| 553 题的重排结果 | 原双路 RRF | 独立 BM25 补位 | 配对差值的 97.5% 区间 |
|---|---:|---:|---:|
| 原始前 5 块来源宏召回 | 0.794575 | 0.796835 | [−0.016294, +0.017086] |
| 文档 nDCG@10 | 0.744134 | 0.745399 | [−0.008709, +0.011203] |
| 文档 Recall@5 | 0.803165 | 0.803165 | [−0.018183, +0.014520] |
| 文档 MRR@5 | 0.712055 | 0.715220 | [−0.006289, +0.012788] |

原始前 5 的 482 个金标对中保留了 477 个，新进 9 个、挤出 5 个。按问题数，5 道从全失恢复，另有 5 道变成全失，至少命中一道来源的问题仍为 452。候选池新获的 8 对只有 4 对进入前 5。原始前 5 宏召回和 nDCG@10 的配对区间下界均低于预登记的 −0.005。18 个基线命中来源失去了原来得分最高的段落，5 个掉出前 5 的来源都发生了这种替换。第 254 题的标注来源 37424881 原最佳块得分 1.81836、排第 4，替换后同来源最佳块得分 −0.15967、排第 6。[两臂全指标及冻结顺序首例](rerank-report.json)

341 道确认题含官方支持或反驳句，覆盖 379 个查询–文档对。另外 246 个 BEIR 正例对没有句级标注。按原始句子在 1,200 字符块中的精确范围计分，只要任一可替代证据组的每个句子完整出现即可；不同句可由不同返回块提供。BM25 20 池中完整的证据对从 357 降至 348（新增 0、失去 9）。经过 BGE 取前 5 后，从 342 降至 339（新增 3、失去 6），至少有一套完整证据的问题从 319 降至 316。原来 128 道开发题的前 5 完整证据对也从 81 降至 76。确认池新取得的 8 个来源对均没有官方句级证据。[官方句级计数与对照](rationale-visibility.json) · [AllenAI 数据说明](https://github.com/allenai/scifact/blob/master/doc/data.md)

第 1159 题说明来源指标与证据指标为何分离。标注来源 12584053 的官方支持句 13 位于字符区间 [1799, 2025)。基线的第 1 块覆盖 [1000, 2086)，第 2 块覆盖 [0, 1200)。BM25 补位后，同一来源仍排第 1，却只剩 [0, 1200) 的首块，支持句不可见。第 254 题则在交付边界上表现为原最佳块离池后同来源降至第 6。[句范围对齐与重放](../../../candidate_recall_rationales.py)

## 保护高分段落的后续探索

在上述 553 题和句级结果已经揭示后，只试验了一个预先写明的配置。对原 20 块先做 BGE，保护融合前 8、每篇首次出现的块、基线 BGE 前 10 及每篇最高分块，再从其余重复段替换最多 4 块。该配置对原 128 题、同一 553 题和原 13 道候选全失题使用完全相同的规则。候选的新增块一定来自先前已实算的同一 BM25 新来源前缀，全部评分可复用。[后续开发协议](protected-followup-protocol.json) · [独立选择函数](../../../candidate_recall_protected.py)

| 已揭示的 553 道题 | 基线 | 原 BM25 补位 | 保护高分块后补位 |
|---|---:|---:|---:|
| 候选池命中的金标对 | 539 | 547 | 542 |
| 完整句证据对，20 池 | 357 | 348 | 356 |
| 完整句证据对，重排前 5 | 342 | 339 | 342 |
| 失去原文档最优块的金标对 | 0 | 18 | 0 |
| 重排前 5 来源对，新增／丢失 | — | 9／5 | 1／1 |
| 正常执行需 BGE 评分对数 | 11,060 | 11,060 | 11,443 |

保护方法在 553 题仅多找到 3 个候选金标对，宏变化 +0.003617，描述性 97.5% 区间 [0, +0.010715]。重排前 5 宏召回、文档 Recall@5 和全部已交付完整证据组持平；20 池仍少 1 个完整证据对。266／553 题需要第二次 BGE 调用，增加 383 个推理对。原 128 题前 5 的金标和完整证据组均原样保留，但原 13 个候选全失题仍为 0／13。该次探索未确立稳定的最终交付收益。[含原 13 题分母及全部指标的结果](protected-followup.json)

## NFCorpus 跨语料结果

在独立于 SciFact 的 128 道 NFCorpus 官方 TRAIN 题上，基线 20 池宏召回为 0.113534。原双路补位为 0.118755（新增 30 个正例对、丢失 0），原 BM25 补位为 0.121842（新增 32、丢失 0）。基线至少命中来源的 85 题均保留，词法臂另恢复 1 道。两者的候选层门槛均通过。[跨语料抽样协议](nfcorpus-protocol.json) · [双路补位](nfcorpus-safe_refill4.json) · [独立 BM25 补位](nfcorpus-lexical_refill4.json)

保护高分段落方案使用第二组 128 道 NFCorpus 官方 TRAIN 题，按新盐抽取，排除第一组的查询。20 池宏召回从 18.6980% 增至 19.5529%，新增 10 个正例对、丢失 0 个，命中至少一篇来源的问题从 92 增至 94。BGE 前 5 的宏召回从 13.70921% 增至 13.71160%，新增 2 对、丢失 1 对，命中问题仍为 84 道。实算共 2,613 个 BGE 对，比原 2,560 个多 53。该组通过预先规定的来源门槛；SciFact 原 13 题仍全部缺失，553 题的 20 池还少 1 套完整官方句证据。[第二组抽样与门槛](nfcorpus-protected-protocol.json) · [第二组实测结果](nfcorpus-protected_refill.json) · [跨语料捕获与评分入口](../../../recall_transfer.py)

## 复现

从仓库根目录在新的本地目录依序执行。协议列有公开语料和本地历史快照的精确输入指纹。原逐题检索和新生成的候选列表均留在本机。

```powershell
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_study freeze --output .scratch\candidate-recall-reproduction
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_study development --output .scratch\candidate-recall-reproduction
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_study confirmation --output .scratch\candidate-recall-reproduction
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_rerank --study .scratch\candidate-recall-reproduction --output .scratch\candidate-recall-bge-reproduction
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_rationales --study .scratch\candidate-recall-reproduction --inference .scratch\candidate-recall-bge-reproduction --archive .scratch\scifact-rationale-source-20261114\data.tar.gz --output .scratch\candidate-recall-bge-reproduction\rationales.json
& .\.venv\Scripts\python.exe -m benchmarks.public_rag.candidate_recall_protected --study .scratch\candidate-recall-reproduction --inference .scratch\candidate-recall-bge-reproduction --archive .scratch\scifact-rationale-source-20261114\data.tar.gz --private .scratch\candidate-recall-bge-reproduction\protected-rankings.jsonl --output .scratch\candidate-recall-bge-reproduction\protected.json
& .\.venv\Scripts\python.exe -m pytest tests/unit/test_candidate_recall_study.py -q -p no:cacheprovider
```

初次确认完成了 553 道题的候选计算，输出汇总时，NumPy 布尔值无法序列化导致进程退出码 1。在打开该未完成报告之前，修复了置信区间数值向原生 Python 类型的转换。按原协议以新目录重放，开发六臂统计、提名及两次运行全部候选排名逐字节一致，确认退出码 0。[序列化修复与证据指纹](serialization-replay.json)
