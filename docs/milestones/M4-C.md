# M4-C — Agent Run + LangGraph Runtime

Status: IMPLEMENTED — acceptance pending final GitHub Actions verification.

## Scope

M4-C adds a non-streaming Agent Run execution path over a real LangGraph `StateGraph`.
Only immutable, hash-verified `AgentVersion` records are executable. The runtime uses the
published model/tool projections, bounded READ tool execution, safe untrusted observations,
and short independent database transactions for `AgentRun` and `RunStep` history.

This milestone does not include streaming, ContextBudgetPolicy, durable LangGraph checkpoints,
interrupt/resume, ApprovalRequest persistence, Agent Builder UI, evaluation datasets, M4-D,
or M5.

## Frozen execution contract

The M2 gateway now exposes `generate_resolved()` for a `ResolvedModelExecutionPlan`. The
published primary and fallback profile identities, provider, model parameters, timeout,
capabilities and retry policy are read from the immutable version. The runtime loads only the
current secret by the frozen `credential_ref` and fails closed on missing, disabled, cross-
workspace or provider-mismatched credentials. Mutable ModelProfile edits do not change an old
run; secret rotation remains allowed.

## Runtime and persistence

The graph nodes are `prepare`, `model`, `tool_proposal`, `policy`, `read_execute`, `observation`
and `finish`, connected with `START`/`END`. M4-C permits only `RUNNING`, `SUCCEEDED` and `FAILED`;
approval-required tools fail with `TOOL_APPROVAL_NOT_AVAILABLE` and do not enter a waiting state.
Guard limits are persisted as safe step metadata, while prompt text, model output, tool
arguments/results, knowledge content, credentials and customer PII are excluded from `RunStep`.

Migration: `0009_m4c_agent_runtime`.

## Verification

The dedicated acceptance command is:

```powershell
uv run --locked pytest tests/integration/test_m4c_agent_runtime.py -vv -rs
```

The test suite uses real PostgreSQL with deterministic fake model adapters and covers successful
LangGraph execution, frozen model mutation, frozen fallback, fail-closed incomplete fallback,
approval terminal behavior, untrusted tool observations, prompt-injection handling, guard
limits, and workspace scoping. GitHub Actions runs this step after the existing M4-B integration
step with the same PostgreSQL, Redis, Qdrant and Celery services; no BGE download or public LLM
call is used.

M4-C is not accepted until backend, frontend, dedicated integration and non-integration CI are
all PASS. M4-D remains the next milestone.
