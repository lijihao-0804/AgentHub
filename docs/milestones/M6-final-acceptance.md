# M6 Final Acceptance

Status: PASS — M6-A and M6-B accepted on `m6/observability`.

## Lineage

- Frozen M5-A baseline: `1e712dff4a8991eaa60fb39933ed7e00fc69bda6`
- M6-A implementation: `27086f7`
- Post-M5/M6 runtime closure: `2eb9b1f`
- M6-A docs closure: `deeb327`
- M6-B metrics/API/index implementation: `a0fce7a`
- M6-B Dashboard/failure analytics: `83fc465`
- Historical migrations `0001`–`0013`: unchanged
- Final migration head: `0014_m6_metrics_indexes`

## Acceptance evidence

- Summary, timeseries, failure analytics and AgentVersion breakdown are workspace-scoped,
  bounded, currency-safe and based on PostgreSQL aggregation.
- Dashboard includes success/failure denominator visibility, p50/p95 latency, usage/cost,
  current operational counters, failure drill-down and AgentVersion descriptive breakdown.
- Safe projections do not expose prompt, output, RAG text, customer data, credentials, raw tool
  arguments/results or checkpoint payloads.
- M6 failure scenario dataset: 10 cases; manifest validation PASS and runtime-backed PostgreSQL
  generation PASS through `AgentRunService`/`ToolRuntime`/Approval Runtime, with safe
  `AgentRun`/`RunStep` evidence.
- Fresh PostgreSQL upgraded through `0014`; `alembic check` and checkpoint bootstrap PASS.
- Non-integration suite: 227 passed.
- M6-B targeted PostgreSQL integration: 3 passed.
- Existing M6-A integration and M3-C retrieval regression: PASS.
- M3 retrieval dataset validation: PASS.
- M4 benchmark: 20/20 PASS.
- M5 benchmark: 20/20 PASS; duplicate side-effect rate `0.0`.
- Frontend production build: PASS.
- `docker compose config`: PASS; configuration parsed without starting containers.
- GitHub Actions run #90 (`35425673479`) passed the exact M6-B closure HEAD
  `a8b2e057089bef4ce348f670be2e1d82b4836ef4`, including the runtime-backed dataset step and
  backend/frontend jobs.

M6 overall is PASS. M7, M8, MCP, REST Tool, Memory, Multi-Agent and GraphRAG remain NOT
STARTED.
