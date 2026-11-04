# AE 能力证据矩阵

> 目的：把“研究报告里写了什么”和“代码/测试/实机到底做到了什么”分开。每一行都必须能追到一个实现、一个测试和一个实机状态。
>
> 当前功能基线：2026-11-04，main `4d5de004c3ee3736360a93dd374f2069eafda0dc`（AE-10 [PR #8](https://github.com/lijihao-0804/AgentHub/pull/8) 已合并）；迁移 head `0051_interactive_lifecycle`。
> 该 main 的 [完整 CI](https://github.com/lijihao-0804/AgentHub/actions/runs/41859097801)：531 integration passed / 0 skipped，2846 non-integration passed / 35 条件跳过。下表历史实验各自保留原分母、时点与证据等级，不相加为质量成绩。

| 能力 | 实现 | 单元测试 | 真实 PostgreSQL | 真实模型 | 状态 / 已知限制 |
| --- | --- | --- | --- | --- | --- |
| **真实 Runtime 端到端（AE-Real-1）** | `benchmarks/evaluation/ae_real_1.py` | `test_ae_real_fixes.py` + 定向测试 | **通过（隔离库 agenthub_ae_real）** | **通过（3 case 真实 Alibaba）** | **PASS**：工具/审批/轨迹/业务断言全链；1 repetition、3 case，非总体性能 |
| **执行证据完整性（AE-Real-2）** | `trajectory.py` + `runtime_graph.py` + `fault_injection.py` | `test_ae_real2_execution_evidence.py`（8）+ `test_ae_real2_fault_isolation.py`（1） | **通过（AE-Real-2）** | **通过（受控故障注入 + 真实模型）** | **PASS**：ACTION 纳入写执行、未知结果不重试、recovery=1、redundancy 不误报重试、故障按 request_id 隔离 |
| **失败/未知恢复统计（AE-Real-2 K=2）** | `statistics_service.py` | 同上 | **通过（Run `78f4cbfe...`）** | **通过（4 次真实执行）** | `success_rate=0.5`、`pass_hat_k=0.5`、`complete_case_rate=1`、`literal_consistency_at_k=0`（字面不稳定/未全部成功）；小样本边界 |
| 轨迹投影（提议/执行/审批分离） | `packages/evaluation/trajectory.py` | `test_ae1_trajectory.py`（16） | **通过（AE-Real-1）** | **通过（AE-Real-1）** | 已实现；执行证据缺失时 `trajectory_success=NOT_AVAILABLE` |
| 轨迹指标（选择/执行/顺序/恢复/循环） | 同上 | 同上 | **通过（AE-Real-1）** | **通过（AE-Real-1）** | 已实现；`recovery_rate` 依赖 `TOOL_EXECUTE.tool_results` |
| 参数身份 hash | `runtime_graph.py` TOOL_PROPOSAL | 同上 | **通过（AE-Real-1）** | **通过（AE-Real-1）** | 已实现并持久化；历史 Run 无该键 → `NOT_AVAILABLE` |
| `pass^k` / 方差 / 一致性 | `statistics.py` | `test_ae2_statistics.py`（10） | **通过（AE-Real-1）** | **通过（AE-Real-1）** | 已实现；同时报 `pass_hat_k_all_cases` / `complete_case_rate` |
| Jeffreys / 按 case bootstrap | 同上 | 同上 | **历史真实运行已生成区间** | AE-Real-1 / AE-Real-2 小样本 | 已实现；小样本及退化区间不证明稳定收益 |
| McNemar / Holm / MDE | 同上 | 同上 | 未有充分真实配对验证 | 未有充分真实配对验证 | 已实现；确定性契约与已知值对照，不扩大为真实模型统计确认 |
| 统计接口 | `statistics_service.py` + route | 契约 | **AE-Real-1/2 服务层、AE-10 HTTP** | **历史 AE-Real-1/2** | 只读；已有 3 case/K=1 与 2 case/K=2，仍无充分规模的独立 HOLDOUT 质量确认 |
| 证据等级 | `models.py` / `0049` / `experiments.py` | `test_ae3_evidence_level.py`（11） | **通过 upgrade/downgrade** | 未跑 | 已实现；等级仍由创建者声明 |
| `REQUIRED_EVIDENCE_LEVEL` 门禁 | `release_gate.py` | 同上 | 未跑 | 未跑 | 已实现 |
| `MIN_PASS_HAT_K` 门禁 | `release_gate.py` | `test_ae3_evidence_level.py` | **真实持久运行 PASS** | **AE-Real-1：3 case/K=1** | FAIL / 覆盖不足 INCONCLUSIVE 是合成契约，见下方逐项证据 |
| `MIN_CI_LOWER_BOUND` 门禁 | 同上 | 同上 | 合成配对不足契约 | 尚无充分真实配对确认 | AE-Real-1 JSON 仅记录 `controlled_contract_only / insufficient_pairs`，不能写为真实 PASS |
| `NON_INFERIORITY` 门禁 | 同上 | 同上 | 实现与契约覆盖 | 尚无真实非劣性确认 | AE-Real-1 JSON 未记录该规则的真实配对验证；与代码已实现分开 |
| 多 Judge ensemble / quorum | `judge.py` | `test_ae4_judge_ensemble.py`（7） | 未跑 | **12 次真实调用** | 已实现；顺序调用；校准队列未做 |
| 冻结 Judge 执行计划 | `judge.py` / worker | `test_ae4...` | 未跑 | 探针直连，未走 gateway | 已实现 `judge_profile_to_plan`，worker 走 `generate_resolved` |
| Ensemble 创建 API | schema / route / service | 契约 | 未跑 | 未跑 | 已实现 `judge_model_profile_ids` + `judge_strategy` |
| 失败分类（12 码） | `taxonomy.py` | `test_ae5_taxonomy.py`（10） | 未跑（接口无集成用例） | 未跑 | 规则归因；非因果 |
| **AE-6b 历史真实模型闭环** | `environment.py` + `interactive_driver.py` + `scenario_driver.py` | `test_ae6b_interactive.py` | **通过（历史受控 PostgreSQL）** | **历史 3 cases 真实 Alibaba** | 历史 PASS 保留；两份不同 Run 的 JSON 不改写 |
| **AE-6b Integration Closure** | `interactive_lifecycle.py` + worker per-item router + `0051` | router/protocol/budget/fencing 单测 | **通过：真实 PostgreSQL/Redis/Celery，四处进程退出重启** | 收口使用确定性 provider | **已合并**：[验收报告](AE-6b-integration-closure.md) 保留历史 528/2831 计数；当前主线 57 项必需 AE-6b 集成仍通过；有限金额派发前拒绝、未知写入留存对账 |
| 环境契约 | `environment.py` | `test_ae6b_interactive.py` + `test_ae6_environment_simulator.py` | **通过（AE-6b）** | **通过（AE-6b）** | `SupportFixtureEnvironment` 已接正式 Experiment；`FixtureEnvironment` 保留轻量契约 |
| 用户模拟器（脚本） | `user_simulator.py` | `test_ae6b_interactive.py` | **通过（AE-6b）** | **通过（AE-6b）** | `ScriptedUserSimulator` 条件披露；模型驱动未做 |
| 安全断言（OWASP 7 族） | `safety.py` | `test_ae7_safety.py`（7） | 未跑 | 未跑 | 断言层；覆盖率已实现，攻击语料未做 |
| 外部基准适配 | 未实现 | — | — | — | AE-8 |
| 在线评测 | 未实现 | — | — | — | AE-9 |
| **AE-10 结果与证据 UI** | 现有 run 页面、`evidence_view.py`、typed client | 13 后端单测、4 个前端专项脚本 | **2 项真库回归及实际 HTTP/本地预览** | UI 验收使用确定性受控数据 | **已合并**：[验收报告](AE-10-minimal-evaluation-ui.md)；列表 observation 固定为空，详情白名单；token 续期保持筛选和分页；非完整 Dashboard 或门禁策略编辑器 |

## 状态口径

- **已实现**：代码存在且单元测试通过；
- **真实 PostgreSQL 通过**：在对应里程碑报告声明的隔离数据库上实际运行；AE-6b 新收口使用本任务独立测试库，数据库与 provider 真实性分开记录；
- **真实模型**：实际调用过 Alibaba MaaS（judge 探针 + AE-Real-1 Runtime）；
- 未写“已验证”的，就是不应当对外宣称的。

## 当前最大的三个未闭环项

1. **真实 Runtime 的样本规模**：已有 AE-Real-1 的 3 case/K=1，以及 AE-Real-2 的 2 case/K=2、失败/未知恢复统计；仍缺充分规模、人工校准和独立 HOLDOUT 的真实质量确认；
2. **Judge 校准**：没有人工标签、没有 kappa、没有位置交换；
3. **门禁统计闭环**：仅 `MIN_PASS_HAT_K` 有上述真实小样本 PASS；配对区间和非劣性仍需充分真实配对验证，不能把合成契约当成真实质量结论。

## 原始证据对应关系

| 原始 JSON | 已支持的结论 | 不支持的扩大结论 |
| --- | --- | --- |
| [ae-real-1-evidence.json](ae-real-1-evidence.json) `gate_checks[0]` | `MIN_PASS_HAT_K`，`basis=real_persisted_run`，PASS | 三题单次即可证明稳定性 |
| 同文件 `gate_checks[1:3]` | `controlled_contract_only`：FAIL、`insufficient_complete_cases` | 真实低分模型和真实覆盖不足实验 |
| 同文件 `gate_checks[3]` | `MIN_CI_LOWER_BOUND`，`controlled_contract_only`，`insufficient_pairs` | 真实配对区间 PASS 或 NON_INFERIORITY 确认 |
| [ae-real-2-evidence-k2-isolated-v4.json](ae-real-2-evidence-k2-isolated-v4.json) | 2 case、4 trial、K=2；success_rate/pass_hat_k=0.5，literal_consistency=0 | 没有重复采样，或已有总体可靠性/语义正确性证明 |

当前入口中的“已合并”“CI 通过”“真实模型质量”“生产部署”各有独立证据；AE-10 本地 production build 预览不等于生产部署。后续开发处置见 [WIP 分类](../reviews/WIP-inventory-20261104.md)。
