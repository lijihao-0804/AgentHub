# AgentHub 求职与面试入口

FINAL-CLOSE-1 已于 2026-11-06 完成本地收口。功能冻结，不再以 backlog 清空作为写简历前提。

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
远程候选尚未推送/CI；本地交付与远程发布分开陈述。到此停止功能施工。
