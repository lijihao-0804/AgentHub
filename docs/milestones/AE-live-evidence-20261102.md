# AE 实机证据（2026-11-02）

> 环境：Docker 全栈已启动（PostgreSQL 16 / Redis / Qdrant），Alibaba MaaS compatible-mode 可用。
> 本文件只记录**实际跑过**的证据；没有跑的一律写“未运行”。

---

## 1. PostgreSQL 迁移 0049

在独立数据库 `agenthub_ae_test` 上执行：

```text
alembic upgrade head      → 0049_evaluation_evidence_level (head)
alembic downgrade 0048    → 成功
alembic upgrade head      → 0049_evaluation_evidence_level (head)
```

结论：`0049` 的 upgrade / downgrade / 再 upgrade 全部成功，`evidence_level` 列与 check constraint 均可用。

---

## 2. 真实 PostgreSQL 集成测试

环境变量：

```text
AGENTHUB_TEST_DATABASE_URL=postgresql+asyncpg://agenthub:***@127.0.0.1:5432/agenthub_ae_test
AGENTHUB_ENVIRONMENT=test
```

先执行 `scripts/bootstrap_checkpoint.py` 建立 LangGraph checkpoint schema（这是部署步骤，不是 Alembic）。

| 测试集 | 结果 |
| --- | --- |
| `test_m7b_experiments.py` + `test_m7c_experiment_runner.py` | **16 passed** |
| `test_m7a_evaluation_datasets.py` + `test_m7ef_ablation_release_gate.py` + `test_m7h_unified_evaluation.py` + `test_interview_foundation.py` + `test_scripted_scenario_driver.py` | **15 passed** |
| 评测相关单元测试（AE-1～AE-7a + M4/M7） | **145 passed** |

说明：第一次跑 `test_scripted_scenario_driver.py` 时因未 bootstrap checkpoint 而失败（`relation "checkpoints" does not exist`），bootstrap 后 8 项全部通过。这不是代码缺陷，符合“framework checkpoint 由部署步骤负责”的约定。

---

## 3. 真实 Alibaba MaaS judge 探针

脚本：`scripts/ae_live_judge_probe.py`
端点：`https://ws-actclc2u6282ierx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
模型：`deepseek-v4.1-flash`
样本：3 个短答案（good / partial / wrong）
调用：3 个 judge（temperature 0 / 0.2 / 0.4，同一 rubric）分别单评 + ensemble 聚合

### 3.1 结果（v2，最终）

| case | single score | ensemble score | ensemble agreement | status |
| --- | ---: | ---: | ---: | --- |
| good | 1.000 | 0.925 | 0.667 | SCORED |
| partial | 0.575 | 0.750 | 0.800 | SCORED |
| wrong | 0.150 | 0.150 | 1.000 | SCORED |

用量：**12 次 judge 调用，3,476 prompt tokens + 5,762 completion tokens = 9,238 tokens**。
按本环境价格量级远低于 30 的预算；没有创建任何持久化实验或改动生产数据。

### 3.2 第一次探针发现的问题（保留）

v1 使用 `max_tokens=700` 且把 `reasoning_content` 当作解析输入，结果：

- `partial` 的 ensemble 只有 1/3 成员产出可解析 JSON → `FAILED / judge_ensemble_quorum_not_met`；
- 原因：`deepseek-v4.1-flash` 是 reasoning 模型，700 tokens 可能全花在 `reasoning_content`，最终 `content` 为空；把 reasoning 喂给“取第一个 `{...}`”的解析器也不可靠。

**修复**：judge 客户端只读取 `content`，不读 `reasoning_content`；`max_tokens` 提到 1500。v2 三个 case 全部 SCORED。

这条经验应写进 judge 运维规范：**reasoning 模型的 judge 必须显式检查 `content`，并给足输出预算**。

### 3.3 边界

- 这是**手动探针**，不是一次持久化 Experiment；不能当模型质量结论。
- 3 个样本、同一模型不同温度，不构成 judge 偏差评测；只验证管道与聚合行为。
- 分数差异（good 1.0 → ensemble 0.925；partial 0.575 → 0.75）说明 judge 方差真实存在，不能把单次分数当精确值。

---

## 4. 未运行

- 真实 **Runtime evaluation**（带工具/审批/知识快照的完整 Experiment）：需要创建 workspace/agent/knowledge 并在 worker 中执行，本轮未做。
- 真实 **judge 校准集 + kappa**：没有人工标签。
- 前端页面。

---

## 5. 证据文件

- `docs/milestones/ae-live-judge-20261102.json`（v1，保留 reasoning 截断问题）
- `docs/milestones/ae-live-judge-20261102-v2.json`（v2，最终）
- `scripts/ae_live_judge_probe.py`（可复现脚本）
