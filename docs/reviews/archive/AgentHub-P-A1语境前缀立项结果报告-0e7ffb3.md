# AgentHub P-A1 Contextual 前缀立项结果报告（门槛未达，立项关闭）

- 输入：[召回立项执行计划](AgentHub-RAG召回立项执行计划-96587c3.md) §4（P-A1）；预注册协议：[results/contextual-prefix-20261024/protocol.json](../../benchmarks/public_rag/results/contextual-prefix-20261024/protocol.json)（门槛：两数据集 nDCG@10 各 ≥ +1.5pp 且 CI95 下界 > 0、方向一致；任一未达即停）。
- 结论先行：**FAIL，立项关闭，不做生产管线提案**。FiQA 强正向（+4.89pp，CI [+2.72,+7.22]），SciFact 负向（−1.40pp，CI 跨零）——按预注册的"两数据集同向"要求判负。事后不重释为通过、不调参重跑。

## 测量（20% 种子抽样语料、生产 BGE-M3 混合检索、配对 bootstrap 5000 次）

| 数据集 | 可评查询 | baseline nDCG@10 | prefixed nDCG@10 | Δ | 95% CI | Recall@20 Δ |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| SciFact | 64 | 0.8135 | 0.7995 | **−1.40pp** | [−3.99, +0.94] | −0.52pp [−1.56, 0.00] |
| FiQA | 252 | 0.3857 | 0.4346 | **+4.89pp** | [+2.72, +7.22] | +3.90pp [+0.26, +7.74] |

**判定：FAIL**（SciFact 未达 +1.5pp 且方向为负；FiQA 单侧达标不足以过门）。

## 机制解读（描述性，不构成重开理由）

- **FiQA（强正向）**：语料为无标题长论坛段落，原文本缺乏任何文档语境——前缀恰好补上"这段在讨论什么"，与 Anthropic 报告的受益场景一致。
- **SciFact（轻负向）**：语料为短学术摘要且索引文本已含标题拼接（title+abstract），语境本已充分；50-100 token 的第三方转述前缀反而稀释了摘要自身的词面信号（其查询也是声明式短句）。
- 方向分裂本身有信息量：**语境前缀的收益取决于"原文语境缺失程度"**——无标题长文受益，短标题文档不受益甚至受损。若未来按语料特征条件化启用，属新协议新立项（此处仅记录观察）。

## 账本（用户已批准支出）

- LLM（阿里云 OpenAI 兼容网关，`deepseek-v4.1-flash-1017`）：**16,141 次 situate 调用、5.27M tokens**（SciFact 1,847 次 100% 有效；FiQA 14,294 次 99.94% 有效，8 块回退无前缀）。一次性摄入成本，逐题账本 `prefixes-*.jsonl` + `prefix-ledger-*.json`。
- 本地 GPU（RTX 3060）：双臂双数据集 4 个隔离 Qdrant 集合 `ctxp-*`（每臂 16,141 块）索引与评测，零 API。
- 评分/gate 阶段：零调用。

## 过程缺陷与修复（诚实记录）

首轮 gate 两臂全 0——upsert payload 漏 `chunk_id` 字段致 eval 的 chunk→doc 映射落空；修复后重建双臂复测（`git log` 可见修复提交）。首轮结果已作废，未用于任何判定。

## 边界

- 20% 抽样使可评查询偏少（SciFact 64、FiQA 252）；FiQA 的 CI 显著性在此样本下已足够，SciFact 的负向点估计 CI 跨零——即 SciFact 结论更接近"无增益"而非"确定有害"。
- 英文双数据集；协议 A1-5（中文小样本交叉）因门槛未达不再执行。
- 生产管线（Celery 摄入、新 revision 语义）未做提案（停止规则）。

## 复现

```bash
PYTHONPATH=. uv run python benchmarks/public_rag/contextual_prefix_runner.py sample|prefix|index --arm X|eval|gate
# 隔离集合可删：ctxp-scifact-{baseline,prefixed}、ctxp-fiqa-{baseline,prefixed}
```

## 状态

- P-E0：已转正（版本级 opt-in，前一份报告）。
- P-C1：负结果关闭（RRF 多查询融合）。
- P-A1：负结果关闭（本报告；FiQA 正向观察保留在案）。
- 计划剩余：P-B1（解析表格，需金标语料前置）、P-D1（实体图，前置 C1/A1 结论已出——C1 负、A1 负，按计划 §6"两者皆负则重估"，建议与用户重估后再启）。
