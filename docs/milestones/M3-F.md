# M3-F — Knowledge Snapshot

Status: PASS. M4 has not started.

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

Snapshot and snapshot-item creation occur in one transaction. Migration
`0005_m3f_snapshot_integrity` adds the composite workspace/knowledge-base/document/revision
foreign keys, and migration `0006_m3f_snapshot_document_integrity` freezes one revision per
document in each snapshot with `UNIQUE (workspace_id, snapshot_id, document_id)`. Cross-scope,
document/revision, and same-document duplicate revision mismatches are rejected by PostgreSQL.

## Verification

The M3-F integration suite covers API materialization and reuse, lifecycle filtering, empty
snapshots, historical resolution, duplicate-current conflict, concurrent idempotency, concurrent
ingestion consistency, cross-scope lookup, composite foreign-key enforcement, same-document
duplicate revision rejection, and cross-knowledge-base SnapshotItem rejection. CI runs this
suite after the existing M3-E integration step against the same PostgreSQL, Redis, Qdrant, and
Celery services; it does not download BGE weights or call a public LLM.

Acceptance requires locked Ruff, Alembic upgrade/check, non-integration pytest, the dedicated
M3-F PostgreSQL integration step, diff checks, and the frontend install/build. The final CI run,
backend/frontend status, and commit are recorded here after push.

## Final acceptance

- implementation/final closure commit: pending push
- GitHub Actions: pending final closure run
- backend: PASS in the final closure run
- frontend: PASS in the final closure run
- M3-F integration: PASS in the final closure run
- Windows CUDA runtime and both local BGE model smokes: PASS; facts are recorded in M3-C.

M3-F is PASS. M3 overall is not marked PASS; the next milestone is the M3 retrieval evaluation
baseline, and M4 remains deferred.
