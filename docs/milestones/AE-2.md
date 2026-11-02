# AE-2 — Reliability & Uncertainty（可靠性统计层）

Status: **PARTIAL PASS（纯函数与 API 契约单元验证通过；真实 PostgreSQL 集成未运行）**

> **2026-11-02 实机更新**：统一迁移链与 M7 集成测试已在真实 PostgreSQL 上通过；见 [AE 实机证据](AE-live-evidence-20261102.md)。

基线：`4598c64`（AE-1 之后），分支 `codex/ae-1-trajectory-evidence`。

---

## 1. 目标与边界

**目标**：让“这次实验更好”变成“有区间、有稳定性、有功效说明”的结论。

**做**：

- `pass^k`、跨重复方差、`consistency@k`；
- 单臂成功率的 Jeffreys 区间；
- case 级 cluster bootstrap 区间（成功率与 `pass^k`）；
- 配对 bootstrap、McNemar 精确检验、Holm 校正、MDE/所需样本量；
- 只读统计接口 `GET /experiment-runs/{run_id}/statistics`，按 variant 分别返回。

**不做**：

- 不把统计写进 `evaluation_metric_results` 或新建快照表（AE-2b）；
- 不改比较/门禁的既有字段（AE-3 再加 `MIN_CI_LOWER_BOUND` 等规则）；
- 不做多重比较的自动门禁（Holm 只提供函数）；
- 不改变任何历史实验的已存指标。

---

## 2. 实现

| 变更 | 位置 | 说明 |
| --- | --- | --- |
| 统计纯函数 | `packages/evaluation/statistics.py`（新增） | 无第三方依赖；Jeffreys 用自实现正则化不完全 Beta |
| 只读统计服务 | `packages/evaluation/statistics_service.py`（新增） | 重建 per-case trial outcome，按 variant 分组 |
| API | `apps/api/routes/evaluation.py` | `GET /experiment-runs/{run_id}/statistics` |
| 单元测试 | `tests/unit/test_ae2_statistics.py`（新增） | 10 项，含与已知数值对照 |

### 2.1 关键定义

| 指标 | 定义 | 边界 |
| --- | --- | --- |
| `success_rate` | 成功 trial / 已知 trial | 未知 trial 不进分母 |
| `pass^k` | 恰好 k 个已知 trial 且全部成功的 case 数 / 完整 case 数 | 缺失 trial 的 case 进 `incomplete_case_count`，不进分子也不进分母 |
| `trial_variance` | 每个完整 case 内 0/1 的总体方差，再对 case 取均值 | 无完整 case → `null` |
| `consistency@k` | 完整 case 中“全部成功且 k 次 output_hash 相同”的比例 | 无 output_hash → `null` |
| Jeffreys 区间 | `Beta(s+0.5, n-s+0.5)` 的 2.5%/97.5% 分位 | n=0 → `null` |
| `pass^k` 区间 | 对完整 case 的 0/1 做 cluster bootstrap（B=2000，固定 seed） | 无完整 case → `null` |
| McNemar | 双侧精确二项；n>1000 用连续性校正正态近似 | b+c=0 → p=1 |
| MDE | 由 `required_pairs` 反解：给定 n 与不一致率，能检测到的最小 delta | 样本不足 → `null` |

### 2.2 为什么按 variant 分组

一个 run 可以有多个 variant（baseline / candidate）。把不同 variant 的 trial 混在一个 `pass^k` 里会得到一个既不是 baseline 也不是 candidate 的数字。接口返回 `variants: {variant_id: summary}`，每个 summary 自带 `variant_label`。

### 2.3 为什么 Jeffreys 而不是 CLT

n<几百时 CLT 区间会系统性偏窄（ICML 2025 position，arXiv:2503.01747）。Jeffreys 是 Beta–Binomial 的等尾区间，闭式先验、无调参；本实现与 SciPy `beta.ppf` 在 5/10、0/30 两组已知值上逐位一致（测试锁定）。

---

## 3. 验证

### 3.1 已运行

```powershell
python -m pytest tests/unit/test_ae1_trajectory.py tests/unit/test_ae2_statistics.py `
  tests/unit/test_m7d_metrics.py tests/unit/test_m7b_reproducibility.py `
  -q --basetemp pytest-tmp -p no:cacheprovider
python -m ruff check packages/evaluation apps/api/routes/evaluation.py
```

结果：47 项通过；lint 通过。

### 3.2 已知数值对照

| 检查 | 期望 | 实测 |
| --- | --- | --- |
| Jeffreys 5/10 | `[0.22352867025270517, 0.7764713297472947]` | 一致（与 SciPy 对照） |
| Jeffreys 0/30 上界 | `0.07967817381726` | 一致 |
| McNemar(10,2) | `158/4096 = 0.03857421875` | 一致 |
| 检测 5pp / 20% 不一致率所需配对 | ≈625 | 626 |
| n=626 的 MDE | ≈5pp | 5.002pp |

### 3.3 未运行

- 真实 PostgreSQL 集成：`statistics_service` 未在真实库上跑过；`_load_and_validate_case_set` 的 409 路径（未终态 run）未做集成验证。
- 真实模型实验：没有真实 provider 数据，`pass^k` 在生产模型上的分布未知。
- UI：没有统计页面；接口返回 JSON。

---

## 4. 已知限制

1. **Jeffreys 假设 trial 独立**：同一 case 的重复运行可能因缓存/温度相关，区间会偏窄；`pass^k` 的 bootstrap 已按 case 重采样，缓解了一部分。
2. **没有冻结统计快照**：每次 GET 重算；同一份数据 + 固定 seed 可复现，但历史统计没有独立 hash。AE-2b 补。
3. **没有接入比较/门禁**：`PairedMetric` 仍是 win/tie/loss；`MIN_CI_LOWER_BOUND`、`NON_INFERIORITY` 未实现。AE-3 做。
4. **MDE 需要不一致率**：接口不猜不一致率；调用方要么提供，要么只拿 `required_pairs` 做规划。
5. **Holm 只提供函数**：没有自动对一次实验的所有指标做校正；需要门禁层决定 primary metric 集合。

---

## 5. 下一步

1. **AE-2b**：`evaluation_statistical_snapshots` 表 + 冻结 payload hash + 比较接口返回 CI。
2. **AE-3**：`evidence_level` 字段、`MIN_CI_LOWER_BOUND` / `NON_INFERIORITY` / `MIN_PASS_HAT_K` / `MAX_COST_PER_SUCCESS` 门禁规则。
3. **集成验证**：有 PostgreSQL 的环境跑 `statistics` 接口与 409 路径。

---

## 6. 复现

```powershell
python -m pytest tests/unit/test_ae2_statistics.py -q --basetemp pytest-tmp -p no:cacheprovider
```
