# M3-D — Retrieval Playground

## Scope

M3-D exposes the existing M3-C snapshot-scoped hybrid retriever through a developer-facing
retrieval debug API and a minimal web playground. It does not add answer generation, chat,
query rewrite, Citation QA, snapshot creation, or `LATEST` resolution.

## Backend

`POST /api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/retrieval/playground`

The request requires a concrete `knowledge_snapshot_id` and accepts bounded dense, sparse,
candidate, and final top-k values. `final_top_k` cannot exceed `candidate_top_k`.

The response contains:

- final evidence with document, revision, locator, retrieval/rerank scores, and a 1,000-character
  maximum snippet;
- `dense`, `sparse`, `fused`, and `rerank` stages with latency and safe result projections;
- snapshot and total latency.

Trace stage metadata is enriched with one workspace/knowledge-base/snapshot-scoped PostgreSQL
query. Provider objects, vectors, point IDs, paths, raw SQL, and raw provider exceptions are not
returned.

Production API composition uses real BGE and Qdrant adapters. Tests inject deterministic fake
retrieval components explicitly. The endpoint retains the M1 WorkspaceExecutionContext and
`knowledge_run` permission boundary.

## Frontend

The page is available at `/knowledge/playground`. It provides workspace, knowledge base,
concrete snapshot, query, and bounded top-k inputs, followed by:

- snapshot / total latency / final evidence summary;
- Dense / Sparse / Fused / Rerank stage cards;
- locator and revision display;
- bounded final evidence snippets;
- initial, loading, empty, and safe error states.

The developer Playground requires an explicit Bearer access token in a password input. The token
is held only in current React state, is not persisted to localStorage or cookies, and never enters
the URL or retrieval request body. The page does not implement a complete Auth UI; the backend
remains responsible for real authentication and workspace permission checks.

By default, browser requests use the relative `/api/...` path and Next.js same-origin proxy.
The server-only `AGENTHUB_API_PROXY_TARGET` controls the FastAPI destination and defaults to
`http://127.0.0.1:8000`. An explicit `NEXT_PUBLIC_API_BASE_URL` remains available for environments
that intentionally use a directly configured API base.

## Verification

- M3-D API unit tests use explicit fake retrieval components and cover limits, concrete snapshot
  validation, tenant/snapshot scoping, permission denial, safe provider errors, trace stages,
  locator/revision enrichment, and bounded snippets.
- Frontend verification is `npm ci --no-audit --no-fund` and `npm run build`.
- M3-E Citation QA has not started.
- M3-F Snapshot lifecycle / `LATEST` resolution has not started.
- M3 overall is not yet PASS until the milestone acceptance checks and CI are recorded.
