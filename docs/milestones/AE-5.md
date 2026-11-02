# AE-5 — Failure Taxonomy（失败分类与归因）

Status: **PARTIAL PASS（规则分类与聚合接口单元验证通过；未接人工标签/LLM 归因/UI）**

基线：`65bc211`（AE-4 之后），分支 `codex/ae-1-trajectory-evidence`。

---

## 1. 目标与边界

**目标**：回答“失败率之外，失败在哪一类、从哪一步开始”。

**做**：

- 固定失败码表（12 个码，覆盖运行、规划、工具、审批、循环、答案）；
- `classify_failure` 从 `trajectory-v1` 直接判定，标签带 `step_index`、`source=RULE`、`confidence=1.0`；
- Runtime driver 在 observation 里持久化 `failure_taxonomy`；
- 只读接口 `GET /experiment-runs/{run_id}/failure-taxonomy`，按 variant 聚合；
- 历史 run 没有持久化标签时，读取时用同一套规则重算。

**不做（明确边界）**：

- 不做 LLM 自动归因（TRAIL 最强模型 11%，不足以支撑“根因”声明）；
- 不做多 agent 责任归因（Who&When 关键步 14.2%）；
- 不做人工标签队列/仲裁（AE-9 在线评审再做）；
- 不做步骤级因果推断，只做“首个错误步骤”定位。

---

## 2. 实现

| 变更 | 位置 |
| --- | --- |
| 码表与规则 | `packages/evaluation/taxonomy.py`（新增） |
| Runtime 持久化标签 | `packages/evaluation/runner.py` |
| 聚合服务 | `packages/evaluation/statistics_service.py` |
| API | `apps/api/routes/evaluation.py` |
| 单元测试 | `tests/unit/test_ae5_taxonomy.py`（新增） |

### 2.1 码表

| 组 | 码 |
| --- | --- |
| 运行 | `RUN_NOT_SUCCEEDED`, `RUN_FAILED`, `BUDGET_EXHAUSTED` |
| 工具 | `REQUIRED_TOOL_MISSING`, `FORBIDDEN_TOOL_CALL`, `TOOL_ORDER_VIOLATION`, `EXCESSIVE_TOOL_CALLS`, `NO_TOOL_CALLS` |
| 治理 | `APPROVAL_BYPASS`, `APPROVAL_DECISION_MISMATCH` |
| 循环 | `AGENT_LOOP` |
| 终态 | `ANSWER_MISMATCH` |

### 2.2 归因只到“首个错误步骤”

`first_error_step_index` 来自轨迹投影：第一个 `status` 非 `SUCCEEDED` 或带 `error_code` 的步骤。它不解释“为什么”，只回答“从哪一步开始”。这是有意的：把规则能证明的和模型猜的分开。

---

## 3. 验证

### 3.1 已运行

```powershell
python -m pytest tests/unit/test_ae5_taxonomy.py tests/unit/test_ae1_trajectory.py `
  -q --basetemp pytest-tmp -p no:cacheprovider
python -m ruff check packages/evaluation apps/api/routes/evaluation.py
```

结果：25 项通过，lint 通过。

### 3.2 覆盖的用例

禁止工具、缺失必需工具、无工具调用、顺序违反、审批绕过、循环、冗余调用、运行失败、答案不匹配兜底、无轨迹不产标签、标签聚合。

### 3.3 未运行

- 真实 PostgreSQL 聚合接口；
- 人工标签覆盖与仲裁；
- UI 失败热力图。

---

## 4. 已知限制

1. **规则只覆盖可观测事实**：语义错误、政策理解错误仍只能靠 judge/人工。
2. **`ANSWER_MISMATCH` 需要 task_success**：Runtime driver 在评分前写标签，因此该码只在读取时补；持久化标签里通常没有它。
3. **一个失败可能多标签**：当前不做优先级/去重，聚合计数会大于失败 case 数；报告时需说明。
4. **没有因果**：`step_index` 是“首个异常步骤”，不保证是根因。

---

## 5. 下一步

1. **AE-9 人工评审**：标签队列、双人一致性、仲裁、标签回流为数据集。
2. **UI**：失败热力图与步骤跳转。
3. **可选 LLM 归因**：只在人工初筛后对争议样本使用，必须标 `source=MODEL` 与 confidence。

---

## 6. 复现

```powershell
python -m pytest tests/unit/test_ae5_taxonomy.py -q --basetemp pytest-tmp -p no:cacheprovider
```
