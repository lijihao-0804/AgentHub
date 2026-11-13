# AgentHub 求职与面试入口

2026-11-14 新增了一条适合现场讲解的客服业务流程。专员录入客户，阿里云模型查未结工单并发起申请，另一账号审批。回执可打开真实工单，专员处理后结案，再次登录可以核对数量和状态。三次真实 Run 分别覆盖批准、查重、拒绝，合计 8 次模型步骤、16,518 个 token。这组数据描述一次流程验收，模型质量另有专项实验。[客服流程与复跑](../reviews/support-operator-closure-20261114.md) · [真实 HTTP 回执](../reviews/evidence/support-desk-20261114/public-http.json)

以下是 2026-11-06 冻结的 FINAL-CLOSE-1 离线展示，可在没有供应商账号的环境中使用。

先阅读[交付记录与能力—证据表](final-closeout-evidence-20261106.md)，按其中命令打开本地 Demo。
现场先讲清楚：模型是确定性适配器，Ops 为模拟回滚，评测为受控工程 observation；
真实运行的是策略、权限、checkpoint、队列、MCP、审批状态和安全结果投影。

建议展示顺序：

1. 打开已批准 Run，说明原 Run 在等待时经历真实进程重启，再由另一账号批准恢复。
2. 打开拒绝 Run，核对执行 NOT_STARTED、动作计数不变和拒绝答复。
3. 打开评测摘要与 UNKNOWN 逐题证据，解释执行状态、任务结果和未知费用各自含义。
4. 按[根 README](../../README.md)架构图解释 API→应用→合同→适配器和持久边界。

| 可讲能力 | 可核查入口 | 必须说明的边界 |
| --- | --- | --- |
| 工具治理与同 Run 审批恢复 | [现场回执](evidence/final-close-20261106/receipt.json)、[ApprovalService](../../packages/approvals/service.py) | 两账号实际不同；OWNER/ADMIN 仍有自审批豁免，不承诺任意远端 exactly-once |
| 评测可追溯与安全展示 | [逐题组件](../../apps/web/components/evaluation/run-case-evidence.tsx)、[安全投影](../../packages/evaluation/evidence_view.py) | 三题受控夹具，不是模型准确率；UNKNOWN 不能当零成本 |
| 上下文与不可变版本 | [ContextBudgetPolicy](../../packages/agent_runtime/context_budget.py)、[ADR-007](../adr/ADR-007-agentversion-resolved-snapshot.md) | 说明准入与冻结机制，不宣称自动摘要/Memory/RAG 新质量收益 |
| R1 失败分类 | [R1 报告](../reviews/R1-memory-extraction-outcomes-20261106.md) | 保留取消、崩溃及簿记失败边界；本轮沿用定点证据 |

已有[53 题深挖手册](../report/interview/README.md)、[项目报告](../report/README.md)和
[学习入口](../learning/README.md)继续复用，不另建题库。

个人简历只填写本人真实负责、复核和复现过的部分。实现、验收与材料含 Codex/DeepSeek 协作；
不写独立实现全部系统、企业生产落地、线上收益、完整硬金额预算或未做过的模型校准。
11 月 6 日的历史交付关联 [PR #17](https://github.com/lijihao-0804/AgentHub/pull/17)。后续本地交付与验证边界见[当前状态](../current-state.md)。
