# M3-E — Citation QA

## Scope

M3-E adds the API-first, evidence-grounded Citation QA path. It does not add a frontend QA
page, snapshot creation, `LATEST` resolution, query rewriting, or M3-F functionality.

## API and tenant boundary

`POST /api/v1/knowledge/query` accepts:

- `workspace_id`
- `knowledge_base_id`
- concrete `knowledge_snapshot_id` (a UUID; `LATEST` is not accepted in M3-E)
- `model_profile_id`
- `query` bounded to 1–4,000 characters

The request requires the existing `knowledge_run` permission. The workspace context is resolved
from the authenticated principal through `TenantService.get_workspace_access`; client-supplied
workspace fields do not construct or override authorization context. Knowledge base, snapshot,
and model profile access remains workspace-scoped.

## Citation QA contract

The independent `CitationQaService` reuses `HybridKnowledgeRetriever.retrieve_with_trace` with
the fixed M3-C limits (30 dense / 30 sparse / 20 fused candidates / 6 final reranked evidence).
Evidence is numbered in rerank order and bounded by `knowledge_qa_max_evidence_chars` (16,000 by
default). Citation excerpts are capped at 1,000 characters.

The service sends only the selected evidence to M2 `ModelGateway.generate`. The system prompt
marks evidence as untrusted reference data, rejects embedded instructions and world knowledge,
requires the answer language to follow the query, and requires `[n]` support for factual claims.
The requested strict structured output is:

```json
{"answer":"...","citation_ids":[1],"insufficient_evidence":false}
```

Model output validation rejects malformed JSON, unknown citation IDs, invalid markers, citation
set mismatches, missing citations for non-insufficient answers, and citations on insufficient
answers. Invalid output maps to `CITATION_QA_INVALID_MODEL_OUTPUT` without exposing provider
details. Configuration errors are safe 4xx responses; provider/timeout failures are safe 503
responses.

When a concrete snapshot has no evidence, the gateway is not called. The deterministic answer is
`Insufficient evidence to answer from the selected knowledge snapshot.` with no citations and the
retrieval trace preserved.

## Safety and trace

The response exposes provider-neutral citations and retrieval trace stages only. It does not
return vectors, Qdrant point IDs, provider credentials, absolute blob paths, raw model errors,
or full document text in the trace. Production composition uses the real SQLAlchemy model
gateway and real retrieval adapters; tests inject deterministic retrieval components and an
explicit fake model gateway.

## Verification and acceptance

- Unit/API tests cover no-evidence short circuit, citation validation, prompt-injection
  separation, evidence and excerpt bounds, permissions, snapshot scope, safe model errors, and
  response redaction.
- Integration coverage uses real PostgreSQL tenant/auth/snapshot access and real Qdrant with
  deterministic fake embedding/sparse/rerank components and an explicit fake model gateway.
- Required checks: locked Ruff, Alembic upgrade/check, non-integration pytest, M3-E PostgreSQL /
  Qdrant integration, `git diff --check`, and frontend install/build.
- GitHub Actions continues to use PostgreSQL, Redis, Qdrant, and Celery without downloading BGE
  weights or calling a public LLM.

M3-F Snapshot lifecycle / `LATEST` resolution and M4 are not started. M3 overall is not marked
PASS until the complete M3 acceptance record is finalized.
