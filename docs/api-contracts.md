# API contracts

All versioned routes use `/api/v1`. Every request receives an `X-Request-ID` response
header. Errors use this shape:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed.",
    "request_id": "..."
  }
}
```

M0 system endpoints:

- `GET /api/v1/health`: process liveness only.
- `GET /api/v1/ready`: readiness depends on PostgreSQL only.
- `GET /api/v1/dependencies`: PostgreSQL, Redis, Qdrant and Langfuse status.

## Run streaming and reconnection

A run's lifecycle is owned by the run, not by the HTTP connection that started it.

- `POST /api/v1/workspaces/{workspace_id}/agent-versions/{agent_version_id}/runs/stream`
  starts a run and streams it as `text/event-stream`.
- `GET /api/v1/workspaces/{workspace_id}/agent-runs/{run_id}/stream?after_sequence=N`
  follows a run this connection did not start. `after_sequence` is the last
  `sequence` the client saw; everything after it is replayed from the durable
  `agent_run_events` log before the connection rejoins the live stream. The
  default `0` replays the run from its first event.

Every frame carries two AgentHub extensions alongside the payload: `sequence` and
`agent_version_id`. **`sequence` is a cursor, not a count** -- `message.delta`
events are deliberately not persisted (doing so would make the log O(tokens) to
buy a typing animation), so gaps in a replayed stream are normal and expected.

Dropping the connection no longer aborts the run. A run is aborted only after
`AGENTHUB_RUN_STREAM_GRACE_SECONDS` have passed with no subscriber attached;
reconnecting inside that window cancels the abort. A run executing in the worker
is never aborted for being unwatched, because unwatched is its normal state.

A repeated `client_token` on `POST .../threads/{thread_id}/turns/stream` is not a
new turn and is not an empty stream either: the request is answered with the
stream of the turn that token already opened, replayed from sequence 0. If the
token's turn exists but has not been attached to a run yet -- two requests
racing with the same token -- the second gets `THREAD_TURN_IN_PROGRESS` (409)
and should retry rather than receive a stream that will never carry anything.

Settings:

| Setting | Default | Meaning |
|---|---|---|
| `AGENTHUB_RUN_STREAM_GRACE_SECONDS` | `60.0` | How long an unwatched run keeps going before it is aborted. |
| `AGENTHUB_RUN_EXECUTION_IN_WORKER` | `false` | When on, `POST .../runs/stream` enqueues the run for the Celery worker and the request becomes an ordinary follower of it. Off keeps execution in the API process, which is what Playground single-shot debugging relies on. |

## What a run response shows, and to whom

Reading a run requires `workspace_read`, which every VIEWER holds. That buys the
run's identity, status, counters, token usage and cost -- the same safe surface
the M6 observability projection exposes. It does not buy `input_text` or
`final_output`: those are the user's prompt verbatim and the model's answer
verbatim, and a role that may not run an agent may not read what was sent to one.

For a caller holding only `workspace_read`, both fields are returned as `null`.
A caller holding `agent_run` gets them populated. The fields are always present
in the response shape -- their presence is frozen contract, their content is
gated -- so clients must treat `null` as "withheld or absent", not as "empty
string". No permission was added for this; `agent_run` already existed.

## Per-run cost ceiling

`runtime_config.max_cost_micro_usd` (optional, integer micro-USD, `1` ..
`100_000_000`) caps what one run may spend. It is frozen into the published
`AgentVersion` like every other runtime limit; omitting it leaves the run
uncapped, which is what every agent published before the field existed has.

The ceiling is checked *before* each model call, not after -- a ceiling that only
notices it has been passed is a report, not a limit. On breach the run reaches the
existing `NEEDS_ATTENTION` terminal state (not `FAILED`: nothing malfunctioned, a
human has to decide whether to raise the budget or abandon the work) with one of:

- `AGENT_COST_LIMIT_EXCEEDED` -- measured spend reached the ceiling.
- `AGENT_COST_UNMEASURABLE` -- spend so far is unpriced or not in USD, so the
  ceiling cannot be enforced. The run stops rather than pretending to be capped.

## LLM-as-judge (evaluation)

`POST /api/v1/workspaces/{workspace_id}/evaluation/experiments` accepts an optional
`judge_model_profile_id`. When present, the profile is resolved through the same
path agent publication uses and its non-secret projection is frozen into the
experiment's `evaluator_manifest`, reduced to an `evaluator_version` of the form
`jv1-<16 hex>`. Changing the judge model therefore changes the evaluator version,
and the existing `evaluator_version_mismatch` / `NOT_COMPARABLE` guard refuses to
compare experiments scored by different judges. No new permission is involved:
choosing a judge is part of `evaluation_manage`.
