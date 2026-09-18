from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.control_plane.models import Organization, User, Workspace
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
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
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.contracts import RetrievalQuery, VectorRecord
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeSnapshot,
    KnowledgeSnapshotItem,
    RevisionIngestionStatus,
)
from packages.knowledge.point_ids import deterministic_point_id
from packages.knowledge.retrieval import HybridKnowledgeRetriever

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
TEST_QDRANT_URL = os.environ.get("AGENTHUB_TEST_QDRANT_URL", "http://127.0.0.1:6333").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL or not TEST_QDRANT_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL and AGENTHUB_TEST_QDRANT_URL for M3-C integration.",
    ),
]


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


@pytest.fixture(scope="session")
def migrated_database() -> None:
    previous = os.environ.get("AGENTHUB_DATABASE_URL")
    os.environ["AGENTHUB_DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous


@pytest_asyncio.fixture
async def db_factory(
    migrated_database: None,
) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


class RecordingTraceSink:
    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, object]]] = []
        self.ended: list[tuple[str, dict[str, object]]] = []

    async def start_span(self, name: str, attributes=None):
        self.started.append((name, dict(attributes or {})))
        sink = self

        class Span:
            async def end(self, *, attributes=None, status="ok", failure_code=None):
                sink.ended.append(
                    (
                        name,
                        {
                            **dict(attributes or {}),
                            "status": status,
                            "failure_code": failure_code,
                        },
                    )
                )

        return Span()


async def _seed_retrieval_fixture(
    factory: async_sessionmaker[AsyncSession],
    *,
    workspace_name: str,
) -> tuple[WorkspaceExecutionContext, UUID, UUID, DocumentChunk]:
    async with factory() as session:
        user = User(
            email=f"{uuid4()}@retrieval.test",
            normalized_email=f"{uuid4()}@retrieval.test",
            password_hash="not-used",
        )
        session.add(user)
        await session.flush()
        organization = Organization(name=f"retrieval-{uuid4()}", created_by=user.id)
        session.add(organization)
        await session.flush()
        workspace = Workspace(organization_id=organization.id, name=workspace_name)
        session.add(workspace)
        await session.flush()
        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="Scoped KB")
        session.add(knowledge_base)
        await session.flush()
        document = Document(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            name="source.txt",
        )
        session.add(document)
        await session.flush()
        revision = DocumentRevision(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            revision_number=1,
            original_filename="source.txt",
            blob_key=f"retrieval/{uuid4().hex}",
            media_type="text/plain",
            file_size=5,
            ingestion_status=RevisionIngestionStatus.READY,
        )
        session.add(revision)
        await session.flush()
        chunk = DocumentChunk(
            chunk_id=f"retrieval-{uuid4().hex}",
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            document_revision_id=revision.id,
            ordinal=0,
            normalized_content_hash="b" * 64,
            text="alpha 中文 knowledge",
            locator={"type": "text_range", "char_start": 0, "char_end": 19},
        )
        session.add(chunk)
        await session.flush()
        snapshot = KnowledgeSnapshot(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            content_hash="c" * 64,
        )
        session.add(snapshot)
        await session.flush()
        session.add(
            KnowledgeSnapshotItem(
                workspace_id=workspace.id,
                snapshot_id=snapshot.id,
                knowledge_base_id=knowledge_base.id,
                document_id=document.id,
                document_revision_id=revision.id,
            )
        )
        await session.commit()
        context = WorkspaceExecutionContext(
            organization=OrganizationContext(
                principal=PrincipalContext(
                    request_id="m3c-retrieval",
                    trace_id="m3c-retrieval",
                    user_id=str(user.id),
                ),
                organization_id=str(organization.id),
                org_role="OWNER",
            ),
            workspace_id=str(workspace.id),
            workspace_role="DEVELOPER",
            permissions=frozenset({"knowledge_run"}),
        )
        return context, knowledge_base.id, snapshot.id, chunk


def _index(settings: Settings, chunk: DocumentChunk) -> QdrantVectorIndex:
    index = QdrantVectorIndex(
        url=settings.qdrant_url,
        collection_name=settings.knowledge_qdrant_collection,
        dense_vector_size=settings.knowledge_dense_vector_size,
        timeout_seconds=settings.knowledge_qdrant_timeout_seconds,
    )
    dense = DeterministicFakeDenseEmbedder(dimension=settings.knowledge_dense_vector_size)
    sparse = DeterministicFakeSparseEncoder()
    vector = dense.embed_query(chunk.text)
    sparse_vector = sparse.encode_query(chunk.text)
    index.ensure_collection()
    index.upsert(
        (
            VectorRecord(
                point_id=deterministic_point_id(chunk.chunk_id),
                chunk_id=chunk.chunk_id,
                dense=vector,
                sparse=sparse_vector,
                payload={
                    "workspace_id": str(chunk.workspace_id),
                    "knowledge_base_id": str(chunk.knowledge_base_id),
                    "document_id": str(chunk.document_id),
                    "document_revision_id": str(chunk.document_revision_id),
                    "chunk_id": chunk.chunk_id,
                    "locator": chunk.locator,
                },
            ),
        )
    )
    return index


def _settings(collection: str) -> Settings:
    return Settings(
        database_url=async_database_url(TEST_DATABASE_URL),
        qdrant_url=TEST_QDRANT_URL,
        knowledge_qdrant_collection=collection,
        knowledge_dense_vector_size=16,
    )


@pytest.mark.asyncio
async def test_snapshot_scoped_hybrid_retrieval_and_trace(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    context, knowledge_base_id, snapshot_id, chunk = await _seed_retrieval_fixture(
        db_factory, workspace_name=f"workspace-{uuid4()}"
    )
    _other_context, _other_kb_id, _other_snapshot_id, other_chunk = await _seed_retrieval_fixture(
        db_factory, workspace_name=f"workspace-{uuid4()}"
    )
    settings = _settings(f"agenthub_m3c_retrieval_{uuid4().hex}")
    index = _index(settings, chunk)
    _index(settings, other_chunk)
    trace = RecordingTraceSink()
    async with db_factory() as session:
        retriever = HybridKnowledgeRetriever(
            session=session,
            dense_embedder=DeterministicFakeDenseEmbedder(dimension=16),
            sparse_encoder=DeterministicFakeSparseEncoder(),
            reranker=DeterministicFakeReranker(),
            vector_index=index,
            trace_sink=trace,
        )
        evidence = await retriever.retrieve(
            context,
            RetrievalQuery(
                text="alpha",
                knowledge_base_id=str(knowledge_base_id),
                knowledge_snapshot_id=str(snapshot_id),
            ),
        )
    assert len(evidence) == 1
    assert evidence[0].chunk_id == chunk.chunk_id
    assert evidence[0].text == chunk.text
    assert evidence[0].source.startswith("document:")
    assert {name for name, _ in trace.started} == {"knowledge.retrieve", "knowledge.rerank"}
    assert all("alpha" not in str(attributes) for _, attributes in trace.started)


@pytest.mark.asyncio
async def test_qdrant_unavailable_is_safe_503(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    context, knowledge_base_id, snapshot_id, _chunk = await _seed_retrieval_fixture(
        db_factory, workspace_name=f"workspace-{uuid4()}"
    )
    settings = _settings(f"agenthub_m3c_unavailable_{uuid4().hex}")
    settings.qdrant_url = "http://127.0.0.1:65530"
    async with db_factory() as session:
        retriever = HybridKnowledgeRetriever(
            session=session,
            dense_embedder=DeterministicFakeDenseEmbedder(dimension=16),
            sparse_encoder=DeterministicFakeSparseEncoder(),
            reranker=DeterministicFakeReranker(),
            vector_index=QdrantVectorIndex(
                url=settings.qdrant_url,
                collection_name=settings.knowledge_qdrant_collection,
                dense_vector_size=16,
                timeout_seconds=1,
            ),
        )
        with pytest.raises(AgentHubError) as error:
            await retriever.retrieve(
                context,
                RetrievalQuery(
                    text="alpha",
                    knowledge_base_id=str(knowledge_base_id),
                    knowledge_snapshot_id=str(snapshot_id),
                ),
            )
    assert error.value.code == "KNOWLEDGE_INDEX_UNAVAILABLE"
    assert error.value.status_code == 503
    assert "65530" not in error.value.message
