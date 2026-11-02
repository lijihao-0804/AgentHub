# AE 能力证据矩阵

> 目的：把“研究报告里写了什么”和“代码/测试/实机到底做到了什么”分开。每一行都必须能追到一个实现、一个测试和一个实机状态。
>
> 更新：2026-11-02 ｜ 分支：`codex/ae-1-trajectory-evidence`

| 能力 | 实现 | 单元测试 | 真实 PostgreSQL | 真实模型 | 状态 / 已知限制 |
| --- | --- | --- | --- | --- | --- |
| **真实 Runtime 端到端（AE-Real-1）** | `benchmarks/evaluation/ae_real_1.py` | `test_ae_real_fixes.py` + 定向测试 | **通过（隔离库 agenthub_ae_real）** | **通过（3 case 真实 Alibaba）** | **PASS**：工具/审批/轨迹/业务断言全链；1 repetition、3 case，非总体性能 |
| **执行证据完整性（AE-Real-2）** | `trajectory.py` + `runtime_graph.py` + `fault_injection.py` | `test_ae_real2_execution_evidence.py`（8）+ `test_ae_real2_fault_isolation.py`（1） | **通过（AE-Real-2）** | **通过（受控故障注入 + 真实模型）** | **PASS**：ACTION 纳入写执行、未知结果不重试、recovery=1、redundancy 不误报重试、故障按 request_id 隔离 |
| **失败/未知恢复统计（AE-Real-2 K=2）** | `statistics_service.py` | 同上 | **通过（Run `78f4cbfe...`）** | **通过（4 次真实执行）** | `success_rate=0.5`、`pass_hat_k=0.5`、`complete_case_rate=1`、`literal_consistency_at_k=0`（字面不稳定/未全部成功）；小样本边界 |
| 轨迹投影（提议/执行/审批分离） | `packages/evaluation/trajectory.py` | `test_ae1_trajectory.py`（16） | **通过（AE-Real-1）** | **通过（AE-Real-1）** | 已实现；执行证据缺失时 `trajectory_success=NOT_AVAILABLE` |
| 轨迹指标（选择/执行/顺序/恢复/循环） | 同上 | 同上 | **通过（AE-Real-1）** | **通过（AE-Real-1）** | 已实现；`recovery_rate` 依赖 `TOOL_EXECUTE.tool_results` |
| 参数身份 hash | `runtime_graph.py` TOOL_PROPOSAL | 同上 | **通过（AE-Real-1）** | **通过（AE-Real-1）** | 已实现并持久化；历史 Run 无该键 → `NOT_AVAILABLE` |
| `pass^k` / 方差 / 一致性 | `statistics.py` | `test_ae2_statistics.py`（10） | **通过（AE-Real-1）** | **通过（AE-Real-1）** | 已实现；同时报 `pass_hat_k_all_cases` / `complete_case_rate` |
| Jeffreys / bootstrap / McNemar / Holm / MDE | 同上 | 同上 | 未跑 | 未跑 | 已实现；与 SciPy 已知值对照 |
| 统计接口 | `statistics_service.py` + route | 契约 | **通过（AE-Real-1 服务层）** | **通过（AE-Real-1）** | 只读；按 variant 返回；真实样本仅 3 case |
| 证据等级 | `models.py` / `0049` / `experiments.py` | `test_ae3_evidence_level.py`（11） | **通过 upgrade/downgrade** | 未跑 | 已实现；等级仍由创建者声明 |
| `REQUIRED_EVIDENCE_LEVEL` 门禁 | `release_gate.py` | 同上 | 未跑 | 未跑 | 已实现 |
| `MIN_PASS_HAT_K` / `MIN_CI_LOWER_BOUND` / `NON_INFERIORITY` 门禁 | `release_gate.py` | 同上 | **真实 PASS + 契约 FAIL/INCONCLUSIVE（AE-Real-1）** | **真实 PASS** | 已实现；`insufficient_pairs` / `insufficient_complete_cases` / `degenerate_interval` 均返回 `INCONCLUSIVE` |
| 多 Judge ensemble / quorum | `judge.py` | `test_ae4_judge_ensemble.py`（7） | 未跑 | **12 次真实调用** | 已实现；顺序调用；校准队列未做 |
| 冻结 Judge 执行计划 | `judge.py` / worker | `test_ae4...` | 未跑 | 探针直连，未走 gateway | 已实现 `judge_profile_to_plan`，worker 走 `generate_resolved` |
| Ensemble 创建 API | schema / route / service | 契约 | 未跑 | 未跑 | 已实现 `judge_model_profile_ids` + `judge_strategy` |
| 失败分类（12 码） | `taxonomy.py` | `test_ae5_taxonomy.py`（10） | 未跑（接口无集成用例） | 未跑 | 规则归因；非因果 |
| **真实交互环境与动态多轮（AE-6b）** | `environment.py` + `interactive_driver.py` + `scenario_driver.py` | `test_ae6b_interactive.py`（7） | **通过（真实 PostgreSQL）** | **通过（3 cases 真实 Alibaba）** | **PASS**：reset/assert/teardown、动态披露、Canary、业务终态 |
| 环境契约 | `environment.py` | `test_ae6b_interactive.py` + `test_ae6_environment_simulator.py` | **通过（AE-6b）** | **通过（AE-6b）** | `SupportFixtureEnvironment` 已接正式 Experiment；`FixtureEnvironment` 保留轻量契约 |
| 用户模拟器（脚本） | `user_simulator.py` | `test_ae6b_interactive.py` | **通过（AE-6b）** | **通过（AE-6b）** | `ScriptedUserSimulator` 条件披露；模型驱动未做 |
| 安全断言（OWASP 7 族） | `safety.py` | `test_ae7_safety.py`（7） | 未跑 | 未跑 | 断言层；覆盖率已实现，攻击语料未做 |
| 外部基准适配 | 未实现 | — | — | — | AE-8 |
| 在线评测 | 未实现 | — | — | — | AE-9 |
| UI | 未实现 | — | — | — | AE-10 |

## 状态口径

- **已实现**：代码存在且单元测试通过；
- **真实 PostgreSQL 通过**：在 `agenthub_ae_test` 或 `agenthub_ae_real` 上实际跑过；
- **真实模型**：实际调用过 Alibaba MaaS（judge 探针 + AE-Real-1 Runtime）；
- 未写“已验证”的，就是不应当对外宣称的。

## 当前最大的三个未闭环项

1. **真实 Runtime 的样本规模**：AE-Real-1 已跑通 3 case / 1 repetition，但还没有多 repetition、失败/重试、HOLDOUT 的真实统计；
2. **Judge 校准**：没有人工标签、没有 kappa、没有位置交换；
3. **门禁统计闭环**：三条统计规则已实现并在真实运行上验证 PASS；失败/不充分分支是契约测试，尚无真实低分样本。
