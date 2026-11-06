# FINAL-CLOSE-1 范围可达性与演示复用核对

- 核对日期：2026-11-06
- 工作树：`.worktrees/ae1/.agents/worktrees/codex/final-closeout`
- 分支：`codex/final-closeout-20261106`
- 源码：`27d78476a0d55a19ca80b2c1e71dd678845ea8ee`（main `5a374621076581cccafb1f686d04c1ad10f4cc4a` + R1）

本文只记录一次限定范围的源码装配核对。没有启动 API、worker 或 MCP，没有连接 PostgreSQL/checkpoint，没有调用模型，也没有运行测试。因此，下文只有两种结论：当前源码能证明什么，以及主代理在隔离演示中还必须现场记录什么。本文没有预写本次批准、拒绝或同 Run 恢复已通过。

## 1. 结论

选定的普通 Incident Run 路径是：

`POST /api/v1/workspaces/{workspace_id}/threads/{thread_id}/turns/stream`

它进入 `apps/api/routes/threads.py::stream_turn`。在该路径上，正式 API 和正式 Celery worker 在构造 `AgentRunService` 时都没有传入 `model_attempt_budget_factory` 或 `local_summary_experiment`：

- API composition：`apps/api/agent_runtime_dependencies.py:29-88`
- worker composition：`apps/worker/tasks/agent_runs.py:221-261`

API 和 worker 也都没有传入自定义 `model_gateway_factory`。因此 `AgentRunService` 使用自己的默认 `SqlAlchemyModelGateway`，该 gateway 的 `attempt_budget` 保持为 `None`（`packages/agent_runtime/runtime_service.py:95-127`）。

在这个正式装配下：

| 已知问题 | 普通 Incident Run 是否启用 | 当前源码结论 |
| --- | --- | --- |
| B02 预算 attempt 的 UNKNOWN 持久化失败 | 否 | 没有 Run attempt budget reservation，相关 unknown-outcome 处理分支不可达；问题没有因此被证明已修复。 |
| B03 repository response observation / `record_response` 失败 | 否 | 没有本地摘要研究 permit，也没有研究 attempt wrapper；`record_response` 分支不可达。 |
| B05 无效响应在 reservation 失效后触发记账漏洞 | 否 | 没有 attempt budget，`_reserve_attempt` 返回 `None`，因此该 reservation 路径不可达；缺陷代码仍在。 |

这条结论针对“正式启动 composition”。若演示代码手工给 `app.state.agent_run_service` 赋值，或在 worker composition 中 monkeypatch/替换 provider seam，就必须把它标为测试或研究装配，不能用它证明正式启动启用了或关闭了这些能力。

B02、B05 也不能与外部工具动作的 `UNKNOWN_OUTCOME` 混为一谈。普通 Incident 路径仍有 MCP action 的审批、执行和未知结果处理；这里判定未启用的只是模型 attempt budget ledger 的三条故障路径。

## 2. 正式调用链与配置条件

### 2.1 API 进程内执行

1. `apps/api/main.py:1-3` 调用 `create_app()`；`apps/api/app.py:81-93` 保存 settings 和 session factory 状态，并注册 Thread 路由。
2. `apps/api/routes/threads.py:46-48` 的 `_service()` 调用 `get_production_agent_run_service()`。
3. `apps/api/agent_runtime_dependencies.py:29-88` 在 `app.state.agent_run_service` 为空时构造正式服务，并缓存在 app state。
4. `stream_turn()` 用 `ThreadService.resolve_agent_version()` 取该 Thread 对应 Agent 的最新版本号（`packages/threads/service.py:337-366`），再用 `ensure_stream_run()` 取得唯一 Turn/Run（`packages/threads/service.py:582-617`）。
5. 当 `AGENTHUB_RUN_EXECUTION_IN_WORKER=false` 时，路由走 `run_service.stream(...)`（`apps/api/routes/threads.py:241-248`）。

`Settings.run_execution_in_worker` 默认是 `false`（`packages/core/config/settings.py:141-144`）。`testing` 默认也是 `false`（`packages/core/config/settings.py:27`），但改变它只会改变 API lifespan 是否自动创建数据库/Redis 等资源（`apps/api/app.py:55-65`）。`testing=true` 不会创建一个 budget factory、测试 provider 或摘要研究 permit。

### 2.2 Celery worker 执行

1. 当 `AGENTHUB_RUN_EXECUTION_IN_WORKER=true` 时，`stream_turn()` 将已准备的 Run 通过 `queue.enqueue(...)` 交给 `agenthub.execute_agent_run`（`apps/api/routes/threads.py:222-240`；任务注册于 `apps/worker/tasks/agent_runs.py:65-77`）。
2. `_execute_agent_run()` 重新加载 Run、重新授权，再调用 `_build_service(settings, factory)`（`apps/worker/tasks/agent_runs.py:149-216`）。
3. `_build_service()` 与 API 一样装配真实 MCP、审批、checkpoint、Thread context、Memory 和 artifact 依赖，但没有传入 `model_attempt_budget_factory`、`local_summary_experiment` 或自定义 gateway（`apps/worker/tasks/agent_runs.py:221-261`）。

因此要现场核验的实际配置条件只有“Run 在 API 内执行还是交给 worker”这一处分流。无论走哪一支，当前正式源码都没有接 attempt budget 或本地摘要研究 permit。`AGENTHUB_TESTING` 不改变这个事实。

### 2.3 模型轮次的源码事实

`packages/agent_runtime/runtime_graph.py:2636-2682` 取得 gateway 后，只有
`service.model_attempt_budget_factory is not None` 时才绑定本次 Run 的 attempt budget。只有同时存在 `local_summary_experiment` 时，才再把预算包装成研究 adapter。

`packages/model_gateway/gateway.py:620-656` 中，`attempt_budget is None` 时 `_reserve_attempt()` 直接返回 `None`，`_mark_unknown_outcome(None)` 也直接返回。这补齐了“默认参数为空”之外的运行时分支证据。

## 3. B02/B03/B05 的精确可达条件

### B02：预算 attempt 的 UNKNOWN 持久化失败

可达条件：

- `AgentRunService.model_attempt_budget_factory` 非空；
- runtime graph 为该轮成功绑定 `gateway.attempt_budget`；
- provider 调用前的 `reserve_attempt()` 建立 reservation；
- 随后发生必须记录 `UNKNOWN_OUTCOME` 的异常，并执行 `mark_unknown_outcome(reservation)`。

正式 API 和正式 worker 都没有满足第一项；默认 gateway 的 `attempt_budget` 也不是由 AgentVersion 自动启用的。当前普通 Incident 路径在源码上不进入 B02 的 persistence 分支。结论仅限“不启用”，不能说 B02 已修复。

### B03：`record_response` / 本地研究 observation

可达条件：

- 既是 attempt-budget 路径；
- 又注入匹配本地数据库、Thread、AgentVersion 和 Run 集合的 `LocalSummaryExperimentPermit`；
- gateway 的普通 budget adapter 被 `wrap_model_attempt_budget()` 包装；
- provider 返回后存在可调用的 `record_response`。

普通 `RunBudgetAttemptAdapter` 不提供 `record_response`。实际实现位于本地研究包装器中（`packages/agent_runtime/local_summary_experiment.py:636-659`），而正式 API/worker 没有创建或传入 permit。`LocalSummaryExperimentPermit` 的模块说明还明确写着 API 和 AgentVersion 发布路径不会创建该 capability（`packages/agent_runtime/local_summary_experiment.py:1-6`）。

`runtime_graph.py:2720-2726` 即使遇到 `record_response` 异常也只记录 warning，不会把它转换成新的派发。该行为是否足以满足 B03 的合同要求不在本次范围；当前只能确认正式 Incident 路径没有调用该 hook。

### B05：无效 response 后的 reservation 失效

可达条件：

- gateway 已经绑定非空 attempt budget；
- `_reserve_attempt()` 返回了 active reservation；
- provider 返回了一个 response，随后 `_validate_usable_response(response)` 失败。

缺陷代码仍位于：

- 非流式：`packages/model_gateway/gateway.py:276-277`
- 流式：`packages/model_gateway/gateway.py:478-480`

两处都在校验 response 前把 `active_budget_reservation` 设为 `None`。但正式 Incident composition 没有 attempt budget，reservation 本身为 `None`，因此当前路径不会触发 B05。不能把“未启用”写成“已修复”，也不能据此宣称模型财务对账保证成立。

## 4. 有限金额、摘要与研究开关

### 4.1 有限金额门

Interactive Evaluation 的 case 配置一旦出现 `max_cost_cny`，`packages/evaluation/interactive_driver.py:686-699` 会在派发前抛出 `INTERACTIVE_COST_RESERVATION_UNAVAILABLE`。当前 evaluation UI 也明确显示“金额硬上限暂不支持，收费调用尚未派发”（`apps/web/app/evaluations/runs/[runId]/run-detail-client.tsx:241-244`）。

这是 Interactive Evaluation 的拒绝门。普通 Thread Turn 请求 schema 只有 `input_text` 和 `client_token`（`apps/api/schemas/threads.py:52-58`），没有 `max_cost_cny`。因此普通 Incident 路径没有“用户声明金额后由 case budget 拒绝”的入口；本次也不接 case budget。

### 4.2 自动摘要发布门

任何新发布配置只要 `resolved_spec.runtime.summary_policy` 非空，都会在
`packages/agent_runtime/publish.py:428-436` 被
`SUMMARY_QUALITY_GATE_NOT_MET` 拒绝。

但主代理仍须检查所选 AgentVersion 的实际 `resolved_spec`，因为历史版本可能早于该发布门；不能只根据“当前 publish() 会拒绝”推断已有版本没有 policy。选定用于演示的版本应当没有 `summary_policy`，或至少不是启用状态。若历史版本带 policy，必须重新判断摘要相关运行时路径，不能沿用本文的普通路径结论。

`summary_request_counter` 与本地研究 permit 也不是一回事。`AgentRunService` 可能根据 tokenizer 配置加载普通 summary counter（`packages/agent_runtime/runtime_service.py:151-156`），但 `local_summary_experiment` 仍为空；这不会创建 B03 的 `record_response` hook。

### 4.3 研究/测试开关

当前没有可把 `LocalSummaryExperimentPermit` 从环境变量直接打开的 settings 开关。全仓构造点只出现在 benchmark/unit test 代码中；正式 `apps/api` 和 `apps/worker/tasks/agent_runs.py` 都没有构造点。研究 permit 是显式代码构造的进程内 capability，并绑定 loopback 实验数据库、Thread、AgentVersion、spec hash 和有限 Run 集。

本地摘要实验如果被注入，`AgentRunService` 还强制要求共享 Run/research budget adapter（`packages/agent_runtime/runtime_service.py:139-150`）。因此不能用 `AGENTHUB_TESTING=true` 或某个普通环境变量临时“打开”B03。演示若使用了测试注入，应保留注入入口、源码路径和测试 provider 标记。

## 5. 主代理必须现场记录的事实

以下内容目前均未验证，不能提前写成通过。

### 5.1 启动与版本一致性

- 实际 API 与 worker 的启动命令、工作目录、进程环境，以及运行源码是否就是上述 HEAD。
- `AGENTHUB_RUN_EXECUTION_IN_WORKER` 的实际值；若为 `true`，记录在线 worker 和 `agenthub.execute_agent_run` 消费记录；若为 `false`，记录 Run 在 API 内执行。
- PostgreSQL、LangGraph checkpoint schema、Redis 和 MCP 均指向隔离 loopback 环境；API 与 worker 使用同一个数据库和 credential master key。
- 仅在隔离 API 进程显式开启 `AGENTHUB_MCP_ALLOW_PRIVATE_TARGETS=true` 时，记录它对应的实际进程；这是 loopback MCP 配置，不应扩大为生产默认。
- 记录 `AGENTHUB_TESTING` 的实际值。如果为了 test harness 将其设为 `true` 并手工设置 `app.state.db_session_factory`，应明确标记为测试装配，不能冒充无人工注入的正式启动。

### 5.2 AgentVersion 与 runtime

- 实际 AgentVersion ID、Agent ID、版本号、`resolved_spec_hash` 和选取时间。Thread 在每次提交新 Turn 时会重新取最新版本，因此不能只记录 Thread ID。
- 该版本实际绑定的 MCP 工具、READ/WRITE effect、risk 和 approval policy；批准演示至少需要真实 MCP 的只读调查与一个 `WRITE / ALWAYS` 提议。
- `resolved_spec.runtime.summary_policy` 为缺失或 null，或明确记录其实际值；不能只引用发布门的源码。
- 实际运行服务的 `model_attempt_budget_factory is None`、`local_summary_experiment is None`。若实际执行在 worker，应在 worker 构造的服务上也记录，而不是只检查 API。
- 确定性 provider 的显式测试标记、注入入口和 provider/model identity。现有 fixture 使用 `provider="fake"`、`model="frozen-model-a"`，并通过 `model_gateway_factory` 注入 `ScriptedGateway`；这属于 test-only composition。

### 5.3 两人审批与同 Run 证据

- 提交者与审批者的真实 user ID、组织/Workspace role 和权限集合。提交者至少需要运行能力；审批者必须具有 `approve_action`。
- `requested_by != decided_by`，并且两个账号确实不同。不能只看到“两个浏览器窗口”就宣称独立审批。
- 批准前后是同一个 `run_id`；审批决定与动作执行分别记录 `decision_status` 和 `execution_status`，不把“已批准”写成“已执行”。
- 批准路径记录远端 MCP 工具确实执行一次、返回内容和最终 Run 状态；拒绝路径记录远端工具调用次数为零、没有成功写入，并记录拒绝后的实际 Run/execution 状态。
- 拒绝或批准若造成外部动作结果不确定，应按实际状态记录 `UNKNOWN_OUTCOME`/`NEEDS_ATTENTION`，不能因为本 Demo 使用模拟回滚就推断生产安全。

### 5.4 审批权限的源码事实

`packages/approvals/service.py:203-205` 要求 `approve_action`。
`packages/control_plane/rbac.py:36-41` 只在组织 OWNER/ADMIN 权限集中加入该权限；普通 Workspace Developer 和 Viewer 没有它。

当前自审批规则是：只有非组织 OWNER/ADMIN 的提交者批准自己的动作时才被拒绝（`packages/approvals/service.py:252-259`）。因此：

- 组织 OWNER/ADMIN 当前可以自批准；
- 普通 Developer 既没有独立的 `approve_action`，也不能靠“自己审批自己”绕过；
- 普通 Incident 人工审批没有强制 distinct actor。

最短的独立演示配置是：提交者使用组织 MEMBER + Workspace DEVELOPER，审批者使用独立的组织 ADMIN/OWNER。两者必须是不同账号，并现场断言 `requested_by != decided_by`。AE6b 自动评测另有一个 distinct actor provider（`packages/evaluation/approval.py:26-67`），但那是 Evaluation 路径，不能拿它证明普通 Incident 人工审批也具有相同约束。

## 6. Hero Demo 与现有 fixture 的最短复用建议

### Hero Demo

首选产品链路仍是 `docs/report/07-现场演示脚本.md`：

- `## 0. 实际启动条件与安全范围`：隔离数据库、Redis、checkpoint、私有 MCP 开关和 Ops MCP fixture；
- `### 从空工作区配置`：固定 Ops 工具策略与两用户审批配置；
- `## 1. 主线演示（5–8 分钟）`：调查、写提案、等待审批、批准后同 Run 恢复；
- `### 可选恢复/失败展示`：对 `UNKNOWN_OUTCOME` 的说明。

该文档明确当前没有一条命令完成租户、模型和工具的自动 seed（同文件第 28 行），主代理应预留手工配置步骤。回滚目标只是 `apps/ops_mcp` 的受控模拟数据，不是真实生产部署。

### 确定性批准/拒绝 fixture

`tests/integration/test_enhancement3bc_mcp_write_approval.py` 是最短的协议级参考：

- `ScriptedGateway`（约 218-243 行）：第一次返回 MCP WRITE tool call，第二次返回最终答复；
- `service(...)`（约 277-288 行）：显式注入 scripted gateway、真实 `ApprovalService`、MCP action runtime 和 checkpoint adapter；
- 批准用例（约 292-331 行）：先 `WAITING_APPROVAL`、批准后同 Run 成功、远端只执行一次；
- 拒绝用例（约 333-361 行）：拒绝后远端调用仍为零。

该 fixture 的批准/拒绝使用同一个已带审批权限的 context，因而在当前 OWNER/ADMIN 自审批豁免下不能证明独立审批。复用时只取同 Run/取消分支和 MCP action 边界；actor 与权限应换成两个独立账号，或在引用测试中取得。

### 两用户审批 seed

`tests/support/ae6b_queue.py:59-84` 的 `QueueAgent(owner_id, approver_id)` 和
`seed_queue_agent()` 会创建独立 ADMIN approver；
`tests/integration/test_ae6b_queue.py:91-95` 明确断言
`requested_by == owner_id`、`decided_by == approver_id` 且二者不同。

可复用其用户/角色/审批 actor 构造思路。它属于 AE6b Evaluation 测试支持，不是普通 Incident 的生产服务装配，也没有证明本次普通 Incident Run。

### AE10 摘要与逐题 UI 素材

`tests/integration/test_ae10_evidence_view.py::_seed_evidence_case`（约 74-450 行）可生成：

- 两个 case result、两个 variant 和摘要列表分页素材；
- 成功、失败、未知和拒绝四类审批证据；
- owner、Viewer 和跨租户用户的真实 HTTP 权限检查；
- 不泄露 prompt、raw arguments、expected 和私有 sentinel 的安全投影。

同一个测试约 464-668 行验证摘要、逐题 evidence、分页、Viewer 结果和跨租户拒绝。它适合复用于 AE10 的 UI/权限素材，并应按计划标注为合成或历史 fixture 结果，不能写成当前候选重新跑出的模型收益。

## 7. 本轮边界

- 本次没有修改上述任何源码、fixture、启动配置或 CI。
- 本次没有执行测试，没有证明批准、拒绝、重启恢复、checkpoint、MCP 或 PostgreSQL 实际可用。
- 本次没有审阅 R2 的全部错误窗口，也没有审阅普通 Incident 路径以外的所有 `AgentRunService` 构造点。
- 本次没有实现 case budget、自动摘要、研究 runner 或任何预算能力。
- 所有运行结论都由主代理在隔离环境中产生；本文只提供源码装配前提、待记录事实和 fixture 路径。


## 8. 主代理实际装配补记（2026-11-06）

以上正文是子代理源码审阅的时点记录。随后主代理在 C1 `ca87af0` 使用
`scripts.portfolio_demo` 启动明确隔离的演示装配，`AGENTHUB_TESTING=false`、worker 执行开启，
默认 API lifespan 建立数据库/Redis。`scripts/portfolio_runtime.py::install_provider` 只在该
可选 launcher 中替换模型 gateway 为 `IncidentProvider`，不改变正式产品 composition。
它拒绝其他模型，并在 API/worker 构造时拒绝 budget/summary permit；当前两个实际进程的
runtime-scope 记录均为 false。AgentVersion 无 summary policy、Memory 未启用、max_cost_cny 为 null。

loopback MCP private-target 配置仅属于该演示进程；数据库、Redis、MCP 均为隔离 localhost 目标。
实际重启、独立账号批准同 Run、拒绝不写入及评测权限已完成，见
[现场回执](evidence/final-close-20261106/receipt.json)和[交付记录](final-closeout-evidence-20261106.md)。
B02/B03/B05 未启用且未修复；不外推金额保证或研究质量。
