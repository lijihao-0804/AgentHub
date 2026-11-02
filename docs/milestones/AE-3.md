# AE-3 — Evidence Level & Gate Rules（证据等级与门禁）

Status: **PARTIAL PASS（单元验证通过；迁移未在真实 PostgreSQL 上执行）**

基线：`69ca38b`（AE-2 之后），分支 `codex/ae-1-trajectory-evidence`。

---

## 1. 目标与边界

**目标**：让“这份分数能声明什么”成为数据库字段和门禁规则，而不是文档里的一句免责声明。

**做**：

- `EvaluationEvidenceLevel` 四个等级：`DETERMINISTIC_CONFORMANCE` / `CONTROLLED_SIMULATION` / `REAL_PROVIDER` / `PRODUCTION_SAMPLE`；
- `evaluation_experiments.evidence_level` 与 `evaluation_experiment_runs.evidence_level` 两列，迁移 `0049`；
- 创建实验时可指定，默认 conformance；实验 spec 冻结该等级，run 创建时复制；
- API 请求/响应暴露 `evidence_level`；
- 发布门禁新增 `REQUIRED_EVIDENCE_LEVEL` 规则：实际等级低于要求 → `FAIL`；缺失 → `INCONCLUSIVE`。

**不做（明确留给 AE-3b）**：

- `MIN_CI_LOWER_BOUND` / `NON_INFERIORITY` / `MIN_PASS_HAT_K`：需要把 AE-2 的统计接入 `comparison.metrics` 或让门禁读取 run 的 trial 数据；
- `MAX_COST_PER_SUCCESS`：现有 `MAX_VALUE` 规则已可对 `cost_per_successful_case` 设上限，不重复造规则；
- 不改变既有 6 条规则的语义。

---

## 2. 实现

| 变更 | 位置 |
| --- | --- |
| 枚举与列 | `packages/evaluation/models.py` |
| 迁移 | `migrations/versions/0049_evaluation_evidence_level.py` |
| 创建/冻结/复制 | `packages/evaluation/experiments.py` |
| API schema 与路由 | `apps/api/schemas/evaluation.py`、`apps/api/routes/evaluation.py` |
| 门禁规则 | `packages/evaluation/release_gate.py` |
| 统计响应附带等级 | `packages/evaluation/statistics_service.py` |
| 单元测试 | `tests/unit/test_ae3_evidence_level.py` |

### 2.1 等级顺序

```text
DETERMINISTIC_CONFORMANCE  <  CONTROLLED_SIMULATION  <  REAL_PROVIDER  <  PRODUCTION_SAMPLE
```

`REQUIRED_EVIDENCE_LEVEL` 只比较排序，不做语义推断。一个 conformance 实验无论任务成功率多高，都不能通过要求 `REAL_PROVIDER` 的门禁。

### 2.2 为什么冻结在实验而不是运行时推断

运行时不“知道”自己是不是真实 provider：同一个 driver 可能被脚本替换、可能走 mock。把等级作为创建者显式声明 + 冻结进 spec，才可审计。代价是：创建者可以谎报。缓解：AE-8 的 provider 调用证据与成本凭证可以交叉核验；在那之前，等级是“声明”，报告里仍必须给出实际调用证据。

---

## 3. 验证

### 3.1 已运行

```powershell
python -m pytest tests/unit/test_ae3_evidence_level.py tests/unit/test_m7f_release_gate.py `
  tests/unit/test_m7b_evaluation_api.py tests/unit/test_m7b_reproducibility.py `
  -q --basetemp pytest-tmp -p no:cacheprovider
python -m ruff check packages/evaluation apps/api migrations/versions/0049_evaluation_evidence_level.py
```

结果：全部通过，lint 通过。

### 3.2 未运行

- **迁移未在真实 PostgreSQL 上执行**（本机无 `AGENTHUB_TEST_DATABASE_URL`，Docker 被沙箱拒绝）。
- **集成测试未跑**：`test_m7b_experiments.py`、`test_m7c_experiment_runner.py`、`test_m7ef_ablation_release_gate.py` 需要真实库。
- **前端未更新**：实验列表/详情页还没有显示证据等级徽标。

---

## 4. 已知限制

1. **迁移风险**：`0049` 只加列与 check constraint，理论上安全；但没有在真实库上验证 upgrade/downgrade。部署前必须在隔离库跑一次。
2. **等级是声明**：没有自动从 provider 调用/成本凭证推断等级。
3. **统计未入门禁**：`MIN_CI_LOWER_BOUND` 等规则尚未实现，发布结论仍可能只看点估计。
4. **旧实验回填**：迁移给所有旧实验/运行填 `DETERMINISTIC_CONFORMANCE`；这是保守默认，不会把旧实验误标为真实 provider。

---

## 5. 下一步

1. **AE-3b**：把 `statistics` 的 `pass_hat_k` 与 CI 写入比较 payload，或让门禁服务按 run 现算；实现 `MIN_CI_LOWER_BOUND` / `NON_INFERIORITY` / `MIN_PASS_HAT_K`。
2. **前端**：实验/运行页面显示证据等级徽标。
3. **集成验证**：隔离库跑 `0049` upgrade/downgrade + 实验创建/门禁测试。

---

## 6. 复现

```powershell
python -m pytest tests/unit/test_ae3_evidence_level.py -q --basetemp pytest-tmp -p no:cacheprovider
```
