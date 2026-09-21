from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID, uuid4

import httpx
import pytest

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_retrieval_components, get_workspace_context
from packages.core.config.settings import Settings
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.adapters.fakes import (
    DeterministicFakeDenseEmbedder,
    DeterministicFakeReranker,
    DeterministicFakeSparseEncoder,
)
from packages.knowledge.composition import RetrievalComponents
from packages.knowledge.contracts import (
    KnowledgeProviderError,
    SparseEncoding,
    VectorRecord,
    VectorScope,
    VectorSearchHit,
)
from packages.knowledge.models import Document, DocumentChunk, DocumentRevision


class _FakeSession:
    def __init__(self) -> None:
        self.workspace_id = uuid4()
        self.knowledge_base_id = uuid4()
        self.document_id = uuid4()
        self.revision_id = uuid4()
        self.snapshot_id = uuid4()
        self.chunk = DocumentChunk(
            chunk_id="chunk-playground",
            workspace_id=self.workspace_id,
            knowledge_base_id=self.knowledge_base_id,
            document_id=self.document_id,
            document_revision_id=self.revision_id,
            ordinal=0,
            normalized_content_hash="a" * 64,
            text="bounded evidence " + ("x" * 1_200),
            locator={"type": "text_range", "char_start": 10, "char_end": 20},
        )
        self.document = Document(
            id=self.document_id,
            workspace_id=self.workspace_id,
            knowledge_base_id=self.knowledge_base_id,
            name="handbook.txt",
        )
        self.revision = DocumentRevision(
            id=self.revision_id,
            workspace_id=self.workspace_id,
            knowledge_base_id=self.knowledge_base_id,
            document_id=self.document_id,
            revision_number=3,
            original_filename="handbook.txt",
            blob_key="documents/handbook.txt",
            media_type="text/plain",
            file_size=1_220,
            ingestion_status="READY",
        )
        self.snapshot_exists = True

    async def scalar(self, _statement):
        return object() if self.snapshot_exists else None

    async def scalars(self, _statement):
        return [self.revision_id]

    async def execute(self, statement):
        selected_count = len(statement._raw_columns)
        if selected_count == 3:
            return [(self.chunk, self.document, self.revision)]
        return [(self.chunk, self.revision)]


class _FakeVectorIndex:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.failure: KnowledgeProviderError | None = None
        self.dense_limit: int | None = None
        self.sparse_limit: int | None = None

    def ensure_collection(self) -> None:
        return None

    def upsert(self, _records: tuple[VectorRecord, ...]) -> None:
        return None

    def _hit(self) -> tuple[VectorSearchHit, ...]:
        return (
            VectorSearchHit(
                point_id="qdrant-internal-point-must-not-escape",
                score=0.91,
                payload={"chunk_id": self.session.chunk.chunk_id},
            ),
        )

    def dense_search(
        self,
        _query: tuple[float, ...],
        *,
        scope: VectorScope,
        limit: int,
    ) -> tuple[VectorSearchHit, ...]:
        del scope
        self.dense_limit = limit
        if self.failure is not None:
            raise self.failure
        return self._hit()

    def sparse_search(
        self,
        _query: SparseEncoding,
        *,
        scope: VectorScope,
        limit: int,
    ) -> tuple[VectorSearchHit, ...]:
        del scope
        self.sparse_limit = limit
        if self.failure is not None:
            raise self.failure
        return self._hit()


def _context(session: _FakeSession, *, permissions: frozenset[str]) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="m3d-request",
                trace_id="m3d-trace",
                user_id=str(uuid4()),
            ),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(session.workspace_id),
        workspace_role="DEVELOPER",
        permissions=permissions,
    )


def _components(session: _FakeSession) -> tuple[RetrievalComponents, _FakeVectorIndex]:
    index = _FakeVectorIndex(session)
    return (
        RetrievalComponents(
            dense=DeterministicFakeDenseEmbedder(dimension=8),
            sparse=DeterministicFakeSparseEncoder(),
            reranker=DeterministicFakeReranker(),
            index=index,
        ),
        index,
    )


def _app(
    session: _FakeSession,
    components: RetrievalComponents,
    context: WorkspaceExecutionContext,
) -> object:
    app = create_app(Settings(testing=True, knowledge_dense_vector_size=8))

    async def override_db() -> AsyncIterator[_FakeSession]:
        yield session

    async def override_context() -> WorkspaceExecutionContext:
        return context

    async def override_components() -> RetrievalComponents:
        return components

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_workspace_context] = override_context
    app.dependency_overrides[get_retrieval_components] = override_components
    return app


async def _post(
    app: object,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    payload: dict[str, object],
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            f"/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/retrieval/playground",
            json=payload,
        )


@pytest.mark.asyncio
async def test_playground_returns_enriched_stages_and_bounded_evidence() -> None:
    session = _FakeSession()
    components, index = _components(session)
    app = _app(session, components, _context(session, permissions=frozenset({"knowledge_run"})))

    response = await _post(
        app,
        session.workspace_id,
        session.knowledge_base_id,
        {"query": "handbook", "knowledge_snapshot_id": str(session.snapshot_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["snapshot_id"] == str(session.snapshot_id)
    assert set(body["stages"]) == {"dense", "sparse", "fused", "rerank"}
    assert body["stages"]["dense"]["results"][0]["document_revision_id"] == str(
        session.revision_id
    )
    assert body["stages"]["dense"]["results"][0]["locator"]["type"] == "text_range"
    assert len(body["evidence"][0]["snippet"]) == 1_000
    assert "qdrant-internal-point" not in response.text
    assert index.dense_limit == 30
    assert index.sparse_limit == 30


@pytest.mark.asyncio
async def test_playground_evidence_carries_document_lifecycle() -> None:
    # The playground is the only surface a human uses to calibrate the rerank
    # floor and to check whether a retired document out-ranks its successor,
    # so the lifecycle the ranking depends on has to reach the response. It
    # used to stop at the retrieval layer's metadata dict.
    session = _FakeSession()
    successor_id = uuid4()
    session.document.effective_date = date(2024, 3, 1)
    session.document.superseded_by_document_id = successor_id
    components, _ = _components(session)
    app = _app(session, components, _context(session, permissions=frozenset({"knowledge_run"})))

    response = await _post(
        app,
        session.workspace_id,
        session.knowledge_base_id,
        {"query": "handbook", "knowledge_snapshot_id": str(session.snapshot_id)},
    )

    assert response.status_code == 200, response.text
    evidence = response.json()["evidence"][0]
    assert evidence["document_name"] == "handbook.txt"
    assert evidence["effective_date"] == "2024-03-01"
    assert evidence["superseded"] is True
    assert evidence["superseded_by_document_id"] == str(successor_id)


@pytest.mark.asyncio
async def test_playground_evidence_of_a_live_document_is_not_marked_superseded() -> None:
    session = _FakeSession()
    components, _ = _components(session)
    app = _app(session, components, _context(session, permissions=frozenset({"knowledge_run"})))

    response = await _post(
        app,
        session.workspace_id,
        session.knowledge_base_id,
        {"query": "handbook", "knowledge_snapshot_id": str(session.snapshot_id)},
    )

    assert response.status_code == 200, response.text
    evidence = response.json()["evidence"][0]
    assert evidence["superseded"] is False
    assert evidence["superseded_by_document_id"] is None
    assert evidence["effective_date"] is None


@pytest.mark.asyncio
async def test_playground_accepts_custom_bounded_top_k() -> None:
    session = _FakeSession()
    components, index = _components(session)
    app = _app(session, components, _context(session, permissions=frozenset({"knowledge_run"})))

    response = await _post(
        app,
        session.workspace_id,
        session.knowledge_base_id,
        {
            "query": "handbook",
            "knowledge_snapshot_id": str(session.snapshot_id),
            "dense_top_k": 7,
            "sparse_top_k": 8,
            "candidate_top_k": 9,
            "final_top_k": 4,
        },
    )

    assert response.status_code == 200
    assert index.dense_limit == 7
    assert index.sparse_limit == 8


@pytest.mark.asyncio
async def test_playground_rejects_invalid_limits_and_latest_snapshot() -> None:
    session = _FakeSession()
    components, _index = _components(session)
    app = _app(session, components, _context(session, permissions=frozenset({"knowledge_run"})))

    invalid_limits = await _post(
        app,
        session.workspace_id,
        session.knowledge_base_id,
        {
            "query": "handbook",
            "knowledge_snapshot_id": str(session.snapshot_id),
            "candidate_top_k": 3,
            "final_top_k": 4,
        },
    )
    latest = await _post(
        app,
        session.workspace_id,
        session.knowledge_base_id,
        {"query": "handbook", "knowledge_snapshot_id": "LATEST"},
    )

    assert invalid_limits.status_code == 422
    assert latest.status_code == 422


@pytest.mark.asyncio
async def test_playground_snapshot_scope_and_permission_are_enforced() -> None:
    session = _FakeSession()
    components, _index = _components(session)
    session.snapshot_exists = False
    app = _app(session, components, _context(session, permissions=frozenset({"knowledge_run"})))

    snapshot = await _post(
        app,
        session.workspace_id,
        uuid4(),
        {"query": "handbook", "knowledge_snapshot_id": str(session.snapshot_id)},
    )
    forbidden_app = _app(session, components, _context(session, permissions=frozenset()))
    forbidden = await _post(
        forbidden_app,
        session.workspace_id,
        session.knowledge_base_id,
        {"query": "handbook", "knowledge_snapshot_id": str(session.snapshot_id)},
    )

    assert snapshot.status_code == 404
    assert snapshot.json()["error"]["code"] == "SNAPSHOT_NOT_FOUND"
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_playground_provider_failure_is_safe() -> None:
    session = _FakeSession()
    components, index = _components(session)
    index.failure = KnowledgeProviderError(
        "QDRANT_UNAVAILABLE", "secret provider URL and traceback must not escape"
    )
    app = _app(session, components, _context(session, permissions=frozenset({"knowledge_run"})))

    response = await _post(
        app,
        session.workspace_id,
        session.knowledge_base_id,
        {"query": "handbook", "knowledge_snapshot_id": str(session.snapshot_id)},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "KNOWLEDGE_INDEX_UNAVAILABLE"
    assert "secret provider URL" not in response.text
