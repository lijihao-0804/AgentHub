# M3-C — Embedding / Qdrant / Hybrid Retrieval / Rerank

## 已完成范围

- provider-neutral `DenseEmbedder`、`SparseEncoder`、`Reranker`、`VectorIndex` contracts；
- BGE-M3 dense adapter、BGE-M3 tokenizer sparse baseline、BGE reranker adapter，均 lazy load；
- CI/test-only deterministic fake models；CI 不下载 BGE 模型；
- Qdrant 单一 named-vector collection：`dense`（Cosine）与 `sparse`（IDF modifier）；
- deterministic point id、payload scope、wait=true upsert 与 schema mismatch hard fail；
- stage-aware ingestion：`EMBEDDING → INDEXING → READY/SUCCEEDED`；
- embedding 不写 PostgreSQL，重算是 intentional and idempotent；
- PostgreSQL transaction/lease/CAS finalize 与 revision replacement/out-of-order lifecycle；
- snapshot-scoped `Dense Top30 + Sparse Top30 + equal-weight RRF Top20 + Rerank Top6`；
- Qdrant 查询本身执行 workspace / KB / snapshot revision filter；
- safe `KNOWLEDGE_INDEX_UNAVAILABLE` 503 与 provider-neutral RetrievalTrace；
- production worker 默认始终使用真实 BGE/Sparse/Reranker composition，Fake 仅由 test-only
  dependency injection 提供；
- provider error 明确区分 terminal configuration/data failure 与 bounded retryable
  provider/infrastructure failure；
- ingestion `knowledge.ingest` safe projection、Qdrant unavailable recovery、lease takeover
  protection、deterministic point id duplicate integration coverage；
- Qdrant sparse schema 显式校验 IDF modifier；retrieval reranker span 在异常路径正常结束。

## 验收记录

| 项目 | 结果 |
|---|---|
| M3-C1 unit tests | PASS |
| M3-C1 PostgreSQL + Qdrant indexing integration | PASS |
| M3-C2 RRF / retrieval unit tests | PASS |
| M3-C2 PostgreSQL + Qdrant retrieval integration | PASS |
| Qdrant unavailable safe error | PASS |
| M3-C closure unit tests | PASS |
| M3-C closure PostgreSQL + Qdrant integration | PASS |
| M3-B Celery worker regression with explicit test composition | PASS in CI / local temp ACL caveat |
| Ruff | PASS |
| Alembic upgrade/check | PASS |
| Frontend build | PASS（Next.js production build） |
| GitHub Actions | PASS（closure CI #34，backend/frontend） |
| Platform-specific PyTorch lock resolution | PASS（Windows cu130 / Linux CPU artifacts） |
| Windows cu130 runtime sync | PENDING（本机 `uv sync --locked` 仍在后台安装） |
| REAL MODEL SMOKE | NOT RUN |

## 明确未完成

- M3-D Playground；
- M3-E Citation QA；
- M3-F full Snapshot lifecycle / LATEST resolution；
- M3 overall PASS；
- M8 Docker build / full Compose deployment。
