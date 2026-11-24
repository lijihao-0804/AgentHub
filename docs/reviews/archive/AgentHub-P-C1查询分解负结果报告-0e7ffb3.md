# AgentHub P-C1 查询分解立项负结果报告（C1-4 门槛未达，立项停止）

- 输入：[召回立项执行计划](AgentHub-RAG召回立项执行计划-96587c3.md) §3（P-C1）；预注册协议：[results/query-decompose-20261024/protocol.json](../../benchmarks/public_rag/results/query-decompose-20261024/protocol.json)。
- 结论先行：**C1-4 无 GPU 判定 FAIL，按预注册停止规则终止 P-C1**。hop2/3/4 记录级召回@5 变化全部为负且 95% CI 上界 < 0。死于最便宜的阶段：总计 200 次 LLM 分解调用（32,506 tokens，预算内），**零答案生成支出**，未触碰生产代码。
- 边写边测纪律执行情况：门槛数值为协议预注册值，未事后放宽；负结果账本全部保留。

## 测量结果（200 题冻结 MuSiQue 确认集，同口径两臂）

两臂共享：题内冻结语料（20 段）、同一确定性 BM25 词面排序器（k1=1.5/b=0.75）、同一切割@5；唯一差异 = 分解+多查询 RRF 融合（N 路、k=60，与生产 `fuse_reciprocal_rank` 同公式推广）。

| hop | n | Δrecall@5 | 95% CI | 预注册门槛 | 判定 |
| --- | ---: | ---: | --- | --- | --- |
| 2 | 114 | **−6.58pp** | [−12.50, −0.43] | ≥ −1.0pp（守护） | 违反守护 |
| 3 | 59 | **−13.56pp** | [−19.88, −7.27] | ≥ +5.0pp 且 CI 下界>0 | FAIL |
| 4 | 27 | **−18.52pp** | [−26.92, −11.29] | ≥ +5.0pp 且 CI 下界>0 | FAIL |

分组 bootstrap 5000 次（复用冻结重采样组），seed 固定。分解账本：200/200 VALID、0 回退（prompt 24,389 + completion 8,117 tokens）。

## 为什么是负的（抽查定案的机制，非实现缺陷）

1. **分解质量本身良好**：抽查最差两题的子问题正确覆盖跳链（如 "Where was Ibn Abbad al-Rundi born?" → "When did the Taifa of that birthplace cease to exist?"）。
2. **RRF 多查询病理（主因）**：在 ~20 文档的题内小语料上，"共识性干扰文档"（每个子查询都排 3~8 名）的 RRF 累计分（3-4 × 1/(60+3..8)）**超过**"专科支持文档"（只被它所属子查询排第 1：1/(60+1)）。融合奖励了泛化相关，惩罚了链上专门命中——top5 被干扰占据，支持文档被挤出。实例：`2hop__539184_119915` 基线命中 2/2 金标、融合命中 0/2。
3. **分解漂移被放大**：模型分解与金标跳链措辞/实体漂移时（`3hop2__145209_20335_9331` 的子问题引入了金标外的中间实体），单查询的词面直查尚能靠原句词面命中，多查询融合则把漂移的共识推到前排（基线 3/3 → 融合 1/3）。

## 边界（负结果的适用范围）

- 本门测量的是**词面排序器（BM25）下的分解+RRF 融合**；协议已声明这是零检索模型成本的代理。结论：该机制在小题内语料 + 词面排序设定下**不成立**，且方向为显著负。
- 不构成对"生产 dense 检索 + 分解融合"的证伪；但按计划纪律（§0.2 负结果不调参重跑、不扫参数），本仓**不再为该方向追加词面或小语料变体实验**。若未来要在 dense 检索上重开，属新协议新立项（须先解决共识干扰的排序聚合问题，例如按子查询分配名额而非纯 RRF——此为观察，非已验证方案）。
- R-08（多跳/补检索）的其余路线不受影响：证据组织侧（互补集合已 CONFIRMED +16.15pp）仍是多跳的对症已验证方向。

## 账本

- LLM：200 次分解调用，32,506 tokens（`decompose-ledger.json`）；答案生成 0 次。
- 工件：`protocol.json`、`decompositions.jsonl`（逐题子问题与状态）、`per-question.jsonl`（逐题两臂召回）、`gate.json`（判定）、`decompose-ledger.json`——均在 `results/query-decompose-20261024/`。
- 代码：`benchmarks/public_rag/query_decompose.py`（分解器 + BM25 + N 路 RRF，15 个单测）、`query_decompose_runner.py`（门执行器）；`tests/unit/test_query_decompose.py`。未改任何生产代码。

## 复现

```bash
PYTHONPATH=. uv run python benchmarks/public_rag/query_decompose_runner.py --decompose  # 需 .env 网关
# 复用既有分解结果复算门槛（零调用）：
PYTHONPATH=. uv run python benchmarks/public_rag/query_decompose_runner.py
uv run --locked pytest -q tests/unit/test_query_decompose.py
```

## 后续

按计划顺序，下一立项为 **P-A1（Contextual 摄入前缀）**：A1-1 协议与 A1-2 生成器先行；**前缀生成 LLM 支出须先报估算并获批准**（计划 §4/§7），将在下一份报告给出规模与 tokens 估算。C1-5/C1-6 未执行（停止规则）。
