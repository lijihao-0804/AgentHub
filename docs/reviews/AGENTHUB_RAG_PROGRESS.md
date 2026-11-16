# AgentHub RAG 进度与成果入口

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
