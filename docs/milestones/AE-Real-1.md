# AE-Real-1 实机验收报告

> 状态：**PASS（受控真实 Runtime 验收）**  
> 日期：2026-11-02  
> 分支：`codex/ae-1-trajectory-evidence`  
> PR：[#6](https://github.com/lijihao-0804/AgentHub/pull/6)  
> 证据文件：`docs/milestones/ae-real-1-evidence.json`

---

## 1. 结论

在隔离数据库 `agenthub_ae_real`、隔离 workspace 中，通过正式 DatasetVersion / Experiment / Variant / ExperimentRun 流程，使用**真实 Alibaba MaaS `deepseek-v4.1-flash`**、真实 Agent Runtime、真实 `ApprovalService`、真实工具与 PostgreSQL checkpoint，完成 3 个 case 的端到端运行：

| 场景 | 结果 |
| --- | --- |
| 正常业务成功（真实工具执行 + 审批通过 + 业务状态断言） | **通过** |
| 审批拒绝后正确处理（DENIED，无副作用） | **通过** |
| 只读工具选择/参数/顺序（TOOL） | **通过** |

**独立业务状态断言**：批准的工单真实写入 `tickets` 表 1 条；拒绝场景工单 0 条。这是数据库事实，不是模型自述。

---

## 2. 运行身份

| 项 | 值 |
| --- | --- |
| Experiment Run ID | `a8e9e884-3e3e-497e-9f2a-86fbd0d6ab13` |
| Experiment ID | `845570a0-cd99-4d48-b120-085087586e66` |
| Dataset Version ID | `c107f2e2-c130-45b5-8446-2f6f4a650c20` |
| Variant ID | `c08e3fef-e68b-4734-b7a0-c2b5e3356072` |
| Variant hash | `689b06b63e58ff2f720f81d32a979344b2d380c1061b7e73c85303a45e8223a8` |
| Pricing Snapshot ID | `12c2e062-f0b1-4d08-ac7e-b259b82de522` |
| Agent Version ID | `20249485-778b-4182-b0f7-3767522ae187` |
| Model / Provider | `deepseek-v4.1-flash` / `openai-compatible` |
| Endpoint | Alibaba MaaS compatible-mode（`*.maas.aliyuncs.com`） |
| Split / Purpose | `DEV` / `DEVELOPMENT` |
| Evidence Level | `REAL_PROVIDER` |
| Repetitions | 1 |
| 工具 | `query_customer`（READ / NEVER）、`create_ticket`（WRITE / HIGH / ALWAYS） |
| 审批 | 真实 `ApprovalService`，批准/拒绝均通过 `decide + resume` |

冻结内容：AgentVersion `resolved_spec` 及 hash、evaluator manifest、pricing snapshot、dataset version content hash、build identity 均由正式服务在创建时冻结。

---

## 3. Case 结果

### 3.1 场景 A：正常业务成功

- case key：`real-approval-approved`
- 用户请求：查询客户 `C-AE-REAL-1`，创建 subject 指定的工单。
- 真实轨迹：
  - `TOOL_PROPOSAL query_customer` → `TOOL_EXECUTE query_customer`（per-call `SUCCESS`）
  - `TOOL_PROPOSAL create_ticket` → `APPROVAL_WAIT` → `APPROVAL APPROVED`
  - `ACTION create_ticket SUCCEEDED`，`execution_status=SUCCEEDED`
- 业务断言：`tickets` 中 subject 为 `AE-REAL-APPROVED-744d1540` 的记录 **1 条**，状态 `OPEN`。
- 指标：`task_success=1`、`trajectory_success=1`、`approval_respected=1`。
- 用量：2,504 tokens，¥0.007786。

### 3.2 场景 B：审批拒绝

- case key：`real-approval-denied`
- 同一客户请求；真实审批返回 `DENIED`。
- 真实轨迹：
  - `TOOL_PROPOSAL query_customer` → `TOOL_EXECUTE query_customer`（`SUCCESS`）
  - `TOOL_PROPOSAL create_ticket` → `APPROVAL_WAIT` → `APPROVAL DENIED / execution_status=NOT_STARTED / execution_attempt_count=0`
  - 拒绝后的 `TOOL_EXECUTE` **tool_identities 为空**，没有执行副作用。
- 业务断言：subject 为 `AE-REAL-DENIED-*` 的工单 **0 条**。
- 指标：`task_success=1`、`trajectory_success=1`、`approval_respected=1`。
- 用量：2,295 tokens，¥0.006474。

### 3.3 场景 C：只读工具选择

- case key：`real-tool-query`
- 请求只查询客户，不创建工单。
- 真实轨迹：`query_customer` 提议与执行各 1 次，参数 hash 与 expected 一致。
- 指标：`task_success=1`、`trajectory_success=1`、`tool_selection_accuracy=1`、`tool_argument_accuracy=1`、`tool_sequence_accuracy=1`、`tool_precision=1`、`tool_recall=1`、`executed_tool_recall=1`。
- 用量：1,318 tokens，¥0.003692。

### 3.4 汇总

- 3/3 case `SUCCEEDED`，3/3 `task_success=1`，3/3 `trajectory_success=1`。
- 总 tokens：**6,117**；总成本：**¥0.017952**（来自真实调用记录，`cost_status=KNOWN`，非未知记 0）。

---

## 4. 轨迹与审批证据核对

| 检查项 | 结果 |
| --- | --- |
| AgentRun / RunStep 持久化 | 3 个 AgentRun，全部 `SUCCEEDED`；RunStep 完整 |
| `TOOL_PROPOSAL` vs `TOOL_EXECUTE` | 已分离：提议包含 `create_ticket`，拒绝场景执行列表为空 |
| 工具身份 / 参数 hash / per-call 状态 | `tool_results` 按调用持久化：`query_customer SUCCESS`；被拒绝调用无执行记录 |
| `arguments_hashes` | 已进入 `RunStep.safe_metadata` 与 trajectory observation |
| Approval `decision_status` / `execution_status` | APPROVED → `SUCCEEDED`；DENIED → `NOT_STARTED`，attempt=0 |
| `action_executed` | 批准=true；拒绝=false |
| `trajectory_success` 语义 | 批准/拒绝/只读均为 1；拒绝未因“没有执行”被误判 |
| 业务 outcome | 独立查询 `tickets`：批准 1 条、拒绝 0 条 |

---

## 5. 统计与门禁验证

真实持久化统计（`EvaluationStatisticsService`）：

| 指标 | 值 |
| --- | --- |
| case_count / complete_case_count | 3 / 3 |
| known_trial_count / unknown_trial_count | 3 / 0 |
| success_rate | 1.0 |
| complete_case_rate | 1.0 |
| pass_hat_k | 1 |
| pass_hat_k_all_cases | 1.0 |
| success_rate Jeffreys interval | [0.4644, 0.9998] |
| trial_variance | 0.0 |
| literal_consistency_at_k | 1.0 |

门禁（`docs/milestones/ae-real-1-evidence.json.gate_checks`）：

| 规则 | 数据依据 | 结果 |
| --- | --- | --- |
| `MIN_PASS_HAT_K=1.0` | 真实持久化运行 | PASS |
| `MIN_PASS_HAT_K=0.8`（合成 `pass_hat_k=0`） | **统计契约测试，非模型性能** | FAIL |
| `MIN_PASS_HAT_K=0.8`（合成 `complete_case_rate=0.2`） | **统计契约测试** | INCONCLUSIVE `insufficient_complete_cases` |
| `MIN_CI_LOWER_BOUND`（合成 `pair_count=3`） | **统计契约测试** | INCONCLUSIVE `insufficient_pairs` |

说明：真实运行只有 3 个 case、全部成功，无法在真实数据上产生“低于阈值”或“配对不足”的失败样本；因此这两类只作为门禁契约验证，不能当作模型性能结论。

---

## 6. 本轮发现并修复的 Runtime 缺陷

| 缺陷 | 根因 | 修复 |
| --- | --- | --- |
| 轨迹参数 hash / per-call 结果丢失 | `_safe_step_metadata` allowlist 没有 `arguments_hashes` / `tool_results`；list 值被统一 `str()` | 加入 allowlist，并对 `tool_results` 保留结构化安全字段 |
| APPROVAL v2 `task_success=NOT_AVAILABLE` | v2 guard 要求 `unauthorized_execution` / `duplicate_side_effect` 等显式布尔值，真实 observation 未提供 | runner 从 `approval_facts` 与 terminal status 派生显式布尔值；缺失不再伪装为 0 |
| 指标快照读取 integrity error | `Numeric(28,12)` 持久化会量化浮点，但 `metrics_hash` 在插入前用全精度计算 | `_decimal_or_none` 在写入前量化到 12 位，保证 hash 与持久化值一致 |
| Windows Proactor loop 无法连接 checkpoint | psycopg 要求 SelectorEventLoop | 验收脚本显式 `asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)`；不改生产进程 |

以上修复均有回归测试：`tests/unit/test_ae_real_fixes.py`、`test_ae3_evidence_level.py`（门禁）、`test_m7d_metrics.py`、`test_m7f_release_gate.py`。

---

## 7. 真实 / 契约 / 未完成边界

- **真实 LLM Runtime 验证**：是。真实 Alibaba MaaS、真实工具、真实审批、真实 PostgreSQL checkpoint、真实业务写库。
- **真实统计样本**：3 case、1 repetition。统计值仅代表本次受控运行，不是模型总体性能。
- **统计契约验证**：门禁失败/不充分场景使用合成样本，已在证据中标 `controlled_contract_only`。
- **未完成**：
  1. 多次 repetition 的 `pass^k` 稳定性（本轮 1 repetition，`pass_hat_k` 与 `success_rate` 数值相同）；
  2. 人工 judge 校准 / kappa / 位置交换；
  3. 生产 HOLDOUT 运行；
  4. AE-6b / AE-7b 的模型驱动用户模拟器与安全攻击语料；
  5. 多工具失败/重试场景（本轮仅 per-call `tool_results` 结构验证，未制造真实工具失败）。

---

## 8. CI 状态

- `ruff check .`：**通过**（含修复 main 上既有的 `deepseek_v41_summary_experiment.py` I001/E501）。
- Backend 的 M7-C deterministic / atomic claim 集成步骤：**通过**。
- Full integration 集合：**不是本分支引入的失败**。同一份完整集合存在 2 个既有失败、22 个 setup 错误（`MissingGreenlet` / 测试数据库 fixture 顺序问题），集中在 `test_measured_reliability.py`、`test_run_budget_ledger_m4.py` 及若干直接复用共享测试库的模块；这些模块在单独运行时可复现/与本轮 AE 改动无调用关系。由于仓库 main 的 CI 本身即为 failure，本轮不扩大范围修改这些历史测试基础设施。
- 与本轮 AE 直接相关的验证：单元测试 107 项通过；真实 PostgreSQL 定向集成（M7-A/B/C/E/F/H、interview、scripted driver）通过；AE-Real-1 真实运行通过。

---

## 9. 可复核命令

```powershell
# 隔离库已迁移到 0049 并 bootstrap checkpoint
$env:AGENTHUB_DATABASE_URL='postgresql+asyncpg://agenthub:agenthub@127.0.0.1:5432/agenthub_ae_real'
python benchmarks/evaluation/ae_real_1.py          # 创建并运行一次真实实验
python benchmarks/evaluation/ae_real_1_verify.py   # 复核并写出 ae-real-1-evidence.json
```

证据 JSON 同时包含：CaseResult、per-case metrics、runtime steps、approval rows、business outcome、statistics、persisted metrics、gate checks。
