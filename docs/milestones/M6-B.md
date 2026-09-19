# M6-B — Metrics, Dashboard and Failure Analytics

Status: PASS — implementation verified by GitHub Actions run #88; final docs closure is
included in the exact-head run for this branch.

M6-B productizes the M6-A safe Run projections into workspace-scoped operational metrics. It
does not change M4 Agent Runtime, M5 Approval Runtime, checkpoint identity, or tool execution
semantics.

## API surface

The read-only API is:

```text
GET /api/v1/workspaces/{workspace_id}/observability/summary
GET /api/v1/workspaces/{workspace_id}/observability/timeseries
GET /api/v1/workspaces/{workspace_id}/observability/failures
GET /api/v1/workspaces/{workspace_id}/observability/agent-versions
```

All queries require workspace access and use a bounded UTC window. The default window is seven
days; the maximum is 90 days. Hourly buckets are limited to 48 hours. `agent_version_id` is an
optional filter where applicable.

## Frozen metric definitions

`finished_runs` is exactly `SUCCEEDED + FAILED + CANCELLED + NEEDS_ATTENTION`. It excludes
`RUNNING`, `WAITING_APPROVAL`, and `CANCEL_REQUESTED`.

Every rate returns `numerator`, `denominator`, and `rate`:

- `success_rate = SUCCEEDED / finished_runs`
- `failure_rate = FAILED / finished_runs`
- `cancelled_rate = CANCELLED / finished_runs`
- `needs_attention_rate = NEEDS_ATTENTION / finished_runs`

Zero denominators return `null`, never a fabricated zero or `NaN`. Current-state counters are
separate from window rates. `unknown_outcome_action_count` comes from Approval
`execution_status = UNKNOWN_OUTCOME`, not from guessing from Run failure codes.

Latency uses only Runs with both `started_at` and `completed_at`; active Runs never enter the
distribution. PostgreSQL `percentile_cont` provides p50/p95 and the response includes
`sample_count`.

Usage separates known from unknown totals. Missing usage is not converted to zero.

Costs are grouped by currency. USD, EUR, or any other currencies are never added together.
`cost_per_successful_run` uses only successful Runs with known cost and returns its denominator.
Estimated and exact counts remain separate; absent cost is `null`.

Approval metrics keep decision state and action execution state distinct and report pending,
approved, denied, expired, cancelled, claimed, succeeded, failed and unknown outcomes, plus
approval wait p50/p95 over approvals with `decided_at`.

## Failure and AgentVersion analytics

Failure categories reuse the M6-A presentation taxonomy: `MODEL`, `KNOWLEDGE`, `TOOL`,
`APPROVAL`, `ACTION`, `RUNTIME`, `AUTH/TENANT`, and `UNKNOWN`. Each category retains bounded
top raw safe failure codes and the failure list preserves the safe action identity fields needed
for `UNKNOWN_OUTCOME` diagnosis without exposing arguments or results.

AgentVersion breakdown is descriptive only: run count, success/failed/needs-attention counts,
p95 latency, average known tokens, and currency-grouped cost. It does not rank versions or claim
an experiment result.

Timeseries uses fixed PostgreSQL UTC day/hour buckets and reports runs, succeeded, failed,
needs-attention, known tokens and currency-grouped cost.

## Dashboard and drill-down

`/dashboard` provides summary cards, current operational state, trend rows, failure categories,
currency-safe costs, AgentVersion breakdown and bounded failure Runs. Failure category selection
reloads the failure API; Run and AgentVersion links preserve filters through URL query parameters.
The existing Runs and Run Detail pages remain the safe drill-down surface.

The developer-facing access token remains React-state-only. No token is written to localStorage,
sessionStorage, cookies, URLs, API bodies, or logs.

## Failure scenario dataset

`benchmarks/observability/dataset.json` contains 10 bounded synthetic failure scenarios covering
model, stream/runtime, tool, knowledge, loop guard, approval decision, action failure,
unknown-outcome, missing checkpoint and cancellation paths. It is a scenario manifest, not a
hand-written database fixture: every case names the real `AgentRunService` runtime path that must
produce the durable Run/RunStep evidence under controlled failure injection. The CI validation
step checks the version, count, categories, safe fields and runtime-path constraint.

No prompt, credential, customer record, raw tool payload, or checkpoint body is committed.

## Performance and safety

Metrics are PostgreSQL aggregates with workspace/time indexes from migration
`0014_m6_metrics_indexes`; the API does not load all Runs into Python and does not perform
per-Run detail queries. All dashboard projections remain provider-neutral and content-redacted.
TraceSink availability is not a metrics dependency; PostgreSQL is the source of truth and the
production TraceSink remains fail-open.

M6-B is accepted. M6 overall is accepted after M6-A and M6-B. M7 is not started.
