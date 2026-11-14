# 在新的 NFCorpus 查询上检验文章召回

从官方 TRAIN 的 2,590 个查询 ID 中排除上一轮使用的两组、共 256 题。用事前固定的盐从剩余 2,334 个 ID 中取出 128 题，不根据查询内容、相关度或表现选题。候选文档覆盖全库 3,633 篇，切块后为 6,818 段。SciFact 的两种提名在此没有调整。这里的独立性指查询与前两组无重叠，论文仍可能重叠。[抽样、指标与否决条件](protocol.json)

| 相同 128 题 | 原双路 RRF | 按文章融合，再选段 | 完整摘要补位 |
| --- | ---: | ---: | ---: |
| 候选前 20 的来源宏召回 | 18.03% | 19.44% | 19.46% |
| 候选来源新增／丢失配对 | — | 76／0 | 60／0 |
| 重排前 5 的来源宏召回 | 11.63% | 11.48% | 11.78% |
| 重排前 5 来源新增／丢失配对 | — | 52／21 | 22／9 |
| 重排前 5 从零命中恢复／原命中清空的题 | — | 3／4 | 1／0 |

两种方案都保住了基线前 20 至少命中一篇标注来源的全部 100 道题。前五的基线分母是 90 道有命中题。按文章回选只保住其中 86 道，完整摘要补位保住了全部 90 道，但在部分多来源题上仍丢了 9 个标注配对。每道题的标注来源数不同，宏召回对各题等权；配对计数和由零变有的题数分别报告。逐题变化见 JSON 的 `changes_by_query`。[完整指标](report.json)

用 BGE-M3 实算查询、原段与父文档向量；在各自的全库矩阵上精确检索，稀疏路线沿用 tokenizer 对数词频和各自语料的 IDF。每个方案送重排 20 个原段。对相同查询的相同段共用 logit，三个方案共打分 3,425 个不同查询–原段对。这里没有问答或句级标注，耗时来自离线矩阵实验。[问答端的实际证据](../live/README.md)

## 重建

在仓库根目录执行。需要本机项目依赖、CUDA、协议指定的 NFCorpus TRAIN 文件与 BGE 快照。以下 `$dense` 和 `$reranker` 分别指向 BGE-M3 和 BGE-reranker-v2-m3 的权重目录；程序逐文件核验 SHA-256。capture 不使用题目的金标选择候选，score 再读金标计分。

```powershell
$w = Join-Path .scratch ("article-nfc-" + [guid]::NewGuid())
New-Item -ItemType Directory $w | Out-Null
.\.venv\Scripts\python.exe -m benchmarks.public_rag.article_transfer index --output "$w/index" --model $dense
.\.venv\Scripts\python.exe -m benchmarks.public_rag.article_transfer capture --index "$w/index" --model $dense --reranker $reranker --output "$w/trace.jsonl"
.\.venv\Scripts\python.exe -m benchmarks.public_rag.article_transfer score --trace "$w/trace.jsonl" --output "$w/report.json"
```
