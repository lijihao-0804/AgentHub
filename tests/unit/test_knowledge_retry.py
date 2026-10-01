from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.models import IngestionJobStatus, RevisionIngestionStatus
from packages.knowledge.services import KnowledgeService


class _Session:
    def __init__(self, *scalar_results: object) -> None:
        self._scalar_results = iter(scalar_results)
        self.committed = False

    async def scalar(self, _statement: object) -> object:
        return next(self._scalar_results)

    async def commit(self) -> None:
        self.committed = True


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[object] = []

    async def enqueue(self, job_id: object) -> None:
        self.enqueued.append(job_id)


def _context(workspace_id: object) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="request", trace_id="trace"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        permissions=frozenset({"knowledge_edit"}),
    )


@pytest.mark.asyncio
async def test_retry_resets_max_attempt_failed_job_and_clears_lease() -> None:
    workspace_id = uuid4()
    document_id = uuid4()
    job_id = uuid4()
    document = SimpleNamespace(id=document_id)
    revision = SimpleNamespace(id=uuid4(), ingestion_status=RevisionIngestionStatus.FAILED)
    job = SimpleNamespace(
        id=job_id,
        status=IngestionJobStatus.FAILED,
        attempt_count=100,  # Settings cap the configured maximum at 100.
        lease_token="expired-lease",
        lease_expires_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    session = _Session(document, revision, job)
    queue = _Queue()

    result = await KnowledgeService().retry_document_ingestion(
        session,  # type: ignore[arg-type]
        context=_context(workspace_id),
        knowledge_base_id=uuid4(),
        document_id=document_id,
        queue=queue,  # type: ignore[arg-type]
    )

    assert result == (document, revision, job)
    assert revision.ingestion_status == RevisionIngestionStatus.PENDING
    assert job.status == IngestionJobStatus.PENDING
    assert job.attempt_count == 0
    assert job.lease_token is None
    assert job.lease_expires_at is None
    assert job.next_attempt_at is None
    assert session.committed
    assert queue.enqueued == [job_id]


@pytest.mark.asyncio
async def test_retry_rejects_non_failed_ingestion() -> None:
    workspace_id = uuid4()
    document_id = uuid4()
    document = SimpleNamespace(id=document_id)
    revision = SimpleNamespace(id=uuid4(), ingestion_status=RevisionIngestionStatus.READY)
    job = SimpleNamespace(id=uuid4(), status=IngestionJobStatus.SUCCEEDED, attempt_count=1)
    session = _Session(document, revision, job)
    queue = _Queue()

    with pytest.raises(AgentHubError) as error:
        await KnowledgeService().retry_document_ingestion(
            session,  # type: ignore[arg-type]
            context=_context(workspace_id),
            knowledge_base_id=uuid4(),
            document_id=document_id,
            queue=queue,  # type: ignore[arg-type]
        )

    assert error.value.code == "KNOWLEDGE_INGESTION_NOT_FAILED"
    assert error.value.status_code == 409
    assert not session.committed
    assert queue.enqueued == []
