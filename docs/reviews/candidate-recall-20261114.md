# 13 道漏召回题与候选策略的验收

本次从本机版本 `f02a6fb` 出发，以相同文档、查询、模型和候选预算重放检索。主线继续使用稠密和稀疏各取 30、RRF 选 20、BGE 重排的流程。新策略扩大了论文来源覆盖，但真实段落和引文的对照未达到合入条件。可运行的候选策略、反例、指标和已冻结的复跑协议随仓库归档。[全部方法及数字](../../benchmarks/public_rag/results/published/candidate-recall-20261114/README.md)

## 13 题丢在哪一层

对 5,183 篇论文的 9,189 个历史分块重新编码。13 题的稠密前 30、稀疏前 30 均逐块复现历史顺序。下表列出每种成因的题号，单题页另给全部 14 个查询–来源对的分块数、全库名次、RRF 名次、证据句位置和反事实检索结果。[逐题核查](../../benchmarks/public_rag/results/published/candidate-recall-20261114/FORENSICS.md)

| 原流程最先丢失的阶段 | 题号 | 复核结果 |
| --- | --- | --- |
| 至少一路召回后，被 RRF 挤出前 20 | 63、572、610、753、900、1074、1109 | 对两路完整并集重排，仅 1109 的一篇标注论文进入前五，实际返回的段只覆盖证据句最后 103/790 个字符。 |
| 稠密和稀疏前 30 均无标注来源 | 609、620、817、818、828、1387 | 609 的最佳稠密块排第 32。818 的两组官方证据都在同一块内，两路仍只排 3684 和 842。 |

切块对具体题有影响。753 的两路各命中同篇论文的不同块，按块融合排 25，按文章合并贡献排 11。828 单独改用整篇表示、固定其余竞争块，稠密排名从 39 到 30。1109 的第二篇论文同样处理后从稠密 45、稀疏 35 变为 19、3。只有 818 和 1109 有官方句级标注，其四组证据都能在原块中完整找到。其余 11 题只能确认来源名次。[AllenAI 数据定义](https://github.com/allenai/scifact/blob/master/doc/data.md) · [全部重建数值](../../benchmarks/public_rag/results/published/candidate-recall-20261114/forensics-cases.json)

## 哪些收益到达了用户

仓库附有 681 条派生明细，每行保存公开题号、标注论文 ID，以及各方案在不同阶段可见的来源和完整证据。只需 Python 标准库即可重新计算宏召回和全部新增、丢失的题目–来源对。要重新生成向量或模型分数，需使用下方完整实验的原始输入。[无需 GPU、数据库或凭据的复核](../../benchmarks/public_rag/results/published/candidate-recall-20261114/portable/README.md)

在 128 道开发题上比较原双路尾部补位、每篇先取一块、头部加权 RRF、DBSF、三路等额 RRF 和独立 BM25 补位。SciFact 确认集的 553 题与开发题不共享金标论文的连通分量。项目早先已用过这批公开 TRAIN 数据，两组不同的 NFCorpus TRAIN 查询补充检验跨语料表现。全程未打开官方 test 或平台 HOLDOUT。[分组协议](../../benchmarks/public_rag/results/published/candidate-recall-20261114/protocol.json)

| 比较 | 结果 | 验收判断 |
| --- | --- | --- |
| 原双路补位，SciFact 553 题 | 新增 3 个金标来源对，保住原有 539 个，来源宏召回增加 0.452 个百分点。 | 低于事先设定的 0.5 个百分点。 |
| BM25 补位，SciFact 553 题 | 候选宏召回 86.46% → 87.72%，新增 8 对、丢失 0 对，6 道原零命中题重新找到来源。恢复的 8 份摘要都没有针对该原题的官方证据句。 | 候选层达标。复核摘要后，8 份均不足以直接回答原断言。 |
| 同一 BM25 方案，真实 BGE 前五 | 首次命中来源的题有 5 道，同时有 5 道原命中题变成零命中。能提供完整官方证据的来源对 342 → 339（新增 3，丢失 6）。候选 20 内则从 357 降至 348。 | 下游非劣性未通过。 |
| BM25 补位，NFCorpus 第一组 128 题 | 20 块来源宏召回 11.35% → 12.18%，原 85 道命中题保留，新增 32 个来源对，丢失 0 对。 | 独立复现了来源层收益。 |
| 再保护原 BGE 前十及每篇最高分块，SciFact 553 题 | 20 块来源对新增 3 个，前五完整证据 342 → 342，候选内完整证据 357 → 356。另需重排 383 个新段。原 13 题仍全部漏召回。 | 仍有一组原证据被替换，且此阶段已知 SciFact 结果。 |
| 同一保护方案，NFCorpus 另 128 题 | 20 块宏召回 18.70% → 19.55%，新增 10 个来源对；重排前五保留原 84 道命中题，来源对新增 2 个、丢失 1 个。共打分 2,613 对。 | 第二组预设门槛通过，前五有命中题数持平。 |

初步候选方案中，双路补位在最早的 13 个漏例里找回 1 题，BM25 补位找回 0 题。跨数据集的宏召回率以各题标注来源数为分母，不同语料的绝对水平不可直接作优劣排序。[模型重排](../../benchmarks/public_rag/results/published/candidate-recall-20261114/rerank-report.json) · [句级核查](../../benchmarks/public_rag/results/published/candidate-recall-20261114/rationale-visibility.json) · [NFCorpus 第一组](../../benchmarks/public_rag/results/published/candidate-recall-20261114/nfcorpus-lexical_refill4.json) · [第二组](../../benchmarks/public_rag/results/published/candidate-recall-20261114/nfcorpus-protected_refill.json)

## 真实问答

用同一个完整快照、本机 CPU BGE、同一个阿里云模型配置，对基线和 BM25 各执行六次首次 `POST /api/v1/knowledge/query`。第 838 题的新增来源从候选第 18 升到重排第 2，但摘要研究的是生殖功能，无法支持原题的人体胆固醇转运断言。两臂均拒答且无引文。对同篇摘要另问它实际报告的功能，BM25 臂回答并引用原文，两臂检索本来都已命中该文献。

基线六次首次请求中有三次 502，实验臂六次均为 200。另用真实上传和 Celery 检查 42 篇论文子集及 18 份合成客服材料。客服题的目标账号记录在两路都排 25，两臂均拒绝给出其他账户的退款期限。三份语料合计 22 次问答，6 次返回 502。错误请求另由不调用生成模型的同配置试验台补取轨迹，22 次候选前 20 块均与独立重放一致。12 项跨工作区或错误快照检查均返回 404。这些首次答复保留模型失败，样本不支持估计总体答案准确率。[真实请求、语义核读及复跑](../../benchmarks/public_rag/results/published/candidate-recall-20261114/live/README.md) · [8 对新来源的逐篇审核](../../benchmarks/public_rag/results/published/candidate-recall-20261114/live/rescue-source-review.json)

## 实验分支与查询成本

本地分支 `codex/rag-candidate-recall-lab-20261114` 的提交 `4673299` 保存了实际用于问答的 BM25 接口、快照索引、迁移和测试。保护高分块的第二版仅在真实 BGE 分数上做检索试验。全库索引有 797,447 条倒排记录。32 题 SQL 检查中，2 题成功，前 20 个块、来源和分数逐项与 Python 参考相同。两题热查询分别耗时 4.39 与 5.73 秒，后两题触及 15 秒上限，余下 28 题按约停止。第 1074 题的单次 PostgreSQL 执行计划显示，快照成员连接过滤了约 8,434 万个中间配对。[SQL 范围、逐题耗时及执行计划](../../benchmarks/public_rag/results/published/candidate-recall-20261114/lexical-index.json)
