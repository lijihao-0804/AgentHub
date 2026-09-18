# M4-B — READ Tool Runtime

Status: PASS — closure verified in GitHub Actions run #57.

M4-B adds a bounded, provider-neutral READ Tool Runtime. It does not start LangGraph, create
AgentRun/RunStep persistence, stream model output, or implement Approval Runtime.

## Runtime contract

- `ToolRuntime.execute` starts from a published `AgentVersion.resolved_spec`.
- The exact published `tool_revision_id` and `tool_spec_hash` are loaded and verified before
  dispatch. Draft state, latest revisions, and mutable `Tool.name` are never dispatch inputs.
- Executable revisions are static `kind=builtin` definitions with an identity, JSON Schema,
  effect, risk, approval policy and bounded timeout. Older M4-A revisions may omit description
  and timeout; execution supplies safe defaults.
- Only `search_knowledge`, `calculator` and `query_customer` with `READ + NEVER` execute
  automatically. Other approval combinations return `TOOL_APPROVAL_NOT_AVAILABLE` without
  invoking a handler. Approval Runtime is M5 scope.
- Arguments use strict JSON Schema validation. Extra properties and workspace/organization/user
  or revision-scope injection keys are rejected.
- Results are explicitly `UNTRUSTED`. Handler errors, timeouts and unknown identities return
  safe error codes without raw exceptions, secrets or provider details.

## Builtins and tenancy

- `calculator` evaluates a bounded AST allowlist and never calls `eval`, a shell or imports.
- `query_customer` reads the new workspace-scoped `customers` and `tickets` tables. Tickets use
  a composite workspace/customer foreign key, so cross-workspace references fail in PostgreSQL.
- `search_knowledge` reuses the production `KnowledgeRetriever` contract, queries only concrete
  snapshots frozen in the published version, and returns a deterministic bounded projection.
  Tests inject a deterministic fake retriever; CI does not download BGE weights or call a public
  LLM.

## Audit and observability

Tool execution records only the safe `ToolAudit` projection through the existing `AuditLog`
model. Production audit writes use an independent session and audit failure is non-fatal.
The `tool.execute` span contains only tenant/version/tool identity, revision, decision, status,
duration, error code and argument keys. Tool trace failure is non-fatal.

## Verification

- Migration: `0008_m4b_tool_runtime`.
- Unit coverage validates policy and executable revision compatibility.
- PostgreSQL integration covers published calculator execution, unknown/unbound tools, strict
  arguments, injection rejection, approval semantics, old revisions, hash mismatch, customer
  tenant isolation, composite FK enforcement, viewer permission, timeout/audit/trace behavior,
  and snapshot-bound retriever injection.
- CI runs real PostgreSQL, Redis, Qdrant and Celery setup, then the dedicated
  `tests/integration/test_m4b_tool_runtime.py` step. No public tool execution endpoint is added.
- Implementation commit: `b305a58`; final documentation closure commit: `f1a9aff`. GitHub
  Actions run #58 passed backend, frontend, and the dedicated M4-B integration step.

M4-C Agent Run / LangGraph remains outside this milestone.
