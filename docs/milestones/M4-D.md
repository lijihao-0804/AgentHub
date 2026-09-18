# M4-D — Context Budget, Streaming and AgentHub Event Protocol

Status: IN PROGRESS

## Scope

M4-D adds a provider-neutral context budget policy, frozen model streaming, the AgentHub
run-event envelope, and the SSE transport surface. It reuses the existing M4-C AgentRun
LangGraph and ToolRuntime path; it does not add a second streaming graph.

The milestone covers mandatory context admission, bounded RAG and tool-result projection,
conversation trimming with atomic tool exchanges, frozen primary/fallback streaming semantics,
pre-visible retry/fallback, post-visible interruption, safe usage/tool events, and client
cancellation persistence as `FAILED / AGENT_STREAM_CANCELLED`.

M4-D does not include durable LangGraph checkpoints, Approval Runtime, `WAITING_APPROVAL`,
Evaluation Dataset, M4-E, or M5.

## Verification

Final acceptance is pending. The dedicated integration suite will run against the existing
real PostgreSQL, Redis, Qdrant and Celery CI services with deterministic fake model adapters;
it will not call public LLMs or download BGE/HuggingFace models.

The final record will include the context budget, frozen streaming, Event Protocol/SSE,
cancellation, M4-A/B/C regressions, non-integration tests, Alembic checks, frontend build,
and the final GitHub Actions run.
