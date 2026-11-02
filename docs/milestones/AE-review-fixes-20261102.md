# AE 分支审阅整改记录（2026-11-02）

> 对应审阅：`codex/ae-1-trajectory-evidence` @ `7b47df65`，基线 `main` @ `fd14bafc`。
> 本文只记录**本轮实际改了什么**；没改的明确列出。

---

## 1. P0 评分语义

| 审阅问题 | 整改 | 验证 |
| --- | --- | --- |
| P0-1 工具轨迹混用“提议”和“执行” | `trajectory.py` 现在分别投影 `proposed_calls` / `executed_calls`；`tool_precision/recall/f1` 只评提议（选择），`executed_tool_recall`、`forbidden_tool_call_rate`、`tool_order_violation_rate`、`redundant_call_rate`、`tool_call_efficiency` 只评执行；`tool_sequence` 优先执行、无执行时回退提议 | `test_ae1_trajectory.py` 新增“提议禁止但执行未发生”用例：precision 0.5、forbidden 0、trajectory_success 1 |
| P0-2 `trajectory_success` 对缺失证据过宽 | 必需工具但无执行证据 → `NOT_AVAILABLE`；有执行但缺工具 → 失败；要求审批但无审批事实 → `NOT_AVAILABLE`；顺序/禁止/审批的必需证据缺失不跳过 | 新增 `test_missing_execution_evidence_is_not_available`、`test_missing_required_tool_fails...` |
| P0-3 安全评分把不完整覆盖当安全 | `safety.py` 新增 `safety_coverage_rate` 与 `missing_checks`；`expected.required_safety_checks` 声明必需族；缺必需检查时 `safety_violation_rate = NOT_AVAILABLE / required_safety_checks_missing` | `test_ae7_safety.py` 新增覆盖率 0.5、全 1 两个用例 |

## 2. P1 统计、Judge 与复现

| 审阅问题 | 整改 | 验证 |
| --- | --- | --- |
| P1-4 真实 Judge 探针温度写死 0 | `HttpJudgeClient` 由冻结 Profile 驱动 `temperature`；每次调用记录 `request_model/request_temperature/request_max_tokens`；重新跑 v3 | `ae-live-judge-20261102-v3.json`：`requested_temperatures=[0,0.2,0.4]`、`actual_request_temperatures=[0,0.2,0.4]`；v2 保留为勘误 |
| P1-5 Judge 冻结身份未闭合 | 新增 `judge_profile_to_plan`；`ModelGatewayJudgeClient` 支持 `generate_resolved(plan)`；worker 用冻结 plan 调用，不再按 `model_profile_id` 重新解析 | `test_ae4_judge_ensemble.py` 新增 plan 字段对照 |
| P1-6 Ensemble 无产品入口 | `EvaluationExperimentCreateRequest` 增加 `judge_model_profile_ids` / `judge_strategy`；route 冻结多 Profile 并构建 `FrozenJudgeEnsemble`；`ExperimentService.create_experiment` 接收 `judge_ensemble` | API 契约测试通过 |
| P1-7 `pass^k` / 成功率口径 | 新增 `complete_case_rate`、`pass_hat_k_all_cases`（缺失按失败）、`success_coverage_rate`、`literal_consistency_at_k`；区间 payload 标 `basis=known_trials`；`reliability_note` 明确完整案例偏差 | `test_ae2_statistics.py` 新增断言 |
| P1-8 TOOL_EXECUTE 统一 SUCCEEDED | `runtime_graph.py` 的 `TOOL_EXECUTE` 增加 `tool_results`（每调用 identity/status/error_code）；runner 把它投影进轨迹；`_recovery_rate` 按 per-call 结果计算 | 单元测试 + 现有集成测试通过 |
| P1-9 门禁仍点估计 | 新增 `MIN_PASS_HAT_K` 规则：读取候选 variant 的 `pass_hat_k`，缺失统计 payload → `INCONCLUSIVE`；`MAX_COST_PER_SUCCESS` 仍由现有 `MAX_VALUE` 表达 | `test_ae3_evidence_level.py` 新增 PASS/FAIL/INCONCLUSIVE 三例 |

## 3. P2 与其他

| 审阅问题 | 整改 |
| --- | --- |
| 失败定位共用 `first_error_step_index` | `taxonomy.py` 对 `FORBIDDEN_TOOL_CALL` 使用实际 offender 的 `step_index`；`TOOL_ORDER_VIOLATION` 使用违反顺序的 after 步骤 |
| `consistency_at_k` 语义易误解 | 保留 `consistency_at_k` 兼容，同时输出 `literal_consistency_at_k`；文档改为“成功且字面一致” |
| 文档/报告不一致 | AE-4 改“顺序调用”；移除“未进行真实 Judge 调用”旧状态；实机证据新增 v2 温度勘误；README 改为“主流平台正在从终态扩展到轨迹”；05 计划 `success_rate` 改为已知 trial 分母并增加覆盖率字段 |
| 缺能力证据矩阵 | 新增 `docs/milestones/AE-capability-matrix.md` |

## 4. 仍未完成（不冒充已做）

1. **`MIN_CI_LOWER_BOUND` / `NON_INFERIORITY`**：需要把配对 bootstrap 结果写入 comparison payload 或门禁现算；当前只实现了 `MIN_PASS_HAT_K`。
2. **真实 Runtime Experiment**：轨迹投影还没有在真实 agent run + 工具 + 审批上端到端跑过。
3. **Judge 人工校准 / 位置交换 / 成本计量**：未做。
4. **AE-6b / AE-7b / AE-8 / AE-9 / AE-10**：未做。
5. **`evidence_level` 仍由创建者声明**：没有从 provider 凭证/成本事实反推。

---

## 5. 验证汇总

- 单元测试：AE + 既有评测相关 106 项通过；
- 真实 PostgreSQL 集成测试：31 项通过；
- `ruff`：全部通过；
- 真实 Alibaba judge：v3 12 次调用、8,062 tokens，三个 case 全部 SCORED，实际温度已核对。
