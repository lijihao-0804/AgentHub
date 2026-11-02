# AE-1 — Trajectory Evidence（轨迹证据层）

Status: **PARTIAL PASS（单元验证通过；真实 PostgreSQL 集成与真实模型实验未运行）**

> **2026-11-02 实机更新**：M7-B/M7-C 16 项集成测试已在真实 PostgreSQL 上通过；见 [AE 实机证据](AE-live-evidence-20261102.md)。

基线：`fd14baf`（分支 `codex/ae-1-trajectory-evidence`）。本里程碑只做“把已有执行事实投影成可评分证据”，不改变任何既有实验的历史结果。

---

## 1. 目标与边界

**目标**：让真实 Runtime 的评测 observation 带上工具调用序列、参数身份、审批事实与循环信号，并据此计算轨迹指标。

**不做**：

- 不新增数据库表（归一化 `evaluation_trajectory_steps` 留到 AE-1b）；
- 不改历史实验的 manifest、observation 或指标；
- 不改 judge、不改发布门禁语义；
- 不引入外部基准、用户模拟器或在线评测。

---

## 2. 实现

| 变更 | 位置 | 说明 |
| --- | --- | --- |
| `arguments_hashes` 写入 `TOOL_PROPOSAL` | `packages/agent_runtime/runtime_graph.py` | 每个工具调用的参数以 `canonical_json_hash` 记录；不写参数原文 |
| 轨迹投影 `trajectory-v1` | `packages/evaluation/trajectory.py`（新增） | 纯函数：steps + approvals + terminal + counters → observation |
| 轨迹 evaluator | 同上 | `tool_precision/recall/f1`、`forbidden_tool_call_rate`、`tool_order_violation_rate`、`redundant_call_rate`、`recovery_rate`、`approval_respected`、`tool_call_efficiency`、`loop_rate`、`trajectory_success` |
| 兼容键 | 同上 | 同时写入 `tool_identity` / `tool_sequence` / `steps` / `terminal_status` / `approval_required` / `approval_decision` / `action_executed` / `answer_hash`，让既有类别 evaluator 在真实 Runtime 下也能工作 |
| 新实验 manifest | `packages/evaluation/reproducibility.py` | `observation_schema_version = 3`，新增 `trajectory-evaluator: trajectory-v1` |
| 按 manifest 启用 | `packages/evaluation/metrics.py` | 只有 schema 3 的 manifest 才计算轨迹指标；schema 2 / 缺字段的历史 manifest 行为不变 |
| Runtime 投影 | `packages/evaluation/runner.py` | `AgentRuntimeEvaluationDriver.prepare` 查询 `run_steps` 与 `approvals`，组装投影 |
| 指标定义 | `packages/evaluation/metrics.py` | 新增 10 个指标定义，方向/聚合方式显式声明 |

### 2.1 为什么用 manifest 而不是 observation 决定是否评分

历史实验的 observation 可能没有 `trajectory_version`；如果只看 observation，新代码不会影响它们。但反过来，如果旧 manifest 的实验重跑一次，observation 会带轨迹——只看 observation 就会给旧实验加上新指标，破坏“历史实验口径不变”。因此开关放在 **frozen manifest**：schema 3 才启用，schema 2/缺字段一律不启用。

### 2.2 为什么参数只存 hash

轨迹指标需要“参数是否一致”，不需要“参数是什么”。`arguments_hashes` 用 canonical JSON 计算，键序不影响结果；同时满足仓库规则 21（trace 内容默认不记业务正文）。

---

## 3. 验证

### 3.1 已运行

```powershell
python -m pytest tests/unit/test_ae1_trajectory.py -q --basetemp pytest-tmp -p no:cacheprovider
python -m pytest tests/unit/test_m7a_dataset_validation.py tests/unit/test_m7b_reproducibility.py `
  tests/unit/test_m7d_metrics.py tests/unit/test_m7d_answer_quality_judge.py `
  tests/unit/test_m7c_runner.py tests/unit/test_m7h_unified_evaluation.py `
  tests/unit/test_m4e_agent_evaluation.py -q --basetemp pytest-tmp -p no:cacheprovider
python -m ruff check packages/evaluation packages/agent_runtime/runtime_graph.py tests/unit/test_ae1_trajectory.py
```

结果：

- AE-1 新增 16 个单元测试全部通过；
- 既有评测相关 7 个单元测试文件全部通过（70 项）；
- `ruff check` 通过。

### 3.2 未运行（明确边界）

- **真实 PostgreSQL 集成测试**：本机没有 `AGENTHUB_TEST_DATABASE_URL`，Docker 也被沙箱拒绝；`tests/integration/test_m7c_experiment_runner.py`、`test_m7ef_ablation_release_gate.py` 未运行。
- **真实模型实验**：没有调用任何付费 provider；因此“轨迹指标在真实模型输出上的分布”未知。
- **API/UI 端到端**：未启动服务；轨迹目前通过既有 `case-results` 接口的 `observation` 字段暴露，没有专门的轨迹页面。

---

## 4. 已知限制

1. **没有归一化步骤表**：轨迹只存在 `case_result.observation` JSONB 里；按步骤查询/聚合要走 JSONB，性能与 SQL 便利性都不如独立表。AE-1b 补。
2. **`loop_rate` 依赖 GUARD 步骤**：只有运行时写入 `AGENT_IDENTICAL_TOOL_CALL_LIMIT` 才有值；历史 Run 没有该步骤 → `NOT_AVAILABLE`，不是 0。
3. **APPROVAL 的 `unauthorized_execution` / `duplicate_side_effect` 仍走旧语义**：投影只补了 `approval_required` / `approval_decision` / `action_executed`；后两项缺失时旧 evaluator 仍按 False 计，这是既有行为，未在本里程碑改动。
4. **精确答案 hash 的语义**：`answer_hash` 只在 `KNOWLEDGE_QA` 且 case 有精确 expected 时投影；开放式答案仍走 judge，不硬判。
5. **轨迹指标是补充，不是替代**：`task_success` 仍由类别 evaluator 决定；`trajectory_success` 是独立指标，不能反过来覆盖 `task_success`。

---

## 5. 下一步

1. **AE-1b**：新增 `evaluation_trajectory_steps` 表与迁移 `0049`，在 `complete_case` 内幂等写入；补故障注入测试（半条轨迹、重复写、事务回滚）。
2. **集成验证**：在有 PostgreSQL 的环境跑 `test_m7c_experiment_runner.py` 与新增的 runtime trajectory 集成用例。
3. **AE-2**：`pass^k`、方差、cluster bootstrap、McNemar、Holm、MDE。
4. **AE-3**：`evidence_level` 字段与门禁规则。

---

## 6. 复现命令

```powershell
git switch codex/ae-1-trajectory-evidence
python -m pytest tests/unit/test_ae1_trajectory.py -q --basetemp pytest-tmp -p no:cacheprovider
```

> 本机 `pytest` 的默认临时目录在沙箱下不可列举；使用工作区内预创建的 `pytest-tmp` 作为 `--basetemp`，并关闭 cacheprovider。
