# AE-4 — Judge Reliability（judge 可靠性）

Status: **PARTIAL PASS（聚合与 manifest 单元验证通过；未接校准队列）**

> **2026-11-02 实机更新**：已用 Alibaba `deepseek-v4.1-flash` 做 12 次真实 judge 调用（3 case × 1 单评 + 3 ensemble），3 个 case 全部 SCORED。详见 [AE 实机证据 §3](AE-live-evidence-20261102.md)。

基线：`61528c0`（AE-3 之后），分支 `codex/ae-1-trajectory-evidence`。

---

## 1. 目标与边界

**目标**：让开放式答案的 judge 分数至少满足三件事：**多 judge**、**不一致可见**、**身份可冻结**。

**做**：

- `FrozenJudgeEnsemble`：多个 `FrozenJudgeProfile` + 聚合策略，整体 hash 成 evaluator version；
- `JudgeEnsemble`：顺序调用成员，按 `MAJORITY` / `MEAN` 聚合；成员失败或结果不可解析时按法定人数（quorum）判断；
- 分歧时不取平均：三维及以上且维度一致率 <0.5 → `UNPARSEABLE / judge_disagreement`；
- worker 根据 manifest 自动选择单 judge 或 ensemble；
- verdict 记录 `judge_agreement`、`judge_strategy`、`member_verdicts`（只有 hash/score/status，无文本）；
- 单元测试 6 项 + 既有 judge 测试。

**不做（留给 AE-4b）**：

- 校准队列/API/UI（`judge_agreement`、`judge_kappa` 仍是离线函数）；
- 位置交换与长度惩罚的运行时控制；
- judge 调用成本的独立计量；
- 真实 judge 调用（无 provider 凭证/预算）。

---

## 2. 实现

| 变更 | 位置 |
| --- | --- |
| `FrozenJudgeEnsemble` / `JudgeEnsemble` | `packages/evaluation/judge.py` |
| manifest 支持 `judge_ensemble` | `packages/evaluation/judge.py`、`packages/evaluation/reproducibility.py` |
| worker 选择单 judge / ensemble | `apps/worker/tasks/evaluation.py` |
| driver 类型放宽 | `packages/evaluation/runner.py` |
| 单元测试 | `tests/unit/test_ae4_judge_ensemble.py` |

### 2.1 聚合规则

| 场景 | 结果 |
| --- | --- |
| 1 个成员 | 直接采用该成员 verdict（与旧行为一致） |
| 2 个成员、`MEAN` | 每个维度取两位 judge 的均值并四舍五入 |
| ≥3 个成员、`MAJORITY` | 每个维度取众数；众数并列时取并列值的中位数 |
| 成员失败数 > 半数 | `FAILED / judge_ensemble_quorum_not_met` |
| ≥3 个成员且平均维度一致率 <0.5 | `UNPARSEABLE / judge_disagreement` |

### 2.2 为什么分歧不取平均

平均会把“两个 judge 意见相反”伪装成一个温和的中间分。平台的口径是：**不确定就标不确定**。`UNPARSEABLE` 会让 `answer_quality` 变成 `NOT_AVAILABLE`，而不是一个看起来可比的 0.6。

### 2.3 身份冻结

`FrozenJudgeEnsemble.projection()` 包含每个成员的 provider/model/参数/rubric 和聚合策略；`evaluator_version = jv1-<hash16>`。换成员、加成员、换策略都会改变版本，比较路径会拒绝跨版本配对。

---

## 3. 验证

### 3.1 已运行

```powershell
python -m pytest tests/unit/test_ae4_judge_ensemble.py tests/unit/test_m7d_answer_quality_judge.py `
  -q --basetemp pytest-tmp -p no:cacheprovider
python -m ruff check packages/evaluation/judge.py packages/evaluation/reproducibility.py `
  packages/evaluation/runner.py apps/worker/tasks/evaluation.py
```

结果：23 项通过，lint 通过。

### 3.2 已覆盖的用例

- 3 judge 多数聚合 → `SCORED`，agreement=2/3；
- 2 judge `MEAN` → 四舍五入；
- 3 judge 全部不同 → `UNPARSEABLE / judge_disagreement`；
- 3 judge 中 2 个失败 → `FAILED / quorum`；
- ensemble manifest 往返后 evaluator version 一致；
- 不同 rubric 不能组成 ensemble。

### 3.3 未运行

- 校准集与 kappa 的生产流程；
- worker 端集成（无 PostgreSQL/Celery 环境）；真实 API 探针已做，见 [AE 实机证据](AE-live-evidence-20261102.md)。

---

## 4. 已知限制

1. **没有校准闭环**：`judge_agreement` 记录了，但没有抽样人工标签、没有 kappa 计算入口。
2. **没有位置偏差控制**：pairwise 场景尚未做 A/B 交换。
3. **成本未计量**：ensemble 的多次 judge 调用成本没有进入实验成本。
4. **并发是顺序调用**：`JudgeEnsemble.judge` 目前顺序 `await` 每个成员；成员多时延迟叠加。后续可改 `asyncio.gather`，但要注意 provider 限流。
5. **成员失败处理**：单个成员失败会被丢弃；只有 quorum 不足才整体失败。这是“可用性优先”的取舍，报告里必须写实际参与评分的成员数。

---

## 5. 下一步

1. **AE-4b**：校准 API/UI（抽样、人工标签、kappa）、位置交换、judge 成本计量。
2. **并发**：`asyncio.gather` + 超时/限流。
3. **集成**：真实 judge 调用 + worker 集成测试。

---

## 6. 复现

```powershell
python -m pytest tests/unit/test_ae4_judge_ensemble.py -q --basetemp pytest-tmp -p no:cacheprovider
```
