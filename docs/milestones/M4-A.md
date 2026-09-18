# M4-A — Agent Draft / AgentVersion / Publish

Status: PASS — accepted in GitHub Actions run #52.

M4-A establishes the immutable publication boundary only. It does not execute an Agent, call a
model provider, execute a Tool, start LangGraph, stream output, or implement Approval.

## Scope

- `agents` stores the workspace-scoped mutable Draft.
- `agent_versions` stores immutable resolved runtime snapshots.
- `agent_knowledge_bindings` stores Draft knowledge selectors.
- `tools`, `tool_revisions` and `agent_tools` persist the minimum binding state needed to freeze
  ToolRevision identity; no Tool Runtime is implemented.
- `AgentPublishService` resolves the existing M2 ModelProfile chain, validates required
  capabilities, resolves concrete Knowledge Snapshots, resolves concrete ToolRevisions, builds
  the provider-neutral `resolved_spec`, and persists its canonical SHA-256 hash.

## Frozen publication contract

`resolved_spec` contains:

- `spec_schema_version`;
- model provider/model/timeout/retry/fallback/capabilities and credential references without
  credential secrets;
- system prompt and prompt version;
- retrieval configuration plus concrete snapshot IDs and content hashes;
- ToolRevision IDs, spec hashes and safe effect/risk/approval projections;
- runtime limits and context budget.

Published versions use a concrete `PINNED` snapshot projection. Draft `LATEST` selectors, when
used, are resolved before the immutable spec is written; `LATEST` is never persisted in the
published retrieval projection.

LATEST publication has an explicit transaction boundary: required LATEST selectors are first
materialized through the existing Knowledge Snapshot service, whose commit is allowed to finish
that materialization transaction. Publish then re-enters a fresh transaction, re-selects the
authoritative Agent with `FOR UPDATE`, re-reads its bindings, and only then resolves model/tool
state and allocates the next version number. If the authoritative draft gained a new LATEST
binding during the boundary, materialization is repeated before the Agent is locked again; no
pre-commit Agent or binding state is used for the immutable spec.

`resolved_spec_hash` is `canonical_json_hash(resolved_spec)`, including the schema version. The
Agent row is locked during version allocation, so concurrent publishes produce serial version
numbers rather than duplicate version numbers.

## API boundary

The minimal API supports Draft create/list/get/update, publish, and version listing under
`/api/v1/workspaces/{workspace_id}/agents`. Request schemas forbid client-supplied workspace,
organization, creator, version, hash and secret fields. The existing `WorkspaceExecutionContext`
and `agent_create` / `agent_edit` permissions remain authoritative.

## Verification

- Unit coverage: canonical hash stability/change, model projection, configuration validation,
  ToolRevision hash, secret exclusion and capability mismatch.
- PostgreSQL integration: publish freeze, version 1/2 concurrency, cross-workspace ModelProfile,
  Snapshot and Tool foreign-key isolation, ToolRevision v2 immutability, and capability failure.
- PostgreSQL integration also covers concurrent LATEST materialization/publish and a deterministic
  draft change after materialization; both verify concrete snapshot IDs/hashes and no LATEST value
  in immutable specs.
- CI uses real PostgreSQL and runs `tests/integration/test_m4a_publish.py`; no provider adapter or
  public LLM is called.
- Final acceptance: GitHub Actions run #52 passed backend, frontend, and the M4-A integration step.

M4-B, Tool Runtime, LangGraph, Agent Run, streaming and Approval remain outside this milestone.
