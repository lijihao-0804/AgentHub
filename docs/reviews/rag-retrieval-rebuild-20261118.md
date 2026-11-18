# SciFact 全库重建与 QASPER 历史召回复测

在 553 道相同 SciFact 题上，固定 20 块的 BM25 补位将候选命中来源从 539 增至 547 对，重排前五的完整证据却从 342 降至 339 对。保留原 20 块再追加 BM25，可保住原先的 342 对完整证据，新增 0 对。把同样数量的额外名额给原双路 RRF，前五多获得 1 对完整证据。四种方案都从刚重建的全库索引检索，并在本机重新打分。

## 材料与比较范围

| 数据 | 已有对照 | 本轮用途 |
|---|---|---|
| SciFact 英文科学摘要 | 5,183 篇论文，历史双路融合、BM25 补位与公开的逐题句级评分 | 从原文重新生成向量，按冻结的 553 道 TRAIN 题比较候选与重排 |
| QASPER 英文论文 | 32 篇文章，72 道可以定位完整标注段落的 TRAIN 题 | 从原文重建三种切块和单篇文章 BM25，复核逐题得失 |
| NFCorpus 与既有跨版本问答 | NFCorpus 标注相关来源，跨版本问答有两名内部 AI 评阅者的盲评记录 | 查阅历史结果 |

SciFact 目录保留了历史查询、文档、冻结分块、逐块排序与官方句标。可以分别追踪「相关论文出现在结果里」和「返回的原文包含一整组依据」。QASPER 的查询以已知论文为范围，可以单独检验切块策略。[历史召回研究](candidate-recall-20261114.md) · [QASPER 历史对照](document-ingestion-and-delegation-20261117.md) · [跨版本问答](rag-crossversion-r2-result-20261115.md)

## SciFact 现场重建

代码基线为 `8ba285beb041892b09091b747976665545c281be`。本机从同一份 BEIR 和官方 TRAIN 输入恢复 5,183 篇原文、9,189 个 1,200 字符分块（重叠 200 字符）和历史块标识。离线 BGE-M3 重新编码全部块与 809 个训练查询，并重建基于该模型分词器的稀疏词频向量。历史构建脚本还会重建 5,183 个整篇向量。分块、全文、查询及其清单合计 8 份文件与历史文件的 SHA-256 全部一致。本轮检索使用分块索引。[输入与模型指纹](../../benchmarks/public_rag/results/published/candidate-recall-20261114/forensics-summary.json)

553 道查询沿用 11 月 14 日的有序确认分区，覆盖 367 个共享正例来源的连通分量。每题在全库计算稠密余弦和稀疏 IDF 分数，各取前 30 块，再以生产 RRF 实现融合，常数为 60。独立 BM25 从同一批 9,189 个块建索引。四种候选表全部送入本地 BGE-reranker-v2-m3，依各自原顺序、每批 8 对、FP16 CUDA 打分。只有查询及该查询的整份候选序列都相同时复用此次已算分数。[本轮固定协议](../../benchmarks/public_rag/experiments/scifact_retrieval_rebuild/protocol.json) · [检索与证据计分](../../benchmarks/public_rag/experiments/scifact_retrieval_rebuild/replay.py)

历史补位规则每题选入 0–4 个 BM25 新来源，记作 *m*。固定 20 块时，BM25 用这 *m* 个新来源替换同一篇论文的靠后重复块。另用两种等深度对照保留原 20 块，一种按 RRF 次序续取 *m* 块，一种追加相同的 *m* 个 BM25 块。每题比较两种追加方式时，实际交给重排器的块数相等。这两种追加方式在本轮查看逐题结果前固定。项目过去已多次使用这个 TRAIN 分区。

官方来源指标把一道题与一篇相关论文计为一对，553 题共有 625 个正例对。其中 341 题、379 对另有官方证据句，参与完整证据评分。其余 246 对只评价来源。一组备选依据里的每句话都须完整落在该论文的某个返回块内，同一组不同句可以跨块。重排前 5 取最前面的五个**实际块**，再统计其中的不同来源与完整依据。

### 四种候选的现场结果

| 方案 | 候选命中来源 / 625 | 前五命中来源 / 625 | 候选含完整证据 / 379 | 前五含完整证据 / 379 | 前五有完整证据的题 / 341 |
|---|---:|---:|---:|---:|---:|
| 双路 RRF，20 块 | 539 | 482 | 357 | 342 | 319 |
| BM25 替换末尾重复来源，20 块 | 547 | 486 | 348 | 339 | 316 |
| 双路 RRF，追加 *m* 块 | 540 | 481 | 358 | 343 | 320 |
| 保留原 20 块，追加 *m* 个 BM25 块 | 547 | 483 | 357 | 342 | 319 |

固定 20 块时，按题等权计算的候选来源宏召回从 86.46% 升至 87.72%。BM25 补位新找回 8 个题目–来源对，候选层原有正例丢失 0 对。进入前五后，来源新增 9 对、丢失 5 对，5 道题从零命中变为有命中，另有 5 道反向变化。前五完整证据新增 3 对、丢失 6 对。第 1159 题的来源 `12584053` 仍在前五，包含完整标注证据的块在补位时已被移出候选池。同一题也可能有得有失，第 973 题的前五获得来源 `28617573` 的完整依据，同时失去来源 `27446873` 的完整依据。[逐题配对与全部退步题](../../benchmarks/public_rag/experiments/scifact_retrieval_rebuild/results/summary.json)

两种追加方案的额外重排量都是 983 个查询–块对，较固定深度的 11,060 对增加 8.89%。99 题追加 0 块，分别有 151、141、98、64 题追加 1、2、3、4 块。保留原块再追加 BM25，候选与前五完整证据逐对持平，前五相关来源相对基线新增 4 对、丢失 3 对。等深度的 RRF 续取使第 246 题在 22 个候选及重排前五都获得来源 `3430789` 的完整证据。这篇论文本就在基线前五，新增的是含标注句的块。BM25 的两个追加名额带来了另一篇相关论文 `8447873`，这篇论文没有该题的官方句标。普通 RRF 扩容也有退步，第 423 题的相关来源 `8595678` 从前五掉出。[553 题脱敏明细](../../benchmarks/public_rag/experiments/scifact_retrieval_rebuild/results/questions.jsonl)

全库索引重建耗时 312.235 秒，检索、四臂重排与评分共耗时 852.891 秒，两步退出码均为 0。固定两臂各有 11,060 个待打分对，扩容两臂各有 12,043 对。对同题的完全相同候选序列复用当次推理后，真实计算 40,139 对。时间包含辅助全文索引或多个实验臂，仅用于复现本轮工作量。该离线任务的外部模型请求数为 0。[文件、环境和模型版本指纹](../../benchmarks/public_rag/experiments/scifact_retrieval_rebuild/results/provenance.json)

### 回放与现场结果的对应

历史仓库公开了每题的候选来源、前五来源、候选完整证据与前五完整证据这四个集合。本轮先由原文和本机模型生成新集合，再逐题核对，历史双路 RRF 与 BM25 补位各自的 553 × 4 组全部一致。稠密前 30 的成员在 553 题全部相同。第 253 题的第 26、27 块和第 1186 题的第 27、28 块分别交换次序，这两组在当次 float32 余弦分数中均为平分。稀疏前 30、基线融合前 20、基线重排前 5 的块次序均为 553/553 完全一致。两种追加方案的数字来自本轮新增的实测。运行结束时工作树 HEAD 已为 `50fac1d`，期间提交涉及表格问答基准与前端，本实验用到的检索、切块、模型实现与起始版本相同。[历史逐题命中档案](../../benchmarks/public_rag/results/published/candidate-recall-20261114/portable/README.md)

项目现有候选设置适用于继续作为对照。本次使用已曝光的英语 TRAIN 分区和单一分块、模型组合，1 对依据的增量需要独立语料验证。计算在本机精确扫描向量，报告的终点是前五原文块。在线向量数据库的耗时、模型最终答复和引用正确性需分别测量。

## QASPER 分块复测

使用种子 `20261117` 对应的原 32 篇论文和 83 个取样问题。72 题同时满足全体标注者判断可回答、至少一份标注段落可在论文原文精确定位。三种切块分别在同篇文章内重建 BM25，取实际前六块。在本轮运行中，下表、所有新增和丢失的题号与 11 月 17 日归档 JSON 逐字段相同。[逐题历史对照](evidence/chunk-strategy-replay-20261117.json) · [复现程序](../../benchmarks/public_rag/chunk_strategy_replay.py)

| 切块 | 块数 | 至少一块包含完整标注段落 / 72 | BM25 前六含完整段落 / 72 | 前六较固定新增 / 丢失 |
|---|---:|---:|---:|---:|
| 固定窗口 | 808 | 48 | 28 | — |
| 按段落 | 1,861 | 71 | 30 | 11 / 9 |
| 自适应 | 1,880 | 66 | 27 | 9 / 10 |

按段落切分多保留 23 题的完整依据，前六净增 2 题。9 道退步题的标注段落仍完整存在，只是没有一个进前六。自适应多保留 18 题，前六净少 1 题。10 道退步题中 9 道仍有完整段落，另 1 道的整段在切块时被拆开。两种策略有 7 道共同退步题。现场导出的完整 JSON 按 UTF-8、LF 计算的 SHA-256 为 `bb9a60733af1022301324768b3c9e648acf65ef84dd7716c85735a88f35a41ac`，与归档相同。

## 复现

干净仓库可用 Python 标准库核验随附的 553 行派生数据、历史归档、各层汇总及每个问题–来源对的进退。此命令使用仓库内的公开数据文件。

```powershell
.\.venv\Scripts\python.exe -I -S benchmarks/public_rag/experiments/scifact_retrieval_rebuild/summary.py
```

全量模型重算需要项目的本地研究缓存。原始 BEIR 语料、查询和 TRAIN qrels 位于 `data/external/rag-public-20261021/scifact/extracted/scifact`，官方归档位于 `.scratch/scifact-rationale-source-20261114/data.tar.gz`。程序还校验历史 `scifact-state.json` 及 `parent-context-20261022` 候选和块标识审计。模型从仓库同级 `hf_cache` 下按固定 revision 读取。本机 QASPER 训练输入使用冻结 SHA-256 `9458bfe76074a8fa8d1685af02bcc73537aa6d338ad20591dfaff1946bc88bf4`。[历史输入清单](../../benchmarks/public_rag/results/published/candidate-recall-20261114/protocol.json) · [重排器权重清单](../../benchmarks/public_rag/results/published/reranker-batching-20261114/protocol.json)

```powershell
$work = Join-Path '.scratch' ('scifact-rebuild-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m benchmarks.public_rag.scifact_recall_index --output "$work/index"
.\.venv\Scripts\python.exe -m benchmarks.public_rag.experiments.scifact_retrieval_rebuild.replay --index "$work/index" --output "$work/results"
.\.venv\Scripts\python.exe -I -S benchmarks/public_rag/experiments/scifact_retrieval_rebuild/summary.py --directory "$work/results"
.\.venv\Scripts\python.exe -m benchmarks.public_rag.chunk_strategy_replay
```

验收时将标准库复核所需的 8 个文件单独复制到新目录，成功重算全部汇总。修改逐题记录会触发摘要校验。修改金标后同步重写摘要仍会触发历史分母核对。另运行 17 项既有召回、证据范围测试和 3 项历史归档自测，全部通过。随附明细只包含公开数字 ID、得失集合、候选增量、打分数量和排序一致性。
