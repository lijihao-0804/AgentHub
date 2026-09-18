from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.models import DocumentRevision
from packages.knowledge.snapshots import (
    KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION,
    KnowledgeSnapshotService,
    ResolvedKnowledgeSnapshot,
    _canonical_membership,
    _content_hash,
)


def _context(*, permissions: frozenset[str] = frozenset({"knowledge_run"})):
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="m3f", trace_id="m3f"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        workspace_role="DEVELOPER",
        permissions=permissions,
    )


def _revision(document_id, *, status="READY", lifecycle="ACTIVE"):
    return DocumentRevision(
        id=uuid4(),
        workspace_id=uuid4(),
        knowledge_base_id=uuid4(),
        document_id=document_id,
        revision_number=1,
        original_filename="source.txt",
        blob_key=f"m3f/{uuid4().hex}",
        media_type="text/plain",
        file_size=1,
        ingestion_status=status,
        lifecycle_status=lifecycle,
    )


def test_snapshot_hash_is_versioned_and_order_sensitive_only_after_sorting() -> None:
    knowledge_base_id = uuid4()
    first = [(uuid4(), uuid4()), (uuid4(), uuid4())]
    second = list(reversed(first))
    first.sort()
    second.sort()

    assert _canonical_membership(knowledge_base_id, first)["snapshot_schema_version"] == 1
    assert _content_hash(knowledge_base_id, first) == _content_hash(knowledge_base_id, second)
    assert len(_content_hash(knowledge_base_id, first)) == 64
    assert KNOWLEDGE_SNAPSHOT_SCHEMA_VERSION == 1


def test_empty_membership_has_deterministic_snapshot_hash() -> None:
    knowledge_base_id = uuid4()

    assert _content_hash(knowledge_base_id, []) == _content_hash(knowledge_base_id, [])
    assert _canonical_membership(knowledge_base_id, []) == {
        "snapshot_schema_version": 1,
        "knowledge_base_id": str(knowledge_base_id),
        "items": [],
    }


@pytest.mark.asyncio
async def test_current_revision_query_rejects_duplicate_active_ready_revisions() -> None:
    document_id = uuid4()
    revisions = [_revision(document_id), _revision(document_id)]
    session = AsyncMock()
    session.scalars.return_value = revisions
    service = KnowledgeSnapshotService()

    with pytest.raises(AgentHubError) as raised:
        await service._current_revisions(session, uuid4(), uuid4())

    assert raised.value.code == "KNOWLEDGE_REVISION_STATE_CONFLICT"
    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_permission_is_required_for_materialization() -> None:
    service = KnowledgeSnapshotService()

    with pytest.raises(AgentHubError) as raised:
        await service.create_current_snapshot(
            AsyncMock(),
            _context(permissions=frozenset()),
            uuid4(),
        )

    assert raised.value.code == "FORBIDDEN"
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_latest_delegates_to_current_snapshot_and_concrete_selector_is_provider_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = KnowledgeSnapshotService()
    expected = ResolvedKnowledgeSnapshot(
        snapshot_id=uuid4(),
        workspace_id=uuid4(),
        knowledge_base_id=uuid4(),
        content_hash="a" * 64,
        snapshot_schema_version=1,
        item_count=0,
        created_at=datetime.now(UTC),
    )
    create = AsyncMock(return_value=expected)
    monkeypatch.setattr(service, "create_current_snapshot", create)

    resolved = await service.resolve_snapshot(
        AsyncMock(),
        _context(),
        expected.knowledge_base_id,
        "LATEST",
    )

    assert resolved == expected
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_selector_is_safe_client_error() -> None:
    service = KnowledgeSnapshotService()

    with pytest.raises(AgentHubError) as raised:
        await service.resolve_snapshot(AsyncMock(), _context(), uuid4(), "not-a-snapshot")

    assert raised.value.code == "INVALID_SNAPSHOT_SELECTOR"
    assert raised.value.status_code == 400
