# Post-M5 Independent Review / M6-A Closure

Status: PASS — forward-fix closure on `m6/observability`.

This record closes the independent review findings against the frozen M5-A baseline without
backporting or rewriting M5 history.

## Immutable review lineage

| Item | SHA / status |
| --- | --- |
| Review baseline | `1e712dff4a8991eaa60fb39933ed7e00fc69bda6` — M5-A accepted |
| M6-A implementation | `27086f7219c9675dd7f8784991ccfd5621fdf85d` |
| Runtime closure | `2eb9b1f55e538e45f8871b74f8a94269dedfc114` |
| Final docs closure | recorded by the commit containing this document |
| Migration head | `0013_m6_operational_indexes` |
| Historical migrations | `0001`–`0012` unchanged |

## Finding disposition

### P1

Both P1 findings are fixed.

| Finding | Disposition |
| --- | --- |
| SSE producer/consumer could hang after producer-side failure | FIXED — every producer exit path performs bounded best-effort queue closure; persistence and terminal-event failures are safe-logged and do not leave the iterator waiting forever. |
| Event envelope diverged from the canonical contract | FIXED — canonical fields are `event_id`, `type`, `request_id`, `run_id`, `step_id`, `timestamp`, and `payload`; `sequence` and `agent_version_id` are documented extensions, and `data` is no longer a duplicate payload field. |

### P2

P2 production correctness findings are closed with the following explicit semantics.

| Area | Disposition |
| --- | --- |
| Durable cancellation and completion races | FIXED — durable cancellation checks occur at scheduling boundaries and terminal precedence prevents ordinary completion from overwriting `CANCEL_REQUESTED`, `CANCELLED`, or `NEEDS_ATTENTION`. |
| Reconciliation trigger and bounded scanner | FIXED — Celery beat invokes a bounded, tenant-safe reconciliation task with stale cutoff, batch size, indexes, per-run isolation, and safe `RECONCILIATION` steps. |
| Checkpoint existence | FIXED — reconciliation and resume use the provider-neutral `CheckpointProbe`; production uses the PostgreSQL checkpoint adapter. |
| Crash windows and missing checkpoint | FIXED — missing checkpoints fail closed as `APPROVAL_CHECKPOINT_MISSING`; pending approvals and recoverable run states are reconciled deterministically. |
| `CLAIMED` action recovery | FIXED — the current internal `create_ticket` action remains idempotent and explicitly replay-safe; uncertain external claims are never automatically retried and fail closed to `NEEDS_ATTENTION / ACTION_RECONCILIATION_REQUIRED`. Automatic replay of a future non-idempotent external action is intentionally deferred. |
| Qdrant and retrieval client lifecycle | FIXED — production caches expose explicit close paths and API/worker shutdown clears cached clients. |
| HTTP 200 streaming preflight | FIXED — durable Run creation is completed before the streaming response starts; post-200 failures emit a safe terminal failure and close the stream. |
| Tool result bounding | FIXED — oversized serializable results remain successful observations with bounded projection and `truncated=true`; only unsafe serialization is a tool-result failure. |

### P3 and prior review residue

The low-risk P3 items are fixed or explicitly bounded: LangGraph primitives are isolated behind
adapters; resume approval identity and original-initiator authorization are revalidated;
`UNKNOWN_OUTCOME` maps to the Run-level reconciliation code; Windows event-loop policy is
explicitly configured at test/process bootstrap; missing-checkpoint resume is fail-closed;
approval decision and action execution failures remain separate in the UI; context-budget upper
bounds and canonical numeric edge cases are covered; the production TraceSink is redacted and
fail-open; and `.dockerignore`, README, architecture documentation, and redundant app setup were
cleaned up.

The following remain `DEFERRED_WITH_REASON`, not silent findings:

- Provider-specific tokenizers and external Langfuse/OpenTelemetry export remain future
  integrations; M6 uses a provider-neutral redacted production sink.
- Automatic replay of uncertain/non-idempotent external `CLAIMED` actions is prohibited until a
  provider contract can prove outcome safety.
- Retrieval/rerank lifecycle events are not fabricated where the current runtime has no reliable
  single boundary; the canonical event envelope is already in place.

## Semantic decisions

### Event protocol

The canonical AgentHub event envelope is:

```text
event_id, type, request_id, run_id, step_id, timestamp, payload
```

`sequence` and `agent_version_id` are documented extensions. Heartbeats are transport comments,
not business events, and are not persisted or included in the event sequence.

### Approval decisions versus Run status

`DENIED` and `EXPIRED` are Approval decision outcomes, not `AgentRun.status` values. The graph
may complete normally after a denied or expired approval. `UNKNOWN_OUTCOME` remains an action
execution result and maps the Run to `NEEDS_ATTENTION` with
`ACTION_RECONCILIATION_REQUIRED`. M6 presents approval-decision failures separately from action
execution failures.

### Cancellation

`RUNNING -> CANCEL_REQUESTED` stops scheduling new graph work at durable boundaries and becomes
`CANCELLED` only when the current operation is safely finished. An already-issued uncertain
external action is never declared cancelled; it becomes `NEEDS_ATTENTION`.

### Reconciliation and identity

The production trigger is the existing Celery beat/worker pair. Approval decisions use the
approver identity, while graph resume re-resolves the original Run initiator's current
workspace permissions. The approver context is never reused as the runtime actor.

## Verification

- `uv sync --locked`: pass.
- Ruff: pass.
- Fresh PostgreSQL migration `0001` through `0013`, `alembic check`, and checkpoint bootstrap:
  pass.
- Integration suite: **88 passed, 4 skipped**; the skips are the Redis-only M3-B cases because
  no Redis test URL was configured for the local run.
- Non-integration suite: **226 passed**.
- M4 benchmark: **20/20**, all reported metrics 1.0.
- M5 benchmark: **20/20**, duplicate side-effect rate `0.0`.
- M3 retrieval dataset validation: pass.
- Frontend `npm run build`: pass.
- `docker compose config`: pass; no containers were started by this verification.
- GitHub Actions [#85](https://github.com/lijihao-0804/AgentHub/actions/runs/35422911503): **Success**
  for runtime closure commit `2eb9b1f`, with backend and frontend jobs passed.

M6-A is accepted. M6-B and later milestones are not started by this closure.
