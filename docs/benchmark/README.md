# Retrieval benchmark

This directory documents the small M3 retrieval-only baseline. It is not the M7 evaluation
framework and it does not measure answer or citation quality.

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

The host-only run uses the production `HybridKnowledgeRetriever`, PostgreSQL, Qdrant, the existing
chunk model, BAAI/bge-m3, BAAI/bge-reranker-v2-m3, and the existing multilingual sparse encoder.
It seeds the corpus, materializes one Knowledge Snapshot, indexes the chunks, evaluates all cases,
and writes JSON plus a Markdown summary in one command:

```powershell
docker compose up -d postgres redis qdrant
$env:HF_HOME = "E:\JAVA\AI+agent\hf_cache"
$env:HF_HUB_CACHE = "E:\JAVA\AI+agent\hf_cache"
$env:HF_HUB_OFFLINE = "1"
$env:AGENTHUB_DATABASE_URL = "postgresql+asyncpg://agenthub:agenthub@127.0.0.1:5432/agenthub"
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
