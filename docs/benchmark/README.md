# Benchmark 与质量证据索引

当前口径见 [状态页](../current-state.md)。本目录包含各阶段 benchmark 报告；不同数据/评估器/基线不能混算。本轮文档同步不重跑付费或大规模历史实验。

| 范围 | 报告/产物 | 指标边界 |
| --- | --- | --- |
| M3 retrieval-only | [M3 baseline](m3-retrieval-baseline.md)、[runner](../../benchmarks/retrieval/runner.py) | 候选/最终召回与 MRR，不测生成答案质量 |
| M4 runtime | [baseline](m4-agent-runtime-baseline.md) | 生产执行路径的受控用例，非线上成功率 |
| M7 评测契约与消融 | [统一评测](m7-unified-evaluation.md)、[retrieval ablation](m7e-retrieval-ablation.md) | 区分 deterministic conformance 与真实策略评测 |
| M-I3/M-I4 客服与 RAG | [正式验收](../reviews/AgentHub-面试增强M-I3-M-I4验收报告-20261003.md) | 60 合成客服/360 trial；144 实际检索生成；助手审阅非真人一致性 |
| Memory 质量边界 | [11 场景结果](../../benchmarks/evaluation/memory_quality/summary.md)、[JSON](../../benchmarks/evaluation/memory_quality/result.json)、[runner](../../benchmarks/evaluation/memory_quality/runner.py) | WRITE/RECALL/USE 分开；真实复杂 USE 未测，无新增付费调用 |

## Memory 小探针：如何读或复现

优先阅读已提交结果，不需要为看 README 再跑。5/8 禁止召回仍准入；6/9 是强制候选门禁结果，10/10 是脚本消费者结果，均不代表真实模型准确率。矛盾 ACTIVE 共存，没有自动替换。

如改动相关代码后需要复现，显式创建并迁移独立测试库（下面名称是示例，不自动创建）：

```powershell
$env:AGENTHUB_TEST_DATABASE_URL = "postgresql+asyncpg://agenthub:agenthub@localhost:5432/agenthub_memory_quality_test"
uv run --locked python -m benchmarks.evaluation.memory_quality.runner --database-url $env:AGENTHUB_TEST_DATABASE_URL --output .scratch/memory-quality-result.json --summary .scratch/memory-quality-summary.md
```

输出目录先创建。runner 会写入隔离合成 workspace、Thread、Run 和 Memory；不要指向共享业务库。它替换模型 gateway，复用真实 worker 解析/引文/store/selector/admission，默认付费调用 0。

## 历史 M3 retrieval-only 复现说明

以下保留 M3 的数据、指标和入口；不是 M7 生成评测，也不测答案/引用质量。

The dataset and runner live in [`benchmarks/retrieval`](../../benchmarks/retrieval/). The fixed
dataset contains ten self-authored multilingual enterprise documents and 30 retrieval cases:
20 `dev` and 10 `holdout`. Each case identifies a document revision and a section locator, so
evaluation does not depend on database UUIDs.

## Validate the dataset

```powershell
uv run --locked python -m benchmarks.retrieval.runner --validate-only
```

The validator rejects duplicate case IDs, duplicate queries, malformed locators, empty ground
truth, unknown document/revision keys, missing splits, and an incorrect benchmark size.

## Run the real host baseline

First create and migrate the dedicated test database shown below; never seed a shared business database.
The host-only run uses the production `HybridKnowledgeRetriever`, PostgreSQL, Qdrant, the existing
chunk model, BAAI/bge-m3, BAAI/bge-reranker-v2-m3, and the existing multilingual sparse encoder.
It seeds the corpus, materializes one Knowledge Snapshot, indexes the chunks, evaluates all cases,
and writes JSON plus a Markdown summary in one command:

```powershell
docker compose up -d postgres redis qdrant
$env:HF_HOME = "E:\JAVA\AI+agent\hf_cache"
$env:HF_HUB_CACHE = "E:\JAVA\AI+agent\hf_cache"
$env:HF_HUB_OFFLINE = "1"
$env:AGENTHUB_DATABASE_URL = "postgresql+asyncpg://agenthub:agenthub@127.0.0.1:5432/agenthub_retrieval_test"
$env:AGENTHUB_QDRANT_URL = "http://127.0.0.1:6333"
uv run --locked python -m benchmarks.retrieval.runner --real-models
```

Use `--database-url`, `--qdrant-url`, `--collection`, `--output`, or `--summary` to make the
run destination explicit. The runner records the Git commit, dataset hash, snapshot ID/hash,
retrieval configuration, model identities, device, timestamp, split metrics, and failure cases.

## Metrics

For a case with one or more ground-truth spans, a retrieved hit matches when its logical
document/revision keys match and its locator matches the ground-truth locator. Section and page
locators require the same key; text ranges match when their intervals overlap.

- Candidate Recall@20 = macro-average of the fraction of ground-truth spans hit in the fused RRF
  candidate top 20.
- Final Recall@5 = macro-average of the fraction of ground-truth spans hit in the reranked final
  top 5.
- MRR@5 = macro-average of `1 / first relevant rank` in the final top 5, or zero if none matches.

Failure categories are deterministic: `FIRST_STAGE_MISS` means no ground-truth span is in the
candidate top 20; `RERANK_DROP` means it is in candidates but not final top 5; and
`FINAL_RANK_LOW` means the first relevant final result is below rank 1.

CI validates the schema, metrics, failure classification, stable dataset hash, and fake runner
wiring only. It never downloads BGE checkpoints or requires a GPU. The real baseline is a manual
host acceptance run.
