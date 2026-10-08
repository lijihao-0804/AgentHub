# AgentHub 学习入口：沿一次 Run 理解系统

2026-10-09；当前源码基线 `a12e8ab`。只整理已有四份教材，不新增学习系列。
[系统工程证据](../reviews/README.md)已经存在；下面是**用户未来亲自执行的学习任务，尚未执行、未标 PASS**。读完材料也不能替代实际掌握。个人记录放在自己的 `.scratch`/笔记，不改业务实现或正式证据。

## 先做什么

先打开 [01 代码导航](01-codebase-navigation.md)，再按 [02 一次 Run](02-one-run-end-to-end.md)跟 Incident。旧章节行号是历史基线，用当前函数名定位；流式 UI 入口还需看 `turns/stream`，不要只看同步 `submit_turn`。

学习主线：**一次 Run → Runtime → Policy → Approval → Durability → Domain Model → RAG / Memory → Evaluation**。

## 执行顺序与理解标准

所有修改只作用于独立 lab 工作区的新草稿/版本、测试 fixture 或临时配置，不修改生产、历史发布版、业务源码或测试断言。先预测，再读函数，最后记录实际结果；没有运行的项写“未执行”。

| 阶段 / 教材 | 首读真实函数与调用链 | 改哪个非生产变量 | 应观察什么 | 如何恢复 | 理解标准 |
| --- | --- | --- | --- | --- | --- |
| 1 一次 Run / [01](01-codebase-navigation.md)、[02](02-one-run-end-to-end.md) | [ThreadService.submit_turn](../../packages/threads/service.py)→prepare_run→execute_prepared_run；流式查 [routes](../../apps/api/routes/threads.py) | 新 lab Turn 的 client_token，重复同一 token | Turn/Run 关联与复用；未关联时可能 409 in-progress | 用新 token 做下一条，保留前次 Run | 能说出 Thread/Turn/Run 的区别与分段提交窗口 |
| 2 Runtime / [02](02-one-run-end-to-end.md)、[04](04-runtime-labs.md) | [AgentRunService.run / _AgentRunGraph.model](../../packages/agent_runtime/runtime.py)→tool_proposal→policy→observation | 新 lab 草稿 max_tool_calls，发布新版本 | 工具预算或循环停止，Run 步骤/失败码 | 发布另一 lab 版本恢复原值，不修改旧版 | 能画循环与无工具/预算/取消退出，不能只背节点名 |
| 3 Policy / [04 Lab 1/2](04-runtime-labs.md) | [ToolPolicy.decide](../../packages/tools/policy.py)→ToolRuntime/ActionRuntime | 新 lab ToolRevision 的 approval_policy；READ/NEVER→ALWAYS | READ 也可进入审批 | 新建修订恢复 NEVER，再发布新 AgentVersion | 明确 effect/risk/policy 三者及当前 auto 条件 |
| 4 Approval / [02](02-one-run-end-to-end.md) | [create_or_get / decide / claim_execution](../../packages/approvals/service.py)→[resume](../../packages/agent_runtime/runtime.py) | lab 同一审批选择 approve/deny，分别在不同 Run 做 | 两种决策、执行状态和 observation；同一个 run_id | 不改已决审批，下一 Run 新建提议 | 能区分批准/成功，解释拒绝不是必然 Run FAILED |
| 5 Durability / [04](04-runtime-labs.md) | [checkpoint adapter](../../packages/agent_runtime/adapters/langgraph/checkpoint.py)→resume；[MCP execute_write](../../packages/mcp/runtime.py) | lab WAITING 时重启 API；另轮 Ops rollback_mode=hang | 原 Run 恢复；已派发未知结果进入 NEEDS_ATTENTION | 停 lab 进程，移除 hang/私网进程变量；保留未知记录，不重试确认 | 解释 checkpoint 与副作用的两个持久化窗口，不承诺 exactly-once |
| 6 Domain Model / [03](03-domain-model-lifecycle.md) | [AgentPublishService.publish](../../packages/agent_runtime/publish.py)→[models](../../packages/agent_runtime/models.py) | lab 草稿 prompt，发布两版本 | spec/hash 不同；旧版和旧 Run 保持原输入身份 | 保留两版，用原版创建新 Run 对照 | 说清可变草稿/不可变版本/有效快照，LATEST 何时解析 |
| 7 RAG / Memory / [04 Lab 8/11/12](04-runtime-labs.md) | [_AgentRunGraph.prepare](../../packages/agent_runtime/runtime.py)→[Memory store.select/load](../../packages/memory/store.py)→[admit_context](../../packages/agent_runtime/context_budget.py) | lab 新版开/关长期记忆；停用一条 lab memory；绑定新的知识快照 | 新 Run 输入变化，旧快照回放；selected 与 admitted 分开 | 新 lab 版本关记忆，reactivate 仅已停用条目；不删除历史 | 能解释 UNTRUSTED/hash/预算、两 ACTIVE 冲突与相关性不足 |
| 8 Evaluation / [04 Lab 13](04-runtime-labs.md) | [publish_version](../../packages/evaluation/service.py)→[finalize_experiment](../../packages/evaluation/experiments.py)→runner | 隔离小型 DEV fixture 的草稿/变体（不用 HOLDOUT 调参） | 发布不可变数据与冻结输入身份、失败记录 | 不改冻结实验，下一实验新草稿；停 worker 后移除临时配置 | 解释合成业务实验、脚本消费者、真实 LLM/真人证据的区别 |

## 隔离与成本

完整启动参考 [Hero Demo §0](../report/07-现场演示脚本.md#0-实际启动条件与安全范围)。必须先准备独立数据库、Redis namespace、blob 路径和 lab 工作区，再运行迁移/checkpoint setup。API/worker 使用同一 lab 配置，测试目标也单独命名；不能把默认开发库“可以随便改坏”作为安全依据。

不覆盖共享 `.env`，使用专属终端的环境覆盖/临时启动配置。学完关闭自己创建的进程、移除临时配置/私网开关；保持用户原服务、原配置与历史证据。不要删除共享数据。第一次只做不产生费用的代码/fixture核查；真实模型实验需自行确认预算与凭据。当前任务未运行这些实验。

| 已有教材 | 用法 |
| --- | --- |
| [01-codebase-navigation](01-codebase-navigation.md) | 文件地图与断点；按函数名找当前代码 |
| [02-one-run-end-to-end](02-one-run-end-to-end.md) | 单次调查/审批链，补查流式入口 |
| [03-domain-model-lifecycle](03-domain-model-lifecycle.md) | 对象可变/不可变与保留语义 |
| [04-runtime-labs](04-runtime-labs.md) | 具体实验步骤；应用本页隔离要求与当前边界，旧时间/行号仅参考 |

## 如何证明自己理解了

每个阶段写六行：预测、实际调用链、变量、观察、恢复、剩余疑问。能在不读答案的情况下解释一个失败分支，并找到函数与历史证据，才开始用 [求职讲述](../portfolio/README.md)。未完成的实验保持未执行；不因教材齐全就说“已掌握全部代码”。

[Memory 最新质量边界](../reviews/AgentHub-closure-memory-quality-20261005.md)：默认关闭与冻结机制已存在，但临时/私人/恶意语义拒写仍依赖 prompt，5/8 禁止召回场景准入，真实复杂 USE 未测。不要把教材中的预期当作保证。
