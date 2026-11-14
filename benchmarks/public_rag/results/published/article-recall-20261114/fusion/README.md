# 文章级融合与原文块回选

SciFact 历史开发集 128 题和按正例文章连通组隔离的 553 题都曾用于研究。比较固定历史分支的前 30 个原块，固定 BGE 重排 20 块、交付前 5 个原块。开发集有 144 个正例文章配对，其中 92 对有官方证据；553 题有 625 个正例配对，其中 379 对有官方证据。完整证据要求至少一组官方备选证据的每句都完整可见。每个句子可在不同返回块内。[预先固定的选择规则](protocol.json) · [原始数据与历史协议](../../candidate-recall-20261114/README.md)

| 553 道历史题 | 候选命中来源对 | 前五命中来源对 | 候选完整证据对 | 前五完整证据对 | 原最佳正例块被移除 |
|---|---:|---:|---:|---:|---:|
| 基线 | 539 | 482 | 357 | 342 | 0 |
| 最多替换 4 个重复来源块 | 543 | 482 | 346 | 336 | 19 |
| 文章 RRF，再选原分支代表块 | 545 | 487 | 276 | 269 | 81 |
| 相同文章槽，重开原文用局部 BM25 选块 | 545 | 479 | 281 | 271 | 105 |

文章 RRF 对每个分支先按文章去重，再对压缩后的文章名次做 RRF；代表块是该文原块 RRF 最高者。回原文策略读完入选文章的全部原块，用同文档各块的词频与文档频率计算 BM25。相对文章代表块，553 题回选策略在候选池内增加 19 对完整证据、丢 14 对；前五新增 16 对、丢 14 对。相对基线，它在前五仅新增 2 对、丢 73 对。单篇一个槽使同篇其他支持块更难保留。553 题中有 1,270 个回选块在原始两个前 30 列表之外。[逐对增减与分组自举区间](report.json) · [逐题来源、证据及最佳原块](per-query.jsonl)

128 道开发题上，按上表顺序，来源对在候选池为 127、129、129、129，在前五为 113、113、114、113。完整证据对在候选池为 84、79、66、62，在前五为 81、76、63、59。

### q753 的文章和块

金标文章 1173667 的标题和摘要共 1,246 字符。题目为 “Malaria has a high vectorial capacity.”。块 0 覆盖字符区间 [0,1200)，全库 dense/sparse 名次 41/24，历史块 RRF 第 39；块 1 覆盖 [1000,1246)，名次 14/397，历史块 RRF 第 25。原双路 49 块完整并集内，两块 BGE 分别第 36、45，历史前 20 候选里两者均落选。分支内按文章去重后，文章 RRF 第 11。[原始双路及全库取证](../../candidate-recall-20261114/forensics-cases.json)

| 方案 | 原块 | 候选位次 | BGE 位次（20 块中） |
|---|---:|---:|---:|
| 最多补 4 块 | 1 | 20 | 20 |
| 文章 RRF 代表块 | 1 | 11 | 20 |
| 回原文选块 | 0 | 11 | 19 |

三个方案均未将该文送入前五。摘要讨论各国消除疟疾的可行性，官方仅给出引用来源，无句级标签。原 13 道漏例中三臂都仅救回 q753 和 q1109 的一篇来源。q1109 文章 13770184 的证据句位于块 2，代表块 3 排到 BGE 第 1，但只保留证据句末尾。局部 BM25 取块 1，排到第 10。全部 13 题均未恢复任何完整的官方证据对。[13 题的逐臂块 ID、坐标及排名](prior-misses.json)

q1210 文档 4928282 的第 9 句位于 [1259,1733)，局部 BM25 从块 0 切到块 1，前五恢复了完整证据。q109 文档 4319174 的第 5 句位于 [1053,1282)，从块 1 切到块 0 时丢失句尾，前五失去完整证据。两例均按历史检查队列固定顺序选取该方向的首例。[原句、块 ID、原文范围与文本摘要值](passage-examples.json)

### 复核

在仓库根目录运行下列命令，用标准库校验已发布逐题数据摘要、公用基线、三队列四方法四层的配对与题级增减。

```powershell
.\.venv\Scripts\python.exe -I -S benchmarks/public_rag/article_fusion_verify.py
```

执行实验使用历史 BGE 原池和完整并集实测 logit，原池共同块优先；额外重用经指纹校验的历史新增块实测值。余下 575 题的 1,558 对在 RTX 3060 Laptop 上用同版 bge-reranker-v2-m3、FP16、batch 8、max length 8192 实测，退出码 0，57.844 秒。未对四臂的所有 20 块重新做同批次推理；既往直接复测只校验了此前方案的 80 题。基线所有 681 题均逐项复现四层命中来源集合。全库 CPU 精确重构两路各 20,430 个候选位置的集合均与历史一致；dense 在 q1186 顺序有差异，所有策略统一使用历史真实顺序。[索引核对](index-parity.json) · [历史 logit 复测](../../candidate-recall-20261114/rerank-report.json)

具备历史研究输入、原文向量和模型缓存时，从当前仓库根目录运行以下 PowerShell 命令。每次创建唯一的工作目录和报告目录，已发布的冻结协议作为只读输入。命令使用此前实测的 1,558 个 BGE 分数，在 CPU 上重建候选、报告和索引核对。

```powershell
$python = '.\.venv\Scripts\python.exe'
$replay = Join-Path '.scratch' ('article-fusion-replay-' + [guid]::NewGuid())
$work = Join-Path $replay 'work'
$results = Join-Path $replay 'results'
$study = 'benchmarks.public_rag.article_fusion_study'
& $python -m $study prepare --work $work
& $python -m $study report --work $work --scores .scratch/article-fusion-20261114/scores.jsonl --output $results
& $python -m benchmarks.public_rag.article_fusion_parity --output $results
& $python -I -S benchmarks/public_rag/article_fusion_verify.py --results $results
```

需要重新进行 GPU 推理时，沿用上方的变量定义，在新工作目录依次执行以下命令。报告此时从同一工作目录读取新分数。索引核对与标准库复核命令与上方相同。

```powershell
& $python -m $study prepare --work $work
& $python -m $study infer --work $work
& $python -m $study report --work $work --output $results
```

逐题文件的四层依次为来源20、来源5、完整证据20、完整证据5；字符坐标为规范化索引文本的左闭右开区间。该实验测量检索和证据可见性，未执行答案生成。原报告保留首次实测的代码指纹，另存了命令行输出路径改动后的重放指纹和核对范围。[CLI 重放记录](cli-replay.json) · [选择器](../../../../article_fusion_candidates.py) · [实验实现](../../../../article_fusion_study.py)
