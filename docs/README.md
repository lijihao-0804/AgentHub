# AgentHub 文档导航

截至 2026-11-05，当前入口同步到 main `fcafc83e2ad68c9b1578f5117365aaac2732852c`：AE6b、AE10、统一回归/文档及上下文/Memory/Wiki（PR #11）已合并，迁移 head 为 `0052_memory_recovery_index`。最终 PR #11 的 PR/push CI 已通过；合并后主线、独立 provider 安全分支与研究 draft 均在 [当前状态](current-state.md)单独记录。历史计数和 skip 按原轮次保留。

先看 [当前状态与证据口径](current-state.md)。它把代码与产品入口、工程验收、真实模型质量和生产部署分开说明；历史报告只代表各自日期与提交，不改写成当前总体结论。

| 想了解什么 | 当前入口 |
| --- | --- |
| 最新主线、实际能力、证据与边界 | [current-state](current-state.md)、[根 README](../README.md) |
| AE6b 交互 Experiment、worker、崩溃恢复和未知写入 | [AE6b Integration Closure](milestones/AE-6b-integration-closure.md) |
| AE10 评测结果 UI、筛选分页和 observation 隐私收口 | [AE10 最小评测结果界面](milestones/AE-10-minimal-evaluation-ui.md) |
| 统计门禁的原始 JSON、逐项 PASS/FAIL/INCONCLUSIVE 与真实/契约分类 | [AE 能力证据矩阵](milestones/AE-capability-matrix.md) |
| 本轮仍未完成或待核对的工作清单 | [WIP inventory 2026-11-04](reviews/WIP-inventory-20261104.md) |
| 统一前端检查、API 隐私专项与旧断言迁移 | [T02/T03 交付及运行命令](reviews/post-ae10-delivery-20261104.md) |
| 上下文配置、Wiki UI、Memory 恢复与显式候选策略 | [2026-11-05 开发交付](reviews/context-memory-delivery-20261105.md) |
| Provider URL、DNS pinning、私网例外及冻结端点 | [出站合同](security/provider-egress.md)、[独立修复交付](reviews/provider-egress-delivery-20261105.md) |
| 一次 Run 如何经过模块 | [架构](architecture.md)、[详细数据流](report/02-架构与数据流.md)、[学习路线](learning/README.md) |
| 对外行为与数据归属 | [API contracts](api-contracts.md)、[data model](data-model.md) |
| Dashboard、失败分析、审批与运行观测 | [M6 最终验收](milestones/M6-final-acceptance.md)、[观测指标口径](reviews/AgentHub-M-I1观测契约与指标口径-20261002.md) |
| 为什么这样设计 | [ADR-001 架构](adr/ADR-001-modular-monolith.md)、[ADR-007 AgentVersion](adr/ADR-007-agentversion-resolved-snapshot.md)、[ADR-011 Memory 快照](adr/ADR-011-memory-must-be-snapshotted.md)、[0051 交互生命周期](adr/0051-interactive-execution-lifecycle.md) |
| 如何展示、面试和连续追问 | [求职材料](portfolio/README.md)、[核心 Demo](report/07-现场演示脚本.md)、[面试手册](report/interview/README.md) |
| 历史评测与失败记录 | [reviews 索引](reviews/README.md)、[benchmark 索引](benchmark/README.md)、[M-I3/M-I4 历史证据](reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md) |
| 安全与部署约束 | [安全基线](security/baseline.md)、[版本交付](versioning.md)、[凭据迁移](deployment/provider-credential-migration.md) |

项目学习从 [00–11 完整课程](learning/README.md)开始。课程按架构、请求、运行时、版本、治理、恢复、知识、Memory、评测、业务闭环和 Agent 工程展开；[第 11 课](learning/11-agent-engineering.md)适合把工具提议、执行证据、恢复与评测串起来。

## 阅读口径

- 代码存在、单元/集成测试通过、真实模型小样本运行和完整生产部署是四种不同证据。Dashboard 已有 p50/p95 与趋势，worker/beat 已有内部周期对账，Memory 读取已过滤过期条目；文档仍需保留各自的实现和验收边界。
- 统计门禁不在这里概化。具体规则、原始 JSON、真实运行与合成契约结果以 [能力矩阵](milestones/AE-capability-matrix.md) 为准。
- `docs/milestones`、有日期或 SHA 的 `docs/reviews`、旧截图和试跑数字是历史材料。历史失败和费用保留；旧报告不能覆盖 AE6b/AE10 当前验收，也不能覆盖当前 worker 接线结论。
- `plan/plan.md` 是需求与语义设计，不是全部完成声明。当前未闭环事项以 [WIP inventory](reviews/WIP-inventory-20261104.md) 和当前状态页为准。
