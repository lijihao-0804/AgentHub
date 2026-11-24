# 阿里云 Qwen3.7 Embedding / Rerank、多切块向量检索与 MuSiQue 联合选择对照

基线提交：`43e3093`（`main`，合并 PR #27 后）。本轮在 **SciFact（Test 300 题 & 冻结 553 题确认集）**、**QASPER（32 篇长论文 / 72 题，三种切块策略）** 与 **MuSiQue（200 题冻结确认集，原版 `consumer_replay` 生产消费者链路）** 上，系统对比本地 `BAAI/bge-m3` + `BAAI/bge-reranker-v2-m3` 与阿里云百炼 `qwen3.7-text-embedding`、`qwen3.7-text-embedding-flash`、`qwen3-rerank` 及 `complementary_v1` 联合选择。

本轮同时新增了可选的 OpenAI/百炼兼容 `OpenAICompatibleDenseEmbedder` 与 `OpenAICompatibleReranker` 适配器（默认仍保持本地 BGE 配置不变，未切换生产默认）。

---

## 一、SciFact 检索与重排对比（5,183 篇 / 9,189 个 1200/200 分块）

重建的本地 BGE-M3 稀疏基线与历史 `benchmarks/public_rag/results/scifact-20261021/test-quality.json` 四位小数完全一致（`Recall@5=0.7069`、`Recall@10=0.7706`、`Recall@20=0.8211`、`MRR@5=0.5932`、`nDCG@10=0.6368`）。

### 1.1 SciFact Test 300 题（文档去重口径）

| 方案 | 阶段 | Recall@5 | Raw Top5 实际块 | Recall@10 | Recall@20 | MRR@5 | nDCG@10 |
|---|---|---:|---:|---:|---:|---:|---:|
| BGE-M3（本地基线） | Dense | 73.96% | — | 79.68% | 83.52% | 0.6082 | 0.6538 |
| BGE-M3 + Sparse RRF20 + BGE Rerank *(现网默认)* | Rerank | 80.16% | — | 86.18% | 87.51% | 0.7096 | 0.7486 |
| `qwen3.7-text-embedding-flash` | Dense | 77.37% | — | 82.09% | 84.53% | 0.6833 | 0.7140 |
| `qwen3.7-text-embedding-flash` + Sparse RRF20 + BGE Rerank | Rerank | 79.89% | 79.16% | 85.62% | 87.52% | 0.7111 | 0.7478 |
| `qwen3.7-text-embedding-flash` + Sparse RRF20 + `qwen3-rerank` | Rerank | 82.51% | 81.09% | 87.19% | 87.52% | 0.7325 | 0.7662 |
| `qwen3.7-text-embedding`（纯 Dense） | Dense | **87.86%** | — | 91.03% | 93.00% | **0.7528** | **0.7903** |
| `qwen3.7-text-embedding` + Sparse RRF20 + BGE Rerank | Rerank | 81.28% | 80.48% | 90.20% | **93.60%** | 0.7133 | 0.7648 |
| `qwen3.7-text-embedding` + Sparse RRF20 + `qwen3-rerank` | Rerank | **85.71%** | **83.39%** | **92.60%** | **93.60%** | 0.7414 | 0.7874 |
| `qwen3.7-text-embedding` 纯 Dense20 + `qwen3-rerank` | Rerank | 84.26% | 80.94% | 90.32% | 93.00% | 0.7373 | 0.7794 |

### 1.2 SciFact 553 题冻结确认集（625 金标对，SHA-256 `24010c64ddec...` 一致）

| 方案 | TOP20 来源宏召回 (命中对/625) | Raw Top5 实际块 (命中对/625) | Doc Recall@5 | MRR@5 | nDCG@10 |
|---|---:|---:|---:|---:|---:|
| BGE-M3 + Sparse RRF20 + BGE Rerank *(现网默认)* | 86.46% (539) | 79.46% (482) | 80.32% | 0.7121 | 0.7441 |
| `qwen3.7-text-embedding-flash` + Sparse RRF20 + `qwen3-rerank` | 85.01% (530) | 79.49% (487) | 80.41% | 0.7259 | 0.7530 |
| `qwen3.7-text-embedding` + Sparse RRF20 + `qwen3-rerank` | 90.89% (567) | 80.93% (495) | 82.67% | 0.7366 | 0.7749 |
| `qwen3.7-text-embedding` 纯 Dense20 + `qwen3-rerank` | **92.04% (575)** | 80.91% (495) | 82.75% | 0.7354 | 0.7727 |
| `qwen3.7-text-embedding`（纯 Dense） | **92.04% (575)** | **81.47% (497)** | **84.17%** | **0.7375** | **0.7754** |

**负结果与边界**：
- `qwen3.7-text-embedding-flash` 在 553 题确认集上 TOP20 反降（`85.01%` vs `86.46%`），不推荐替换。
- 在短摘要单跳检索上，本地 `BGE-reranker-v2-m3` 会压低 `qwen3.7-text-embedding` 的前五排序（Test 300 Recall@5 `87.86% → 81.28%`）；`qwen3-rerank` 显著优于本地 BGE 重排（`85.71%` vs `81.28%`），但仍略低于 `qwen3.7` 纯 Dense。

---

## 二、QASPER 32 篇长论文 / 72 题：切块策略 × 向量检索对照

沿用 `benchmarks/public_rag/chunk_strategy_replay.py` 的种子 `20261117`、72 道可回答题与完整标注段落判定口径，补齐了此前缺失的向量检索对照：

| 模型链路 | 切块策略 | 块数 | 完整保留上限 | 单篇重排 Top5 | 跨库 TOP20 完整证据 | 跨库重排 TOP5 完整证据 | 跨库重排 TOP6 完整证据 |
|---|---|---:|---:|---:|---:|---:|---:|
| BGE-M3 + Sparse RRF + BGE Rerank | `fixed_v1` *(现网默认)* | 808 | 48 (66.7%) | 26 (36.1%) | 27 (37.5%) | 16 (22.2%) | 18 (25.0%) |
| BGE-M3 + Sparse RRF + BGE Rerank | `structural_v1` | 1,861 | **71 (98.6%)** | 43 (59.7%) | 34 (47.2%) | 22 (30.6%) | 25 (34.7%) |
| BGE-M3 + Sparse RRF + BGE Rerank | `adaptive_v1` | 1,880 | 66 (91.7%) | 40 (55.6%) | 29 (40.3%) | 21 (29.2%) | 22 (30.6%) |
| `qwen3.7-text-embedding` + `qwen3-rerank` | `fixed_v1` | 808 | 48 (66.7%) | 30 (41.7%) | 29 (40.3%) | 17 (23.6%) | 20 (27.8%) |
| `qwen3.7-text-embedding` + `qwen3-rerank` | **`structural_v1`** | 1,861 | **71 (98.6%)** | **45 (62.5%)** | **42 (58.3%)** | **30 (41.7%)** | **31 (43.1%)** |
| `qwen3.7-text-embedding` + `qwen3-rerank` | `adaptive_v1` | 1,880 | 66 (91.7%) | 40 (55.6%) | 39 (54.2%) | 27 (37.5%) | 29 (40.3%) |

**结论**：在向量检索下，`structural_v1`（按段落切分、0 重叠）无论搭配本地 BGE 还是云端 Qwen3.7 均显著优于 `fixed_v1` 和 `adaptive_v1`，且因无 200 字符重叠，32 篇论文 Embedding 总 token 反省 8.8%（`199,559` vs `218,835`）。在长论文多文档场景中，`qwen3-rerank` 将 `structural_v1` 跨库 TOP5 完整证据从 `29.2%` 拉升至 `41.7%`（+12.5pp）。

---

## 三、MuSiQue 200 题冻结确认集（`consumer_replay` 生产 `legacy5` 链路）

在 `benchmarks/public_rag/results/consumer-rag-20261023/confirmation-candidates-1` 的 200 题候选池（`heading_fill` 臂）上，通过未修改的 `benchmarks/public_rag/consumer_replay.py` 走完整生产消费者链路（Top5 / 8000B 预算）：

| 阶段 / 方案 | 模型可见证据召回 | 全文级覆盖 | 完整证据集合率 | 2 跳召回 / 完整集 | 3 跳召回 / 完整集 | 4 跳召回 / 完整集 |
|---|---:|---:|---:|---:|---:|---:|
| 修复前旧基线（截断未修 + BGE） | 59.0% | 56.5% | 25.5% | — | — | — |
| 交付层修复（现网默认 + BGE） | 78.3% | 74.3% | 54.0% | 84.2% / 70.2% | 75.1% / 42.4% | 60.2% / 11.1% |
| `qwen3.7-text-embedding` 纯 Dense（不重排） | 74.0% | 71.0% | 46.5% | 82.0% / 66.7% | 66.7% / 23.7% | 56.5% / 11.1% |
| `qwen3-rerank`（直接重排 20 候选池） | 78.2% | 74.5% | 55.0% | 84.7% / 70.2% | 75.7% / 44.1% | 56.5% / 14.8% |
| `qwen3-rerank` + `complementary_v1`（思考截断 59/200 生效） | 79.6% | 75.9% | 58.0% | 86.8% / 74.6% | 76.3% / 45.8% | 56.5% / 14.8% |
| **`qwen3-rerank` + `complementary_v1`（`enable_thinking=False`，198/200 生效）** | **91.4%** | **87.5%** | **78.0%** | **96.1% / 92.1%** | **90.4% / 74.6%** | **74.1% / 25.9%** |

**负结果与限制**：
- 纯 Dense（不重排）在多跳题上明显回退（`78.3% → 74.0%`），说明多跳场景不能省掉精排。
- `complementary_v1` 首轮未关闭思考模式时，256 输出上限被 reasoning token 耗尽（仅 59/200 生效）；关闭思考后有效率恢复至 198/200。因额外增加 1 次 LLM 选择调用，继续保持按场景 opt-in，不改为全局默认。

---

## 四、用量、代码与测试验证

- **本轮离线对照 API 总用量**（不含本地零费用 BGE 重算）：Embedding 约 `5.83M` prompt tokens，Rerank 约 `15.91M` total tokens，Chat（`complementary_v1` 两轮）约 `696k` 输入 / `13.4k` 输出 tokens。
- **生产代码变更**：新增 `packages/knowledge/adapters/api_embeddings.py`、`packages/knowledge/adapters/api_reranker.py`，在 `packages/core/config/settings.py` 与 `packages/knowledge/composition.py` 提供 `knowledge_embedding_provider="openai_compatible"` / `knowledge_reranker_provider="openai_compatible"` 可选配置，默认仍为 `"local"`（BGE-M3 / BGE-reranker-v2-m3）。
- **测试**：`pytest tests/unit/test_knowledge_api_adapters.py tests/unit/test_knowledge_m3c_retrieval.py tests/unit/test_knowledge_model_warmup.py` 14 项全部通过；改动文件 `ruff check` 全部通过。
