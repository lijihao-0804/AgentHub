# AgentHub 文档导航

学习项目请走 [00–10 完整课程](learning/README.md)，按架构→请求→运行时→版本→治理→恢复→RAG→Memory→评测→业务闭环→实验顺序。

先看 [当前状态与证据口径](current-state.md) 和 [根 README](../README.md)。功能收口基线为 main `e7dc1f6`；功能开发已停止，旧计划未全部实现。

| 想了解什么 | 入口 |
| --- | --- |
| 项目已做什么、已证明什么、还不知道什么 | [current-state](current-state.md)、[最终收口报告](reviews/AgentHub-closure-memory-quality-20261005.md) |
| 一次 Run 如何经过模块 | [architecture](architecture.md)、[详细数据流](report/02-架构与数据流.md)、[学习路线](learning/README.md) |
| 对外行为与数据归属 | [API contracts](api-contracts.md)、[data model](data-model.md)、[模型/API 讲解](report/09-数据模型与API.md) |
| 为什么这样设计 | [报告目录](report/README.md)、[ADR-001 架构](adr/ADR-001-modular-monolith.md)、[ADR-007 发布](adr/ADR-007-agentversion-resolved-snapshot.md)、[ADR-011 Memory](adr/ADR-011-memory-must-be-snapshotted.md) |
| 如何面试讲解和连续追问 | [面试手册](report/interview/README.md)、[现场演示](report/07-现场演示脚本.md)、[历史截图](report/18-截图演示.md) |
| 数据和评测如何解释 | [benchmark 索引](benchmark/README.md)、[M-I3/M-I4 正式证据](reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md) |
| 找验收、失败和审查记录 | [reviews 索引](reviews/README.md)、[M4](milestones/M4-E.md)、[M5-A](milestones/M5-A.md)、[M6](milestones/M6-final-acceptance.md)、[M7-H](milestones/M7-H.md) |
| 安全与交付约束 | [安全基线](security/baseline.md)、[版本交付](versioning.md)、[凭据迁移](deployment/provider-credential-migration.md) |
| 原始需求及未来范围 | [产品总计划](../plan/plan.md)、[应用拓展](../plan/extend-plan.md)、[归档延期计划](reviews/AgentHub-待做事项详细推进计划-20261005.md) |

求职/展示优先入口：[紧凑项目介绍、简历和 12 道追问](portfolio/README.md)、[核心 Demo](report/07-现场演示脚本.md)、[两张架构图](architecture.md)。

## 阅读口径

当前状态页是汇总，不覆盖产品不变量、源码或 OpenAPI。验收报告固定历史时点，旧的“缺 MCP UI/无前端脚本/0029 head”不能当作当前结论。
测试、合成业务实验、脚本消费者、助手审阅与真人/生产证据分别解释。历史报告中的失败保留，不替换成后来成功值。
