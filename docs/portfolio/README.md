# AgentHub 求职与面试入口

2026-10-09，源码基线 `a12e8ab`。这是现有 [报告](../report/README.md)、[53 题深挖手册](../report/interview/README.md)和 [学习文档](../learning/README.md)的紧凑入口，不是另一套教材。材料已整理不代表作者已掌握代码；个人贡献、开发时长、AI 使用和实际参与范围必须按可核实事实填写。

## 1. 项目介绍

### 30 秒版

AgentHub 是一个围绕工具治理、人工审批和可复现评测的 Agent Runtime 与控制平面。应用共用 Runtime，模型负责提议，执行前由发布工具策略检查；审批挂起后保存 checkpoint，批准后恢复同一个 Run。最值得展开的是不确定写入如何保留证据，以及历史知识/配置变化后如何解释结果。项目有集成与合成模型实验，尚未证明企业生产规模。

### 2 分钟版

我用这个项目研究 Agent 从“能调用工具”到“执行过程可控”的工程问题。四类应用复用 Thread/Run/Artifact，把 prompt、工具和展示差异留在应用层。控制平面发布不可变 AgentVersion；Runtime 装载实际知识和 Memory 输入、经过上下文预算，再调用模型。

模型提出工具调用后，READ + NEVER 可以自动执行，其余需要审批。审批决策与执行状态分开保存，恢复原 Run 和 checkpoint，而不是重新发起并清空预算。逻辑动作身份用于约束重复执行，但远端副作用无法声称 exactly-once；派发后不知结果时保留 UNKNOWN_OUTCOME，Run 进入 NEEDS_ATTENTION。

我会用 Incident 的调查→回滚提议→审批→恢复链解释这些设计，再展示冻结实验与失败记录。模型客服实验使用合成数据，不是线上收益；Memory 的机制与质量分开，11 场景探针发现无关与矛盾事实仍可准入。本人具体负责的模块和借助 AI 的部分，需要补充真实贡献说明。

### 5 分钟版（按时间展开，别背长答案）

| 时间 | 讲什么 | 打开什么 |
| --- | --- | --- |
| 0–0:30 | 上面的定位与治理问题 | [根 README](../../README.md) |
| 0:30–1:15 | 模块化单体+worker，应用差异不进入运行时 | [系统图](../architecture.md#system-architecture) |
| 1:15–2:30 | tool proposal / policy / interrupt / 同一 Run resume | [生命周期图](../architecture.md#governed-agent-run-lifecycle)、[policy](../../packages/tools/policy.py) |
| 2:30–3:15 | 决策/执行分离，逻辑 ID，不确定写入 | [ApprovalService](../../packages/approvals/service.py)、[MCP runtime](../../packages/mcp/runtime.py) |
| 3:15–4:15 | 冻结版本/有效快照/实验身份；证据与合成评测 | [验收索引](../reviews/README.md) |
| 4:15–5:00 | 一项真实失败与取舍；个人贡献、未知和边界 | [Memory 质量](../reviews/AgentHub-closure-memory-quality-20261005.md) |

选择自己亲自理解的一条失败链说明，回答“为何这样设计”和“仍不能保证什么”；不要用所有模块名填满五分钟。

## 2. 简历项目描述（按真实贡献裁剪）

以下描述项目能力；提交简历前只保留能解释源码、复现机制且自己实际参与的条目，不默认独立实现所有内容。不可补造企业用户、生产流量、商业收益或劳动归属。

### AI Agent / AI 应用方向

- AgentHub：基于 FastAPI、LangGraph 适配器与 PostgreSQL 的受控 Agent Runtime，Research/Incident/Analyst/Support 共用会话和产物链路。
- 通过不可变 AgentVersion、发布工具修订、策略审批和 checkpoint 恢复管理执行；不确定外部写入记录 UNKNOWN_OUTCOME / NEEDS_ATTENTION。
- 将 RAG/Memory 输入快照与上下文预算结合，提供固定引用、运行观测及冻结数据集/实验身份；记忆质量限制有独立探针记录。
- 项目包含 60 条合成客服任务、两个变体各三次的 360 trial 实验，以及 144 次 RAG 检索生成对照；结果按冻结报告解释，不标作企业线上提升。

### Python 后端方向

- 采用模块化单体+Celery worker，API/Application/Contracts/Adapters 分层，PostgreSQL 作为业务状态真相源，Redis 为任务队列，Qdrant 为检索索引。
- 实现范围包含多租户 RBAC、版本化规格规范哈希、审批决策/执行分离、逻辑动作身份和数据库并发守卫，外部结果不确定时禁止静默重试。
- 提供 checkpoint 同 Run 恢复、持久生命周期事件与 SSE 重连；异步知识入库具有可重复执行、对账及历史修订留存机制。
- 工程证据包含 PostgreSQL 集成、审批 crash 窗口/安全隔离测试和受控负载演练；不承诺跨机 exactly-once、饱和容量或生产 SLA。

## 3. 最重要的 12 道追问

每题先画链路或打开函数，再用自己的话回答。实验一栏是已有可核查文件；本轮未复跑，不代表个人已完成学习实验。

| 问题 / 既有题库 | 核心思路 | 真实代码入口 | 可验证实验/证据 | 容易说错 |
| --- | --- | --- | --- | --- |
| 1. Agent Loop 如何运行？ / 运行时手册 | prepare→model→proposal→policy→execute→observation→model/finish | [_AgentRunGraph.model / policy](../../packages/agent_runtime/runtime.py) | [核查入口](../../tests/integration/test_m5a_approval_runtime.py) | 不是固定四个工具顺序，也不是每个 Thread 只有一个 Run |
| 2. 与 LangGraph 什么关系？ / 架构手册 | 框架负责图与 checkpoint，AgentHub 定义业务策略/状态 | [compile_agent_graph](../../packages/agent_runtime/adapters/langgraph/runtime.py) | [核查入口](../../tests/integration/test_m5a_checkpoint_runtime.py) | checkpoint 不自动保证远端副作用 exactly-once |
| 3. 为什么冻结 AgentVersion？ / 架构手册 | 规范 JSON/hash、不可变规格，历史输入另冻结 | [AgentPublishService.publish](../../packages/agent_runtime/publish.py) | [核查入口](../../docs/report/03-核心机制详解.md) | LATEST 策略不是发布时永久锁定每个未来知识条目 |
| 4. 工具治理在哪里？ / 运行时手册 | 发布修订、参数校验、ToolPolicy、ActionRuntime | [ToolRuntime.execute](../../packages/tools/runtime.py) | [核查入口](../../tests/integration/test_enhancement3bc_mcp_write_approval.py) | 远端工具 annotation 不能代替工作区治理 |
| 5. READ / WRITE 的边界？ / 运行时手册 | effect 与 risk 分开；READ+NEVER 才 auto | [ToolPolicy.decide](../../packages/tools/policy.py) | [核查入口](../../tests/integration/test_enhancement3bc_mcp_read_runtime.py) | 风险 LOW 不是当前 policy 独立放行条件 |
| 6. 审批怎么恢复？ / 运行时手册 | 独立决定/执行状态、interrupt、原 Run 恢复预算 | [AgentRunService.resume](../../packages/agent_runtime/runtime.py) | [核查入口](../../tests/integration/test_approval_usage_resume.py) | 拒绝不必然整 Run 失败；批准不等于执行成功 |
| 7. 重复/不确定写入？ / 故障手册 | 逻辑身份和执行抢占；派发后未知不重试 | [create_or_get / claim_execution](../../packages/approvals/service.py) | [核查入口](../../tests/integration/test_m5a_approval_runtime.py) | provider call ID 不是逻辑身份；不承诺外部 exactly-once |
| 8. SSE 断线怎么办？ / 故障手册 | 客户端游标与持久生命周期事件重放，附着原 Run | [attach_stream](../../packages/agent_runtime/runtime.py) | [核查入口](../../tests/unit/test_durable_run_stream.py) | 不是全部 delta 永久落库；断线不等于重开 Run |
| 9. RAG 与预算如何衔接？ / RAG 手册 | 冻结知识、可信边界、最终 context admission | [ContextBudgetPolicy.admit_context](../../packages/agent_runtime/context_budget.py) | [核查入口](../../docs/reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md) | 检索命中不等于准入，更不等于真实答案正确 |
| 10. Memory 为何快照？ / C15 | 记录当时输入，停用不删除历史，hash 校验 | [select / load / touch](../../packages/memory/store.py) | [核查入口](../../tests/unit/test_b2_memory_snapshot_replay.py) | 机制通过不等于有收益；5/8 禁止召回是质量问题 |
| 11. Evaluation 如何避免自证？ / 评测手册 | DEV/HOLDOUT、发布不可变数据、独立 evaluator/version 和失败记录 | [finalize_experiment](../../packages/evaluation/experiments.py) | [核查入口](../../docs/reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md) | 合成数据/助手审阅不等于真人标注一致性 |
| 12. 最关键的取舍？ / 复盘手册 | 模块化单体便于事务和核查；外部不确定宁可人工处理 | [execute_write](../../packages/mcp/runtime.py) | [核查入口](../../docs/reviews/post-M5-independent-review-closure.md) | 不把单机受控负载外推为生产 SLA |

题库继续追问：[项目介绍/架构](../report/interview/01-项目介绍与架构取舍.md)、[运行时/审批/故障](../report/interview/02-运行时审批与故障拷打.md)、[RAG/Memory/评测](../report/interview/03-RAG记忆与评测拷打.md)、[工程复盘](../report/interview/04-工程复盘与现场追问.md)。不新增另一套题库。

## 4. 展示前的个人准备

- 用 [学习入口](../learning/README.md)跟完一个 Run，在源码中解释分支，保留自己的预测/观察记录。
- 亲自完成 [主 Demo](../report/07-现场演示脚本.md)；不能运行时明确使用历史截图+静态代码展示，不声称现场验证。
- 写真实贡献说明：负责什么、复核什么、AI 帮助什么、哪些不能独立解释；简历只使用已掌握的部分。
- 核对 [当前边界](../current-state.md)，遇到未测生产/Memory 问题直接解释证据不足。本轮到此收口，不扩展新功能。
