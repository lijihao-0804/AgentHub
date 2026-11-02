# AE-Real-2 执行证据完整性与可靠性验证

> 状态：**PASS（执行证据与受控失败恢复验收）**  
> 日期：2026-11-02 ｜ 分支：`codex/ae-1-trajectory-evidence` ｜ PR #6  
> 证据：`docs/milestones/ae-real-2-evidence.json`、`docs/milestones/ae-real-2-evidence-k2.json`

---

## 1. 统一执行证据模型

本轮把执行证据从“只统计 `TOOL_EXECUTE`”扩展为按语义分层、可追溯的统一投影：

| 层 | 来源 | 关键字段 |
| --- | --- | --- |
| 模型提议 | `TOOL_PROPOSAL` | `tool_call_id`、工具身份、`arguments_hash`、step |
| 策略决定 | `POLICY` / `APPROVAL_WAIT` | `policy_decision`、工具身份、step |
| 只读执行 | `TOOL_EXECUTE` | `tool_call_id`、工具身份、per-call `status/error_code` |
| 写操作执行 | `ACTION` 或 Approval `execution_status` | `logical_action_id`、工具身份、`status/error_code` |
| 审批事实 | `Approval` | `approval_id`、`logical_action_id`、decision/execution status |
| 业务副作用 | 独立业务表断言 | ticket subject/数量等，不进入模型自述 |

`build_trajectory_observation` 新增：

- `execution_evidence`：按 step 顺序排列的 `PROPOSAL / POLICY / TOOL_EXECUTE / ACTION / APPROVAL_WAIT / APPROVAL` 事件；
- `tool_execute_calls` 与 `action_calls`：分别保留只读执行和写执行；
- `executed_calls`：两者合并后的真实执行序列；
- `execution_evidence_version = execution-evidence-v1`；
- `action_results`：`ACTION` 步骤的 per-action `logical_action_id / status / error_code`。

关键语义：

- `ACTION` 不再被简单追加为工具文本，而是带 `logical_action_id` 的写执行；
- 审批已通过但 `ACTION` 步骤缺失（例如 `UNKNOWN_OUTCOME` 提前返回）时，从 Approval `execution_status` 生成写执行证据；
- `TOOL_EXECUTE` 和 `ACTION` 通过 `tool_call_id` / `logical_action_id` 保留关联；
- 历史 `trajectory-v1` observation 字段保持兼容，新增字段为 additive，不重写历史结果。

---

## 2. 指标与安全边界修正

| 指标 / 信号 | 修正 |
| --- | --- |
| `executed_tool_recall` | 现在覆盖 `TOOL_EXECUTE + ACTION`，写入类工具不再漏评 |
| `forbidden_tool_call_rate` / `tool_order_violation_rate` | 对真实执行（含 ACTION）计算 |
| `redundant_call_rate` | 只把**成功调用**的重复签名算冗余；失败后的重试不算重复副作用 |
| `recovery_rate` | 从统一 `executed_calls` 计算；`UNKNOWN_OUTCOME` 不作为可重试失败，返回 `NOT_AVAILABLE / unknown_outcome_not_retryable` |
| `trajectory_success` | 出现 `UNKNOWN_OUTCOME` 时返回 `NOT_AVAILABLE / unknown_outcome`，不算成功 |
| `approval_execution_accuracy`（新增） | 期望 APPROVED 时必须 `action_executed=true`；未知写操作不能因“审批决定正确”而被记为 task success |
| `unauthorized_execution` | 仅在有写执行/审批事实的写作用域内判断；读-only 运行不伪造 false |
| `duplicate_side_effect` | 仅在有写执行证据时判断；按 `logical_action_id` 去重 |
| `unknown_outcome_semantics_ok` | 只有存在未知结果且终态为 `NEEDS_ATTENTION` 时才为 true |
| 证据范围 | 新增 `execution_evidence_complete` / `execution_evidence_scope=persisted_run_steps_and_approvals`，明确“检查范围内未违规”不等于全局保证 |

新增回归测试：

- `tests/unit/test_ae_real2_execution_evidence.py`：ACTION 纳入执行、拒绝不执行、未批准写执行、重复 action、未知结果、失败恢复、`action_results` 持久化；
- `tests/unit/test_ae_real_fixes.py`：此前修复的 metadata 与 Numeric 量化回归。

---

## 3. 真实失败恢复与未知结果实验

### 3.1 单次实验（AE-Real-2，真实模型）

- Run ID：`9b611ce9-0f5d-4638-80ed-6f8778ee58db`
- 模型：Alibaba MaaS `deepseek-v4.1-flash`
- 故障注入：`CONTROLLED_FAULT_INJECTION`（明确标注，不是真实外部服务故障）
  - `query_customer` 第一次返回 `SIMULATED_TOOL_FAILURE`，第二次成功；
  - `create_ticket` 在审批通过后返回 `UNKNOWN_OUTCOME`。

| case | 真实执行事实 | 指标 |
| --- | --- | --- |
| `real-recovery` | 2 次 `TOOL_EXECUTE query_customer`：第一次 `ERROR/SIMULATED_TOOL_FAILURE`，第二次 `SUCCESS` | `task_success=1`、`trajectory_success=1`、`recovery_rate=1`、`redundant_call_rate=0` |
| `real-unknown-write` | `TOOL_EXECUTE query_customer SUCCESS`；`ACTION create_ticket UNKNOWN_OUTCOME`（`logical_action_id` 存在）；Approval `execution_status=UNKNOWN_OUTCOME` | `task_success=0`、`approval_execution_accuracy=0`、`trajectory_success=NOT_AVAILABLE/unknown_outcome`、`recovery_rate=NOT_AVAILABLE/unknown_outcome_not_retryable` |

结论：

- 失败步骤与真实 `ToolResult` 对应，`recovery_rate` 来自第二次真实调用，不依赖模型自述；
- 未知写操作没有被重试，没有被记为 success；
- `action_executed=false` 与业务表无新增工单一致。

### 3.2 小规模重复运行（K=2）

- Run ID：`d6d02613-15ed-43dc-9784-a84d57c15c4d`
- 2 个 case × 2 repetitions = 4 次真实执行。

| 统计 | 值 |
| --- | --- |
| `case_count` | 2 |
| `complete_case_count` | 2 |
| `known_trial_count` | 4 |
| `success_count` | 2 |
| `success_rate` | **0.5** |
| `pass_hat_k` (K=2) | **0.5** |
| `pass_hat_k_all_cases` | 0.5 |
| `complete_case_rate` | 1.0 |
| 95% Jeffreys 区间 | [0.123, 0.877] |

解释：

- `success_rate` 是所有已知 trial 的成功比例（2/4）；
- `pass_hat_k` 要求同一 case 的 K 次全部成功；`real-recovery` 两次成功，`real-unknown-write` 两次均非 success，因此 1/2；
- `complete_case_rate=1` 说明没有缺失 trial；未知结果作为明确的非成功进入统计，没有被当作成功或删除；
- 这是 DEV 小样本契约验证，**不是模型总体质量结论**。

---

## 4. CI 历史失败基线归因

最新 PR 的 backend：
- `ruff check .`：通过；
- M7-C deterministic / atomic claim：通过；
- Full integration：失败 2 项、setup error 22 项。

基线归因结果：

- `git diff --name-only origin/main..HEAD` 未修改 `tests/integration/test_measured_reliability.py`、`tests/integration/test_run_budget_ledger_m4.py` 或它们依赖的测试 fixture；
- `test_measured_reliability.py::test_persistent_side_effect_matrix` 失败是既有测试 context 只有 `agent_run/tool_run/approve_action`，而当前 `AgentRunService.resume` 要求 `workspace_read`；
- `test_run_budget_ledger_m4.py::test_retained_rows_resist_rewrite_and_destructive_migration` 及后续 22 个 setup error 是既有共享测试库 fixture/事件循环问题（`MissingGreenlet`），在单独运行时也无法稳定复现，属测试基础设施；
- 这些失败与本轮 AE 执行证据/评分改动没有调用关系；本轮不扩大为全仓测试重构。

---

## 5. 交付物与边界

- 统一执行证据与评分语义修复：`packages/evaluation/trajectory.py`
- ACTION/TOOL_EXECUTE/Approval 关联：`packages/evaluation/trajectory.py`、`packages/evaluation/runner.py`、`packages/agent_runtime/runtime_graph.py`
- 安全边界：`packages/evaluation/trajectory.py`、`packages/evaluation/safety.py`
- 真实失败/恢复实验：`benchmarks/evaluation/ae_real_2_failure.py`
- 证据：
  - `docs/milestones/ae-real-2-evidence.json`（1 repetition）
  - `docs/milestones/ae-real-2-evidence-k2.json`（2 repetitions）
- 能力矩阵：`docs/milestones/AE-capability-matrix.md`

未完成/不宣称：

1. 不是真实外部服务故障，而是明确的**受控故障注入**；
2. 2 cases × 2 repetitions 不足以做总体统计，仅验证口径和隔离；
3. 未做人工 judge 校准；
4. 未启动 AE-8/9/10，未合并 main。
