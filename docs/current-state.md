# AgentHub 当前状态与证据口径

更新日期：2026-10-05；功能/质量收口基线：`e7dc1f6f238c2c6e1c8739082a8301bc2d0c115e`，已在 main。
本次是文档同步，不新增功能或实验。**FEATURE DEVELOPMENT: STOP**。
本页汇总状态；产品不变量仍由 [总计划](../plan/plan.md) 和 [AGENTS.md](../AGENTS.md) 定义。

## 1. 已实现能力

| 领域 | 当前实现 | 入口/证据 |
| --- | --- | --- |
| 租户与模型 | Auth UI、组织/工作区、RBAC、凭据加密、模型档案、能力/重试/可见 token 前 fallback | `packages/control_plane`、`packages/model_gateway` |
| 发布与执行 | 不可变解析规格与规范哈希、LangGraph Run、预算、流式/重连/停止、费用闸门 | `packages/agent_runtime`、[ADR-007](adr/ADR-007-agentversion-resolved-snapshot.md) |
| 知识 | 修订/生命周期、异步入库、索引对账、Dense/Hybrid、快照、固定证据、T12 分片预览 | `packages/knowledge`、[T12/T28 验收](reviews/AgentHub-前端补缺批次一验收报告-20261003.md) |
| 工具与审批 | effect/risk/approval 分离、持久审批与恢复、逻辑动作身份、不确定结果保留、MCP 管理 UI | `packages/tools`、`packages/approvals`、`/tools/mcp` |
| 应用 | Research/Incident/Analyst/Support、Thread/Artifact、多轮会话流式、Markdown 复制 T28 | `packages/threads`、`packages/artifacts`、`apps/web` |
| 评测/反馈 | 发布数据集、冻结实验、holdout 暴露、消融/成对比较/门禁 UI；评分/纠正/审核/DEV 草稿导入 | [M-I1](reviews/AgentHub-面试增强M-I1完成报告-20261002.md)、[M-I2](reviews/AgentHub-面试增强M-I2完成报告-20261002.md) |
| 观测/接管 | 时延/失败/usage/cost、工具摘要、固定引用；分配/接手/关闭、权限/并发版本/审计/证据保留 | [M-I5](reviews/AgentHub-面试增强M-I5验收报告-20261003.md) |
| Memory | 共享 workspace+agent 语义；默认关闭、异步用户输入抽取、精确引文、去重、停用/启用、ID/hash 快照、UNTRUSTED、最终准入 touch | [长期记忆报告](report/19-长期记忆与上下文管理实施报告.md)、[ADR-011](adr/ADR-011-memory-must-be-snapshotted.md) |

T12/T28：**CLOSED**。MCP 管理 UI、Auth UI、工具参数摘要、handoff 生命周期、前端专项脚本均已存在。
M7-G UI 实现存在；不因后续功能落地补造旧里程碑的独立验收记录。
当前迁移 head 为 `0031_handoff_cases`（`0030_run_feedback`、`0031` 是早期 0029 基线之后的变化）。

## 2. ALREADY VERIFIED

- Memory API/Celery/PostgreSQL、跨 Thread ON/OFF、停用/启用、快照重放/审批恢复、hash 失败关闭、UNTRUSTED、RBAC/租户隔离、并发冲突、准入 touch 和独立预算有既有证据。
- [M-I3/M-I4](reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md)：60 条合成客服、DEV 40/HOLDOUT 20、两个变体各三次，共 360 个 trial。HOLDOUT 业务成功 47/60→60/60；结合助手语义 41/60→57/60。
- 同报告 RAG 对照：8 个短政策、24 DEV 问题、两策略各三次，共 144 次；最终目标召回两策略均 72/72。重排排序略改善，时延更高；不是一般规模下策略优劣结论。
- 有安全注入、租户隔离、worker crash/beat 恢复、HTTP 重连、usage/cost、1/4/8 并发各 60 秒受控负载证据。受控吞吐不是系统饱和容量；人工接管不等于自动副作用对账。
- [排版专项](reviews/AgentHub-前端文字排版修正报告-20261003.md)与 [T12/T28](reviews/AgentHub-前端补缺批次一验收报告-20261003.md)保留各自浏览器范围，不宣称全部交互持续 E2E。

## 3. NEWLY VERIFIED：Memory 质量边界

[收口报告](reviews/AgentHub-closure-memory-quality-20261005.md)和 [结果](../benchmarks/evaluation/memory_quality/result.json)记录 11 个确定性场景，10 类；1 个 OFF sanity control，新增付费调用 0。

| 指标 | 分子/分母 | 解释 |
| --- | --- | --- |
| Write precision | 6/9 | 按强制候选落库条目计，非真实模型抽取准确率 |
| Write recall | 5/5 | 按应写场景计 |
| Required recall | 2/2 | 必须真实进入最终 ContextAdmissionResult，选中不等于准入 |
| Forbidden/irrelevant recall | 5/8 | 错误召回；已有条目的 5 个禁止场景全部准入 |
| Task correctness | 10/10 | 脚本消费者后置条件，非真实 LLM 成功率；冲突场景 null 排除 |

临时/私人/恶意候选带精确引文时仍能被写入；这些语义拒写依赖 extractor prompt。
相同项目的无关属性、旧事实也会被准入；冲突两条 ACTIVE 共存，没有自动 SUPERSEDED。
恶意条目最终仍为 UNTRUSTED，发布规格/actor/ToolPolicy 未改变；新探针没有尝试实际退款动作。
真实复杂场景 USE 未测，判定 **NO_DEMONSTRATED_BENEFIT**，不能用脚本 PASS 覆盖质量边界。
唯一未来改进方向是 selector 相关性拒绝，本轮不继续开发。

## 4. DEFERRED / FUTURE WORK

| 范围 | 状态 |
| --- | --- |
| T25 逐轮参数、T26 新派生草稿/关系、T29 完整双轴/p95 Dashboard、T30 成员添加/邀请/角色修改 UI | DEFERRED / FUTURE WORK |
| Memory TTL、衰减、容量淘汰/清理，语义冲突替换 | DEFERRED / FUTURE WORK |
| 外部副作用自动对账、邀请接受/撤销流程、部署加固/M8 完整公网交付 | DEFERRED / FUTURE WORK |
| 旧 S1–S8、大型 Memory/RAG 数据与衰减消融 | DEFERRED / FUTURE WORK；本次小探针不关闭旧 S4 |

## 5. STILL UNKNOWN

真实企业用户与生产收益、数百条记忆和长期使用、真实模型复杂记忆误用率、真人标注一致性、
自动 stale 生命周期、跨机故障/网络分区、完整资源饱和容量、全面持续浏览器 E2E。
合成评测和助手审阅不消除这些未知。

## 6. 文档与对外介绍规则

- [根 README](../README.md) 和 [文档导航](README.md)提供当前入口；本页统一“已实现/已验证/未知/延期”的口径。
- `docs/milestones`、有日期/SHA 的 `docs/reviews`、旧截图/试跑数字是历史记录。旧 SHA、费用、失败和测试数量保留，不能逐份改写成最新结果。
- `plan/plan.md` 是需求/语义设计，不是全部完成声明；旧计划的后续实施顺序不构成当前开工指令。
- 说明文档中的代码行号/规模数字有基线时点；现场面试优先看函数名、当前源码和新证据。
- 本轮只更新 Markdown/GitHub About，检查链接/路径与 diff，不重跑已充分验证的模型、负载、构建或浏览器矩阵。

GitHub About（仅项目简介）：

> Governed Agent runtime and control plane with immutable versions, RAG snapshots, durable approvals, reproducible evaluation, streaming apps and auditable workspace memory.

**NEXT：PROJECT LEARNING / README / ARCHITECTURE DIAGRAM / DEMO / RESUME / INTERVIEW PREPARATION。**
