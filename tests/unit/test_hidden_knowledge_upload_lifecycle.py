from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Iterable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.blob_store import StoredBlob
from packages.knowledge.models import (
    Document,
    DocumentRevision,
    IngestionJob,
    KnowledgeBase,
)
from packages.knowledge.services import KnowledgeService


def _context(
    workspace_id: UUID,
    *,
    permission: str = "knowledge_create",
) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="hidden-knowledge-unit",
                trace_id="hidden-knowledge-unit",
                user_id=str(uuid4()),
            ),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="OWNER",
        permissions=frozenset({permission}),
    )


class _BlobStore:
    def __init__(self) -> None:
        self.puts: list[str] = []
        self.deleted: list[str] = []
        self.contents: dict[str, bytes] = {}

    async def put(self, blob_key: str, chunks: AsyncIterator[bytes]) -> StoredBlob:
        content = b"".join([chunk async for chunk in chunks])
        self.puts.append(blob_key)
        self.contents[blob_key] = content
        return StoredBlob(blob_key=blob_key, size_bytes=len(content))

    async def delete(self, blob_key: str) -> None:
        self.deleted.append(blob_key)


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue(self, job_id: UUID) -> None:
        self.enqueued.append(job_id)


class _UploadSession:
    def __init__(
        self,
        *,
        knowledge_base: KnowledgeBase,
        commit_error: BaseException | None = None,
        confirmation_error: BaseException | None = None,
        fail_flush_at: int | None = None,
        rollback_error: BaseException | None = None,
    ) -> None:
        self.knowledge_base = knowledge_base
        self.commit_error = commit_error
        self.confirmation_error = confirmation_error
        self.fail_flush_at = fail_flush_at
        self.rollback_error = rollback_error
        self.added: list[Any] = []
        self.flush_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.commit_started = False
        self.durable_rows: list[Any] | None = None
        self.confirmations = deque([None])

    async def scalar(self, statement: object) -> object:
        if self.commit_started:
            if self.confirmation_error is not None:
                raise self.confirmation_error
            return self.confirmations.popleft()
        del statement
        # The upload path first reads its knowledge base and then asks for the
        # latest revision. There is none for a new document.
        if not self.added:
            return self.knowledge_base
        return None

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_calls += 1
        if self.fail_flush_at is not None and self.flush_calls == self.fail_flush_at:
            raise RuntimeError("flush failed before COMMIT")
        for value in self.added:
            if isinstance(value, (Document, DocumentRevision, IngestionJob)) and value.id is None:
                value.id = uuid4()

    async def commit(self) -> None:
        self.commit_calls += 1
        self.commit_started = True
        self.durable_rows = list(self.added)
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollback_calls += 1
        if self.rollback_error is not None:
            raise self.rollback_error


async def _chunks() -> AsyncIterator[bytes]:
    yield b"durable source bytes"


def _rows(session: _UploadSession) -> tuple[Document, DocumentRevision, IngestionJob]:
    assert session.durable_rows is not None
    document = next(item for item in session.durable_rows if isinstance(item, Document))
    revision = next(item for item in session.durable_rows if isinstance(item, DocumentRevision))
    job = next(item for item in session.durable_rows if isinstance(item, IngestionJob))
    return document, revision, job


def _new_upload(
    *,
    commit_error: BaseException | None = None,
    confirmation_error: BaseException | None = None,
    fail_flush_at: int | None = None,
    rollback_error: BaseException | None = None,
) -> tuple[_UploadSession, _BlobStore, _Queue, WorkspaceExecutionContext, UUID]:
    workspace_id = uuid4()
    knowledge_base_id = uuid4()
    session = _UploadSession(
        knowledge_base=KnowledgeBase(
            id=knowledge_base_id,
            workspace_id=workspace_id,
            name="Durable uploads",
        ),
        commit_error=commit_error,
        confirmation_error=confirmation_error,
        fail_flush_at=fail_flush_at,
        rollback_error=rollback_error,
    )
    return session, _BlobStore(), _Queue(), _context(workspace_id), knowledge_base_id


async def _upload(
    session: _UploadSession,
    blobs: _BlobStore,
    queue: _Queue,
    context: WorkspaceExecutionContext,
    knowledge_base_id: UUID,
) -> tuple[Document, DocumentRevision, IngestionJob]:
    return await KnowledgeService().upload_document(
        session,  # type: ignore[arg-type]
        context=context,
        knowledge_base_id=knowledge_base_id,
        filename="durable.txt",
        media_type="text/plain",
        chunks=_chunks(),
        blob_store=blobs,  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_lost_upload_commit_ack_keeps_confirmed_blob_and_enqueues_once() -> None:
    acknowledgement = TimeoutError("COMMIT acknowledgement lost")
    session, blobs, queue, context, knowledge_base_id = _new_upload(commit_error=acknowledgement)
    original_commit = session.commit

    async def commit_with_ack_loss() -> None:
        try:
            await original_commit()
        finally:
            session.confirmations = deque(_rows(session))
        raise acknowledgement

    session.commit = commit_with_ack_loss  # type: ignore[method-assign]

    document, revision, job = await _upload(session, blobs, queue, context, knowledge_base_id)

    durable = _rows(session)
    assert (document, revision, job) == durable
    assert revision.blob_key in blobs.puts
    assert blobs.contents[revision.blob_key] == b"durable source bytes"
    assert blobs.deleted == []
    assert queue.enqueued == [job.id]
    assert session.rollback_calls == 1


@pytest.mark.asyncio
async def test_unverifiable_upload_commit_error_retains_blob_and_propagates() -> None:
    acknowledgement = TimeoutError("COMMIT acknowledgement lost")
    confirmation_failure = ConnectionError("confirmation query unavailable")
    session, blobs, queue, context, knowledge_base_id = _new_upload(
        commit_error=acknowledgement,
        confirmation_error=confirmation_failure,
    )
    original_commit = session.commit

    async def commit_with_ack_loss() -> None:
        try:
            await original_commit()
        finally:
            session.confirmations = deque(_rows(session))
        raise acknowledgement

    session.commit = commit_with_ack_loss  # type: ignore[method-assign]

    with pytest.raises(TimeoutError) as excinfo:
        await _upload(session, blobs, queue, context, knowledge_base_id)

    blob_key = next(iter(blobs.contents))
    assert excinfo.value is acknowledgement
    assert len(blobs.puts) == 1
    assert blobs.contents[blob_key] == b"durable source bytes"
    assert blobs.deleted == []
    assert queue.enqueued == []
    assert session.commit_calls == 1
    assert session.rollback_calls == 1


@pytest.mark.asyncio
async def test_failure_before_commit_rolls_back_and_removes_unreferenced_blob() -> None:
    session, blobs, queue, context, knowledge_base_id = _new_upload(fail_flush_at=2)

    with pytest.raises(RuntimeError, match="before COMMIT"):
        await _upload(session, blobs, queue, context, knowledge_base_id)

    assert session.commit_calls == 0
    assert session.rollback_calls == 1
    assert len(blobs.puts) == 1
    assert blobs.deleted == blobs.puts
    assert queue.enqueued == []


@pytest.mark.asyncio
async def test_lost_ack_is_confirmed_even_when_rollback_also_errors() -> None:
    acknowledgement = TimeoutError("COMMIT acknowledgement lost")
    session, blobs, queue, context, knowledge_base_id = _new_upload(
        commit_error=acknowledgement,
        rollback_error=ConnectionError("rollback connection failed"),
    )
    original_commit = session.commit

    async def commit_with_ack_loss() -> None:
        try:
            await original_commit()
        finally:
            session.confirmations = deque(_rows(session))
        raise acknowledgement

    session.commit = commit_with_ack_loss  # type: ignore[method-assign]

    _, _, job = await _upload(session, blobs, queue, context, knowledge_base_id)

    assert blobs.deleted == []
    assert queue.enqueued == [job.id]
    assert session.rollback_calls == 1


class _GraphResult:
    def __init__(self, rows: Iterable[SimpleNamespace]) -> None:
        self._rows = tuple(rows)

    def __iter__(self):
        return iter(self._rows)


class _LifecycleSession:
    def __init__(
        self,
        *,
        target: Document,
        graph: Iterable[SimpleNamespace],
    ) -> None:
        self.target = target
        self.graph = tuple(graph)
        self.statements: list[object] = []
        self.scalar_calls = 0
        self.executed_statements: list[object] = []
        self.added: list[object] = []
        self.committed = False
        self.rolled_back = False

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        self.scalar_calls += 1
        return self.target.id if self.scalar_calls == 1 else self.target

    async def execute(self, statement: object) -> _GraphResult:
        self.statements.append(statement)
        self.executed_statements.append(statement)
        return _GraphResult(self.graph)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def refresh(self, _value: object) -> None:
        return None


def _document(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    name: str,
    successor: UUID | None = None,
) -> Document:
    return Document(
        id=uuid4(),
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base_id,
        name=name,
        effective_date=None,
        superseded_by_document_id=successor,
    )


def _graph_row(document: Document) -> SimpleNamespace:
    return SimpleNamespace(
        id=document.id,
        superseded_by_document_id=document.superseded_by_document_id,
    )


async def _update_lifecycle(
    session: _LifecycleSession,
    *,
    context: WorkspaceExecutionContext,
    knowledge_base_id: UUID,
    document: Document,
    successor: Document,
) -> Document:
    return await KnowledgeService().update_document_lifecycle(
        session,  # type: ignore[arg-type]
        context=context,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        effective_date=None,
        superseded_by_document_id=successor.id,
        set_effective_date=False,
        set_superseded_by=True,
    )


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_lifecycle_locking_is_kb_first_and_locks_the_complete_graph() -> None:
    workspace_id = uuid4()
    knowledge_base_id = uuid4()
    first = _document(workspace_id, knowledge_base_id, "first")
    second = _document(workspace_id, knowledge_base_id, "second")
    session = _LifecycleSession(
        target=first,
        graph=(_graph_row(first), _graph_row(second)),
    )

    result = await _update_lifecycle(
        session,
        context=_context(workspace_id, permission="knowledge_edit"),
        knowledge_base_id=knowledge_base_id,
        document=first,
        successor=second,
    )

    assert result is first
    assert len(session.statements) == 3
    knowledge_base_sql, document_sql, graph_sql = map(_sql, session.statements)
    assert "FROM knowledge_bases" in knowledge_base_sql
    assert "FROM documents" in document_sql
    assert "FROM documents" in graph_sql
    assert all(
        "FOR UPDATE" in statement for statement in (knowledge_base_sql, document_sql, graph_sql)
    )
    assert session.committed is True


@pytest.mark.asyncio
async def test_lifecycle_rejects_a_sequential_reverse_edge_without_commit() -> None:
    workspace_id = uuid4()
    knowledge_base_id = uuid4()
    first = _document(workspace_id, knowledge_base_id, "first")
    second = _document(workspace_id, knowledge_base_id, "second")
    first.superseded_by_document_id = second.id
    session = _LifecycleSession(
        target=second,
        graph=(_graph_row(first), _graph_row(second)),
    )

    with pytest.raises(AgentHubError) as excinfo:
        await _update_lifecycle(
            session,
            context=_context(workspace_id, permission="knowledge_edit"),
            knowledge_base_id=knowledge_base_id,
            document=second,
            successor=first,
        )

    assert excinfo.value.code == "INVALID_DOCUMENT_SUPERSESSION"
    assert second.superseded_by_document_id is None
    assert session.committed is False
    assert session.added == []


@pytest.mark.asyncio
async def test_lifecycle_rejects_a_cycle_at_the_end_of_a_multi_hop_chain() -> None:
    workspace_id = uuid4()
    knowledge_base_id = uuid4()
    first = _document(workspace_id, knowledge_base_id, "first")
    second = _document(workspace_id, knowledge_base_id, "second")
    third = _document(workspace_id, knowledge_base_id, "third")
    first.superseded_by_document_id = second.id
    second.superseded_by_document_id = third.id
    session = _LifecycleSession(
        target=third,
        graph=tuple(_graph_row(item) for item in (first, second, third)),
    )

    with pytest.raises(AgentHubError) as excinfo:
        await _update_lifecycle(
            session,
            context=_context(workspace_id, permission="knowledge_edit"),
            knowledge_base_id=knowledge_base_id,
            document=third,
            successor=first,
        )

    assert excinfo.value.code == "INVALID_DOCUMENT_SUPERSESSION"
    assert third.superseded_by_document_id is None
    assert session.committed is False
