# AgentHub 上下文预算统一修复实施报告（工作单 FIX-01~FIX-11）

- 输入：[上下文预算统一修复工作单](AgentHub-上下文预算统一修复工作单-0730a94.md)；协议：[results/budget-unify-20261025/protocol.json](../../benchmarks/public_rag/results/budget-unify-20261025/protocol.json)。
- 结论先行：**11 项全部处置完毕，验收总门全过**——语言中性（同池同 k 中英文均 5/5 完整送达，修复前中文 1/5）、16k 窗口可准入 12k 真实 token、窗口利用率入 trace、池随窗口派生、@8000 预算送达 7.05 条/题（>5）、抽样 A/B **+2.08pp**（非劣效门通过且正向）、四臂重放零回归（gold 1.000）、全量单测与集成 exit 0、ruff 全过。LLM 支出：**1 次 A/B ≈ 200 次调用**（100 题，抽样纪律内；批次一 A/B 因消息逐字节相同（200/200 离线证明，`ab-identity-proof.json`）按工作单合并条款归入本次）。

## 逐项处置

| 编号 | 实现 | 验证 |
| --- | --- | --- |
| FIX-01 | `ApproxTokenEstimator`（CJK c/1.6 + 其他 a/4，离线确定性）；旧名保留 deprecated 别名；runtime 三处引用更名 | 中英同量级断言 + 与旧字节口径比值 3~5x + 确定性单测 |
| FIX-02 | `ContextBudgetUsage.window_utilization` 入 trace（白名单同步）；16k/12k 回归测试 | 终报实验 2 反转为 PASS；满池利用率 ≥0.38（上限=25%+15% 份额减元数据） |
| FIX-03 | `derive_context_budget/derive_memory_tokens`（25%/15%/8%）；publish 侧 `_resolved_runtime_config`：默认配置→按链上最小窗口派生，显式配置优先不回改，低于派生给 INFO | 32k→8192/4915、16k→4096/2457 单测；显式值保持 |
| FIX-04 | `_bounded_tool_result` 对 `data.results` 列表改逐条整取整弃 + `dropped_results` 计数；非列表保持前缀切割 | 5 条记录不腰斩单测 |
| FIX-05 | Citation QA 证据选择改统一估计器（同记账）；设置名保留、语义注明 | QA 选择测试按近似单位重校 |
| FIX-06 | `limit` 语义=上限（默认 10、schema 补 default/description），预算治理实际送达 | replay 新增 legacy10 臂：**7.05 条/题、召回 0.783→0.848、完整集合 0.540→0.660** |
| FIX-07 | 新记账下 compact5 vs rag_records 重放对比：成员相同、3900 vs 5466 字节（−28.6%），无 ≥+25% 容量增益 → 维持 opt-in，记录关闭（沿 G-02 报告结论，数字在新记账下复核一致） | replay-b1 四臂 |
| FIX-08 | 选择器上限常量入 `contracts.py`（`MAX_EVIDENCE_ITEMS/MAX_SELECTION_CANDIDATES`），组合器引用同一源 | 既有选择器测试全过 |
| FIX-09 | `bootstrap_langgraph_checkpoint.py` 补 sys.path 处理（对照真入口写法） | 直接执行 exit 0 且真实 bootstrap 成功 |
| FIX-10 | G-02 报告补 complete_records=0 形态错配脚注 | 已入报告 |
| FIX-11 | **定因**：`agenthub-api-1` Exited(1) = docker 环境下 `AGENTHUB_AUTH_JWT_SECRET` 沿用默认值被显式配置校验拒绝（pydantic ValidationError 日志）；环境配置问题非代码/迁移，留档不动业务代码 | docker logs 证据 |

## 抽样 A/B（合并批次一+二，100 题 ≈200 调用，seed 20261024）

| 臂 | F1 | EM | 拒答率 | 平均 prompt tokens |
| --- | ---: | ---: | ---: | ---: |
| before（legacy5 消息，5 条/题） | 0.4121 | 0.3100 | 0.450 | 1928 |
| after（legacy10 消息，7.05 条/题） | **0.4329** | **0.3400** | 0.420 | 2544 |

**ΔF1 +2.08pp**（门槛 ≥−1.0pp：通过且正向），未触发升级路径。196 有效/4 网关 PROVIDER_ERROR（计入成本未计入评分分母之外——按口径计 0 分，保守）。

## 语义变更声明（配置者须知）

- 所有"字节"命名字段（`context_budget_bytes`、`max_retrieval_tokens` 等）语义变为**近似 token 单位**（校验范围不变）；**已发布 AgentVersion 冻结数值不回改**，仅新版本按新语义生效。
- `search_knowledge` 的 `limit` 默认 5→10（上限语义，预算治理送达量）；显式传 limit 的调用方行为不变。
- 新发布版本的池默认值按模型窗口派生（25%/15%/8%），显式配置优先。

## 账本与复现

- LLM：~200 次调用（deepseek-v4.1-flash-1017，Aliyun 网关）；其余全部零 API。
- 工件：`results/budget-unify-20261025/`（协议、语言中性、消息相同性证明）；replay/A/B 在 `.scratch/rag-fix-verify/`（replay-b1/b2/b2c、ab_batch_results.json）。
- 四件套：全量单测 exit 0、ruff 全过、集成 exit 0（Docker 栈）、前端未动。

```bash
uv run --locked pytest -q -m "not integration"
AGENTHUB_TEST_DATABASE_URL=... uv run --locked pytest -m integration
PYTHONPATH=. uv run python benchmarks/public_rag/consumer_replay.py ... --profiles legacy5,legacy10,gold_budgeted
uv run python .scratch/rag-fix-verify/ab_batch.py 100
```
