# AgentHub RAG 进度与成果入口

2026-11-24，基于 `43e3093` 新增可选的 OpenAI/百炼兼容 `OpenAICompatibleDenseEmbedder` 与 `OpenAICompatibleReranker` 适配器（保持本地 BGE 默认不变，14 项单测与 ruff 通过），并完成 SciFact、QASPER 与 MuSiQue 三基准系统对照。SciFact Test 300 上 `qwen3.7-text-embedding` + `qwen3-rerank` 将 TOP20 召回由 87.51% 升至 93.60%（纯 Dense nDCG@10 达 0.7903），553 题确认集 TOP20 由 86.46% 升至 92.04%（`flash` 版降至 85.01% 不建议替换）。QASPER 72 题向量对照中 `structural_v1` 搭配 `qwen3.7` + `qwen3-rerank` 将跨 32 篇 TOP5 完整证据由 22.2% 升至 41.7%（本地 BGE 下也由 22.2% 升至 30.6%）。MuSiQue 200 题生产 `consumer_replay` 链路中，`qwen3-rerank` + `complementary_v1`（关思考 198/200 生效）将模型可见证据召回由 78.2% 升至 91.4%、完整证据集合率由 55.0% 升至 78.0%（纯 Dense 降至 74.0%）。[三基准数据、负结果与适配器说明](rag-qwen37-api-benchmark-20261124.md)

2026-11-20，实测关闭选句推理与保留空截断回执。固定历史证据的三题全部恢复交付，p6 完整通过，p4 漏选规则，p7 原输入缺 R@3。真实入口每臂 17 题都交付，新题均 8/8 严格通过，旧题均 7/9 严格通过。候选完整答案少一题，PR #27 未过门槛。98 笔供应商回执全部确认，费率估算 0.52546000 元。[Agent 首答、用量及逐题结果](pr27-selector-repair-20261120.md)

2026-11-20，在 `c721243` 上测试论文选句回填 `15095e3`。固定历史证据恢复 3/4。真实入口新题严格通过从 7/8 到 8/8，历史回归从 6/8 到 5/8。p4、p6、p7 的选句轮均触及 2,048 token 上限，无可见正文。100 次调用全部有回执，费率估算 0.65600064 元。PR #27 未过合并门槛。[逐题证据和费用](pr27-sentence-delivery-20261120.md)

2026-11-20，从 `0ff247a` 到 `71b7f19`，通过实际工作区线程配对复测 9 道论文题和 14 道数分题。纯表格交付从 3/9 提升到 9/9，论文严格通过从 8/9 降到 1/9。PO-EMO 的混淆矩阵曾由第三次搜索找到，最终逐字摘录策略实际触发 7 次，均未返回答案。全程 95 回合、301 个模型回执，按回执估算 1.21993536 元。PR #27 保持开放，主线维持原默认。[配对报告、失败定位和计费](pr27-paper-table-closure-20261120.md)

2026-11-20 从 `e4292ac` 开始，验证了工作区论文研究的真实模型入口和同步引用回查。QASPER 三篇论文走上传、worker、BGE-M3 和固定快照；种子词典及随机森林题的核心结论有据，PO-EMO 仍漏掉标注者标签的混淆矩阵。12 篇语料的受控检索对照和 BGE 实收成绩分别列出。[逐题结果与限制](business-agents-field-20261120.md)

2026-11-19 从主线 `4a0daa2` 审阅入库、证据预算、引用和网关计量。278 题的历史候选池重放逐项保留原选段，401 条已确认回复覆盖 342 次问答，仅 W13 改为正确拒答。三道上传至问答的本地 HTTP 实测均符合预期，外部模型调用为零。历史 57 次输出截断仍单列。[缺陷、回归与验收边界](rag-pipeline-correctness-20261119.md)

2026-11-18 一般文本的预算实验固定 553 道 SciFact 题的候选融合与 BGE 重排。512 个估算正文 token 中合并同源重叠区间，让完整依据从 277/379 到 285/379，8 题新增、0 题丢失。当前按研究策略交付，线上引用仍需多块定位契约。[逐题结果与边界](../../benchmarks/public_rag/experiments/source_span_budget/README.md) · [本轮工作区整体验收](workspace-quality-20261118.md)

2026-11-18 从 `f2822c3` 修复 MinerU 表格中多层列名的丢失。两张 W3C 公开示例表、14 条冻结查询经过 PostgreSQL、离线 BGE 和生产检索路由。有答案题重排第一均为 10/10，严格抽取值和源单元格同时正确由 2/10 提升到 10/10，缺失条件拒答均为 4/4。在3B本地受限解码的真实引用问答中，值与引用同时正确由1/10到7/10，正确拒答由1/4到2/4，仍有两题值对引错。0.5B失败、语法编译降级和首笔云端401另行记录。[多层表头实验与复现](rag-table-header-evidence-20261118.md)

2026-11-18 从 `9a27147` 增加工作区精确向量复用，稠密和稀疏独立指纹，新修订保留自己的块 ID 与引用位置。最终生产版本 `6d2b731` 用两篇真实 Git 修订完成结构切块、默认固定窗口各四对。结构切块新版每路少编码 105/132 块（79.55%），上传至 READY 中位耗时下降 30.13%；固定窗口少算 0/23，耗时增加 8.79%。640 条引用定位核对全部通过，结构实验两次旧快照的 Top5 尾部出现差异。细节、逐轮数据、负结果和复现见[交付报告](document-vector-reuse-20261118.md)。本轮模型本地离线运行。

2026-11-17 从 `fafe4aba` 增加 Windows 离线 OCR（`32c0df1`）、布局与表格导入（`baf2caf`、`30f7ff4`）和修订级切块（`8c84de4`）。QASPER 训练集 72 题的单篇文章 BM25 前六命中为固定 28、段落 30、自适应 27。两种可选策略分别丢失原先命中的 9、10 题，继续默认固定窗口。本轮外部模型请求为 0。测试覆盖真实 PostgreSQL、上传、worker 重投、冻结证据与检索入口。实现、复现命令与指标口径见[交付和验收](document-ingestion-and-delegation-20261117.md)，逐题变动见[配对结果](evidence/chunk-strategy-replay-20261117.json)。

2026-11-16 本地主线整合收录运行时修复（`0ad18de`）、旧版本读回验证（`debdb84`）与研究摘要（`97708fd`）。学习入口见[开发历程](../development-history/index.html#view=recent)。

2026-11-15 记录。本轮成果已选择性整合回原项目 `E:\JAVA\AI+agent\AgentHub`，当前本地开发分支 `codex/local-main-synced-20261115`。今后默认在原目录使用主题分支和小提交，不另开项目副本；历史研究副本仅保留。未推送、未部署、未切默认，业务库未迁移。

## 当前成果

- [12题四臂真实比较与归因](rag-crossversion-r2-result-20261115.md)：旧默认10/12、当前默认9/12、互补9/12、充分证据诊断8/12。互补没有净增且新增误拒；总输入2.23倍、费用2.04倍，不支持切默认。
- [逐题派生失误表](evidence/rag-crossversion-r2-20261115/question-failure-matrix.csv)、[配对汇总](evidence/rag-crossversion-r2-20261115/paired-summary.json)、[真实PG持久化对账](evidence/rag-crossversion-r2-20261115/real-postgres-reconciliation.json)。
- [冻结协议](rag-crossversion-r2-protocol-20261115.md)、[原目录免费复核/历史材料定位](rag-crossversion-r2-reproduce-20261115.md)、[本次整合验收](agenthub-local-rag-integration-20261115.md)。
- [RunStep迁移验收](run-step-kinds-acceptance-20261115.md)：原 `f32f66c`、`c3ea72b` 依次带回为 `5b1858c`、`b03cc73`，6文件最小修复，没有引入失败候选。

正式112笔请求全部有回执，新增UNKNOWN为0；保守估算新增¥0.954522，含历史139笔usage及两笔UNKNOWN预留累计¥1.923046。不是供应商账单。本次本地整合请求0、费用0；账本CLOSED，不重发旧UNKNOWN。

## 重要阶段与封存位置

表中前序文件位置相对于**各自封存checkout**，不是声称文件已经全部带入本仓库。封存根/目录和免费复核命令见[复核说明](rag-crossversion-r2-reproduce-20261115.md)。本地已有10月研究报告也继续保留，见[原研究保留索引](rag-research-retention-20261115.md)。

| 提交 | 阶段与结论 | 封存目录 / 报告相对路径 |
|---|---|---|
| `73a287a`、`47bf311`、`16463a0` | P01/D01停在失败；两笔UNKNOWN保留，wire/SDK离线排查 | `agenthub-support-scope-r1` / `docs/reviews/e2e-scope-20261115.md`、`e2e-scope-amended-20261115.md` |
| `d1f9131`、`d85c87c` | D02单次真实链路成功，只证明回执链路 | 同上 / `docs/reviews/d02-result-20261115.md` |
| `14694e2`、`4d0f9cc`、`6d6e9a2` | 18题默认13/18、范围候选4/18，候选否定 | 同上 / `docs/reviews/e2e-formal-result-20261115.md`、`evidence/e2e-formal-20261115/failure-matrix.csv` |
| `4853fea`、`3f768c5`、`1023d8f` | 16题7/16→9/16，联合门失败；H未过可靠性门 | 同上 / `docs/reviews/slot-state-result-20261115.md` |
| `e8be4e8`至`248daa8` | 免费跨度配准36组响应不变；语义改善未认证 | 同上 / `docs/reviews/span-registration-result-20261115.md`、`rag-source-comparison-design-20261115.md` |
| `1b05224`至`efa786a` | 来源分段/邻窗/父条款交付三组均10/10，增1905近似token；扩展关闭 | 同上 / `docs/reviews/delivery-segments-result-20261115.md` |
| `6e5e6f8`、`05261cc`、`c047042` | ConditionalQA 34题：仅1题1文档确定top6漏项，未过机制门 | 同上 / `docs/reviews/conditionalqa-trace-result-20261115.md` |
| `2c32fd6`、`bcade96`、`69f7358` | LegalBench 35文档728块28题；标签/身份/容量限制，结束扩段研究 | 同上 / `docs/reviews/legalbench-privacy-final-result-20261115.md`，完整逐轮索引为`AGENTHUB_RAG_PROGRESS.md` |
| `df13914`、`c2240b1`、`c4eb5f4` | 已审产品切片合并远端，再与原本地开发内容三方整合 | 本仓库 / `docs/reviews/agenthub-release-readiness-20261115.md` |
| `a532002` | 首批15题/11可裁定，零付费、关闭；发现PG步骤约束遗漏 | `agenthub-crossversion-20261115` / `docs/reviews/rag-crossversion-result-20261115.md` |
| `f32f66c`、`c3ea72b` | 两类步骤最小修复与真实PG/旧读取验收 | `agenthub-run-step-fix-20261115`；已带回本仓库 |
| `2478f1d`、`cbd179e`、`5fcc53e`、`577b387` | 第二批24组先审16合格8UNKNOWN，固定12正式；执行完成、负结果收口 | `agenthub-crossversion-r2-20261115`；精简结果已带回本仓库 |

没有把改评分口径当提升：R206等价关系表达对四臂一致处理；严格要求字面0.89会四臂同减一题，配对差不变。评阅为内部AI同伴，非新增人工金标。12个指定来源英文金融题不代表中文客服或生产总体。

后续若用户授权新质量工作，优先针对有原文支持的单位、指数基准、计算和范围理解提出可证伪方案，另冻新题；不在本轮样本上循环调参，不复活失败默认候选。

## 2026-11-18 召回历史复测

从 `8ba285b` 的检索实现重新编码 SciFact 的 5,183 篇全文、9,189 个分块和 809 个训练查询。553 题的双路基线与 BM25 替换补位逐题重现了候选、前五和完整依据四组历史评分。固定 20 块时，BM25 使候选来源 539→547 对，前五完整证据 342→339 对（新增 3、丢失 6）。保留 20 块再追加 BM25，前五完整依据 342→342，等深度的双路 RRF 续取为 343。两种扩容均多打分 8.89%。另从 QASPER 原文复算 72 题，固定、段落、自适应切块的前六完整证据仍为 28、30、27 题。两个语料均使用公开 TRAIN。17 项检索回归和 3 项历史复核自测通过，本轮离线外部模型请求为 0。[复现、逐题进退与限制](rag-retrieval-rebuild-20261118.md)

## 2026-11-18 北京工作区路由和表格问答复核

以项目原配阿里云路由重测两张公开 W3C 表。真实问答入口对 14 题各跑两个版本，值对且引用覆盖金标单元格的可答题为 3/10 与 10/10，首次 HTTP 请求正确拒答为 3/4 与 4/4，两臂直接在首个模型回复正确拒答均为 0/4。连同冻结证据抽取对照，本轮合计 63 次供应商请求，对应 63 个不同回执，费率估算 0.163492 元。上轮 401 仍单列，已曝光小样本用于机制诊断。[问答入口、配对记录和计费](workspace-appearance-and-live-rag-20261118.md)
