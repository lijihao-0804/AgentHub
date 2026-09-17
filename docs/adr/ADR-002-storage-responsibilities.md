# ADR-002: Storage responsibilities

Status: Accepted in M0.

PostgreSQL owns business state and migrations. Redis owns queue/cache concerns. Qdrant is
an infrastructure adapter for vector retrieval and never the tenant/business source of
truth. External observability is optional and must not be on the business critical path.
