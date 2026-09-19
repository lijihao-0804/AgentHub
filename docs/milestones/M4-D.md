# M4-D — Context Budget, Streaming and AgentHub Event Protocol

Status: PASS — final review closure verified in GitHub Actions run #67.

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

Implementation commit: `1980640`.

Final review correction commit: `5aef5c5`.

GitHub Actions run #67 (`35370216514`) passed backend and frontend. The backend job explicitly
passed the
dedicated `tests/integration/test_m4d_streaming_budget.py` step, as well as the M4-A/B/C
regressions, Alembic checks, Ruff, non-integration tests, and existing infrastructure services.
The local M4-D PostgreSQL integration suite also passed 8 tests and the frontend production
build passed.

The dedicated integration suite uses deterministic fake model adapters and is intended to run
against the existing real PostgreSQL, Redis, Qdrant and Celery CI services; it does not call
public LLMs or download BGE/HuggingFace models.

Context budget uses a deliberately conservative offline UTF-8-byte estimator; provider-specific
tokenizers remain a future optimization.

The AgentHub event envelope is canonicalized as `event_id`, `type`, `request_id`, `run_id`,
`step_id`, `timestamp`, and `payload`. Local `sequence` and `agent_version_id` are documented
extensions only; M4-D does not claim SSE replay semantics.

M4-D is accepted. M4-E and M5 remain outside this milestone; no durable checkpoint or Approval
Runtime was added.
