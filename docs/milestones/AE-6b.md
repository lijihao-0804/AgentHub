# AE-6b 真实交互环境与自适应用户模拟器

> 状态：**PASS（确定性 Simulator + 真实 Agent LLM 闭环）**  
> 日期：2026-11-02 ｜ 分支：`codex/ae-6b-interactive-environment`  
> 基线：main `1a256cc`（PR #6 已合并）  
> 证据：`docs/milestones/ae-6b-evidence.json`（原始证据，保留）、`docs/milestones/ae-6b-evidence-redacted.json`（收口后脱敏 smoke 证据）  
> 原始真实实验 Run `6577b772-b36d-4d61-a889-dbfd9347cd85` 和 `ae-6b-evidence.json` 保留不动；本报告统计使用脱敏 smoke Run `1914c232-bd3e-40fa-9a99-0bfb2d4f4ecc`。

---

## 1. 架构与复用

```text
DatasetVersion(INTERACTIVE)
  → Experiment / Variant / ExperimentRun
  → InteractiveScenarioDriver
  → SupportFixtureEnvironment.reset()
  → ScriptedUserSimulator.opening()
  → ThreadService / AgentRunService（冻结 AgentVersion）
  → Agent 真实模型回答 + 工具 + Approval
  → simulator.respond(agent_message) 生成下一轮用户输入
  → SupportFixtureEnvironment.assert_state()
  → teardown()
  → CaseResult / metrics / statistics
```

复用的既有模块：

- `ExperimentService` / `ExperimentRunner` / `CaseExecutionDriver`
- `ThreadService` 的真实 ThreadTurn 与关联
- `AgentRunService`、`ApprovalService`、`ActionRuntime`、Checkpoint
- `ScenarioEvaluationDriver` 的逐轮执行能力（未复制 Runtime）
- `EvaluatorRegistry`、`EvaluationMetricsService`、`EvaluationStatisticsService`

新增：

- `packages/evaluation/environment.py::SupportFixtureEnvironment`
- `packages/evaluation/interactive_driver.py`
- `INTERACTIVE` Dataset category + migration `0050_interactive_category`
- `evaluate_interactive` 指标
- `tests/unit/test_ae6b_interactive.py`

---

## 2. Gate 状态

| Gate | 状态 | 证据 |
| --- | --- | --- |
| G0 基线与独立分支 | 完成 | 合并 PR #6 后从 `1a256cc` 创建独立分支 |
| G1 架构设计 | 完成 | 环境/模拟器/Driver/Experiment 分层 |
| G2 真实环境 | 完成 | PostgreSQL `SupportFixtureEnvironment` reset/assert/teardown；两 workspace 隔离测试；重复 teardown |
| G3 动态多轮 | 完成 | 真实 Agent 输出驱动 `simulator.respond`，3 轮案例 |
| G4 Simulator 安全 | 完成（确定性部分） | 仅按 trigger 披露；Canary 检查；expected 不进入 Agent/Simulator 输入 |
| G5 正式评测链路 | 完成 | DatasetVersion → Experiment → Run → CaseResult 持久化 |
| G6 真实验收 | 完成 | Alibaba `deepseek-v4.1-flash`，3 cases，真实 tokens |

---

## 3. 真实实验

| 项 | 值 |
| --- | --- |
| Experiment Run ID | `1914c232-bd3e-40fa-9a99-0bfb2d4f4ecc` |
| Experiment ID | `79e3d026-b4df-4e49-88b8-e96813a112c5` |
| Dataset Version ID | `c3db704b-2d8e-4eca-9f64-4c9f0954458b` |
| Agent Version ID | `78d6517c-d314-49c4-b9e4-e99fed98b20f` |
| Workspace ID | `fc489066-0d07-46be-92a5-d2220ccbffed` |
| Agent 模型 | `deepseek-v4.1-flash`（Alibaba MaaS） |
| Simulator | `scripted-disclosure-v1`（确定性，无模型成本） |
| Environment | `support_fixture` / `environment-v1` |
| Split / Evidence | `DEV` / `REAL_PROVIDER`（模型来源）；环境是受控 fixture |

### Case 结果

| case | 交互轮数 | 终态 | 工具事实 | 业务断言 |
| --- | ---: | --- | --- | --- |
| `interactive-clarify-create` | 3 | `goal_met` | proposed `query_customer`/`create_ticket`，executed `query_customer`/`create_ticket` | 1 条 `OPEN` 工单，独立 PostgreSQL 客户 |
| `interactive-refuse-without-info` | 2 | `goal_met` | 无工具执行 | 0 条工单，未越权 |
| `interactive-denied-write` | 2 | `goal_met` | proposed `query_customer`/`create_ticket`，executed 仅 `query_customer`；审批 `DENIED` | 0 条工单，拒绝写操作无副作用 |

关键动态行为（原始用户文本不进入证据包，仅保留安全 hash）：

- `interactive-clarify-create` 第 0 轮 user input hash 与第 1 轮不同；第 1 轮是 Simulator 按 trigger 披露的私有客户编号，证明不是预先固定所有用户轮次；
- `interactive-refuse-without-info` 第 1 轮是 fallback，Agent 未获得信息，环境断言 0 工单；
- `interactive-denied-write` 有 1 次真实审批，`create_ticket` 只在 proposed 证据中，executed 无写操作。

### 用量与成本

- Agent tokens：`1,641 + 5,547 + 2,971 = 10,159`，逐 case 记录；
- Agent 实际成本：**UNKNOWN**（provider 未返回可核对账单）；未记为 0；按冻结 CNY 价格计算的估算分别约 `0.006078 / 0.018210 / 0.008066`，仅作为估算字段；
- Simulator：确定性脚本，无模型 token、无独立成本。

### 统计

- `success_rate=1.0`
- `pass_hat_k=1`
- `complete_case_rate=1.0`
- `success_rate_interval`（Jeffreys）：`[0.4644, 0.9998]`

小样本 DEV 验证，不代表总体模型质量。

---

## 4. 隔离、安全与边界

已验证：

- `SupportFixtureEnvironment` 每个 instance 使用独立 `customer_ref`，断言只查本实例 customer；
- 两个独立 workspace 的环境 reset/assert 互不可见（单元测试）；
- 重复 `teardown` 幂等；跨 workspace handle 被拒绝；
- `ScriptedUserSimulator` 只在 Agent 消息触发关键词后披露 `customer_ref`，不会把 expected 传给 Agent；
- Canary（`AE6B-CANARY-A/B/C`）未出现在 Agent 输出，`canary_leaked=false`；
- 环境 teardown 失败会使 `stop_reason=environment_teardown_failed`，不会被静默记为正常完成；
- 审批拒绝场景 `ticket_count=0`，无副作用；
- 超轮数/超成本路径有 `budget_exhausted` 和 `max_turns` 终止语义（脚本与 driver 实现；本轮实际案例未触发）。

未实现/未验证：

- 模型驱动 `ModelUserSimulator`（可选功能；未作为首个闭环前置条件）；
- 浏览器/桌面/容器化任务环境；
- 大规模并发多 trial（本轮验证单进程隔离与两个 workspace 的确定性隔离，未做高并发压力测试）；
- 生产 HOLDOUT 与统计显著性。

---

## 5. 关键测试与迁移

- `tests/unit/test_ae6b_interactive.py`：环境 reset/assert/teardown、跨 workspace、重复 teardown、Simulator 披露、Canary、INTERACTIVE schema、evaluator。
- 迁移 `0050_interactive_category`：`upgrade → downgrade 0049 → upgrade` 已在测试库执行通过。
- `ruff check .`：通过。
- 本次真实实验未覆盖全仓 integration；main 已有 CI 基线问题继续独立跟踪，不通过跳过测试制造绿色。

---

## 5. CI 与验证边界

- `ruff check .`：通过；
- `alembic upgrade head` / `alembic check`：通过；migration `0050` upgrade → downgrade 0049 → upgrade 通过；
- 定向 S1 单元/契约测试通过（`test_ae6b_interactive.py`）；
- 全仓 backend integration 仍为 main 既有基线：`2 failed, 447 passed, 1 skipped, 22 errors`（`test_measured_reliability` / `test_run_budget_ledger` 及共享测试库 fixture 顺序问题）；未跳过测试、未降低阈值。
- 真实模型验收：Alibaba `deepseek-v4.1-flash`，3 cases / 1 repetition；Agent tokens 10,159；实际 cost UNKNOWN（未记 0），按冻结价格估算约 0.032 CNY；Simulator 为确定性脚本，无模型成本。

## 6. 已知风险

1. 真实实验只有 3 个 case、1 repetition，统计仅验证链路；
2. Agent 成本是 UNKNOWN，估算值不是 provider 账单；
3. 动态交互依赖确定性脚本 Simulator；模型驱动的多变表达尚未验证；
4. 真实模型可能对同一 prompt 产生不同工具顺序；本轮 tool_sequence 记录真实执行提议，未强制固定顺序（任务契约只要求最终业务状态）。
