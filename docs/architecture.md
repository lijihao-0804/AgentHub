# Architecture

AgentHub uses a modular monolith plus worker: one repository, one API process, one worker
process and one web application. PostgreSQL is the business source of truth; Redis is used
for queue/cache concerns; Qdrant is the vector retrieval adapter; Langfuse is optional.
The M6-A production composition uses a provider-neutral redacted structured trace sink. It
does not export business content or credentials and remains fail-open; an external Langfuse or
OpenTelemetry exporter can be added behind the same contract later. `langfuse_enabled` does not
claim that an external Langfuse connection is active.

The dependency direction is:

```text
Transport/API -> Application -> Domain/Contract -> Infrastructure Adapter
```

This page is the boundary summary, not the full request-flow guide. For process topology,
the Run/approval/Thread/evaluation flows, and event persistence boundaries, see
[`docs/report/02-架构与数据流.md`](report/02-架构与数据流.md). For selected externally
observable route behavior, see [`docs/api-contracts.md`](api-contracts.md); the detailed
route and table inventory is in [`docs/report/09-数据模型与API.md`](report/09-数据模型与API.md).

M6-A provides read-only Run query/detail/timeline projections over the existing Agent Runtime,
Tool and Approval records. LangGraph and Qdrant remain behind adapter boundaries; raw checkpoint
payloads and provider SDK types do not cross the application contract.
