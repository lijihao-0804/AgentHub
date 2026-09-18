# M3-F — Knowledge Snapshot

Status: implementation and acceptance closure in progress. M4 has not started.

## Scope

M3-F materializes a provider-neutral knowledge snapshot for one workspace and knowledge base.
The current set contains only document revisions that are both `READY` and `ACTIVE`. Items are
ordered deterministically by document ID, revision number, and revision ID. A conflicting pair
of current revisions for one document fails safely instead of producing an ambiguous snapshot.
An empty knowledge base is a valid snapshot.

The canonical payload is schema version 1, the knowledge-base ID, and the stable document/revision
items. Its SHA-256 is produced by the existing `canonical_json_hash` helper. Repeating the same
materialization is idempotent through the scoped `(workspace_id, knowledge_base_id, content_hash)`
unique key; a concurrent insert is re-read and never silently discarded.

## Runtime contract and API

`KnowledgeSnapshotService` provides concrete UUID resolution and the reusable `LATEST` selector.
It returns `ResolvedKnowledgeSnapshot` with the effective snapshot ID, content hash, schema
version, item count, and creation time. Concrete resolution is scoped to both workspace and
knowledge base, and historical snapshots remain resolvable after their source revisions are
retired.

The API endpoint is:

`POST /api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/snapshots`

It requires the existing `knowledge_run` permission and returns the snapshot identity and item
count. M3-D and M3-E continue to require concrete snapshot UUIDs; only M3-F introduces the
reusable `LATEST` resolution helper.

## Database integrity

Snapshot and snapshot-item creation occur in one transaction. Migration `0005_m3f_snapshot_integrity`
adds the composite uniqueness and foreign-key constraints needed to keep snapshot items within the
same workspace, knowledge base, document, and revision. Cross-scope and document/revision
mismatches are rejected by PostgreSQL.

## Verification

The M3-F integration suite covers API materialization and reuse, lifecycle filtering, empty
snapshots, historical resolution, duplicate-current conflict, concurrent idempotency, concurrent
ingestion consistency, cross-scope lookup, and composite foreign-key enforcement. CI runs this
suite after the existing M3-E integration step against the same PostgreSQL, Redis, Qdrant, and
Celery services; it does not download BGE weights or call a public LLM.

Acceptance requires locked Ruff, Alembic upgrade/check, non-integration pytest, the dedicated
M3-F PostgreSQL integration step, diff checks, and the frontend install/build. The final CI run,
backend/frontend status, and commit are recorded here after push.

M3 overall is not marked PASS, and M4 remains deferred.
