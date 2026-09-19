# M6-A — Run Query, Run Detail and Timeline

Status: PASS — post-M5 review closure is verified on `m6/observability`, branched from the frozen M5-A
acceptance SHA `1e712dff4a8991eaa60fb39933ed7e00fc69bda6`.

M5-A remains a frozen contract during this milestone. M6-A adds a read-only observability query
surface without changing Approval state transitions, checkpoint identity, reconciliation, or
the `UNKNOWN_OUTCOME` / `NEEDS_ATTENTION` semantics.

## Query surface

The API exposes workspace-scoped safe projections at:

```text
GET /api/v1/workspaces/{workspace_id}/runs
GET /api/v1/workspaces/{workspace_id}/runs/{run_id}
GET /api/v1/workspaces/{workspace_id}/runs/{run_id}/steps
```

The list supports `status`, `agent_version_id`, `limit`, and an opaque created-at/id cursor. The
list query joins AgentVersion and aggregates Approval counts in one bounded query; it does not
perform per-run AgentVersion or Approval lookups.

Run detail includes identity, AgentVersion number and resolved spec hash, effective knowledge
snapshot identities, timestamps, model/tool counts, usage, cost, failure code/category, and an
approval summary. Raw prompts, model output, credentials, RAG/customer content, tool arguments or
results, and checkpoint payloads are not part of the projection.

## Timeline and failure presentation

The `/steps` response is a provider-neutral safe timeline assembled from RunStep and Approval
records. It exposes stable event kinds (`RUN_STARTED`, `MODEL`, `RETRIEVAL`, `TOOL`,
`APPROVAL_WAIT`, `APPROVAL_DECISION`, `ACTION_EXECUTION`, `RESUME`, `FINISH`, and `FAILURE`)
with ordering, status, bounded duration, safe metadata, and the original safe failure code.
LangGraph node names and checkpoint payloads are not returned.

Failure categories are presentation/query mappings only: `MODEL`, `KNOWLEDGE`, `TOOL`, `APPROVAL`,
`ACTION`, `RUNTIME`, `AUTH/TENANT`, and `UNKNOWN`. Stored failure codes remain unchanged.

## Frontend

The developer-facing `/runs` and `/runs/{run_id}` pages keep the access token in React state only.
They display status, AgentVersion, duration, usage, cost, tool calls, approval summary and the
safe timeline. `WAITING_APPROVAL` and `NEEDS_ATTENTION` have distinct operator-facing states;
approval decision failures are not presented as action execution failures.

## Verification target

Acceptance requires targeted M6-A integration coverage for workspace/RBAC isolation, viewer read,
pagination, safe projection, timeline ordering, approval-vs-action distinction, WAITING_APPROVAL,
NEEDS_ATTENTION, usage/cost, and a one-query run list. Existing M4 and M5 contracts and their
benchmarks remain unchanged and must continue to pass.

## Final verification

Implementation commit: `27086f7` (`feat: add M6-A run observability`).

Post-M5/M6 runtime closure commit: `2eb9b1f` (`fix: close M6 runtime review gaps`).

GitHub Actions run [#85](https://github.com/lijihao-0804/AgentHub/actions/runs/35422911503)
passed the backend and frontend jobs for the closure commit. Fresh PostgreSQL verification
upgraded through migration `0013_m6_operational_indexes`; Alembic drift checking and the
LangGraph checkpoint bootstrap passed. The final local verification recorded 88 integration
tests passed with 4 intentionally skipped Redis-only M3-B cases, and 226 non-integration tests
passed. M4 and M5 benchmark suites both passed 20/20. The retrieval dataset validation, frontend
build, and `docker compose config` also passed.

M5-A remains frozen at `1e712dff4a8991eaa60fb39933ed7e00fc69bda6`; no M5 state-machine or
historical migration was rewritten. M6-B and later milestones remain outside this acceptance.
