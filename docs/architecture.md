# Architecture

AgentHub uses a modular monolith plus worker: one repository, one API process, one worker
process and one web application. PostgreSQL is the business source of truth; Redis is used
for queue/cache concerns; Qdrant is the vector retrieval adapter; Langfuse is optional.
The current M4 TraceSink is intentionally no-op; `langfuse_enabled` is reserved for the M6
observability integration and does not claim an active Langfuse connection.

The dependency direction is:

```text
Transport/API -> Application -> Domain/Contract -> Infrastructure Adapter
```

The current M0 code contains only the boundaries and system endpoints. Agent runtime,
knowledge and tool behavior are intentionally deferred to their milestones.
