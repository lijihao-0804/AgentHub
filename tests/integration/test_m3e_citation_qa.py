from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_model_gateway, get_retrieval_components
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.core.auth.security import issue_access_token
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge.adapters.fakes import (
    DeterministicFakeDenseEmbedder,
    DeterministicFakeReranker,
    DeterministicFakeSparseEncoder,
)
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.composition import RetrievalComponents
from packages.knowledge.contracts import VectorRecord
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
from packages.model_gateway.contracts import ModelRequest, ModelResponse
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
TEST_QDRANT_URL = os.environ.get("AGENTHUB_TEST_QDRANT_URL", "http://127.0.0.1:6333").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL or not TEST_QDRANT_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL and AGENTHUB_TEST_QDRANT_URL for M3-E integration.",
    ),
]
db_session_dependency = Depends(get_db_session)


def async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
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
async def db_factory(migrated_database: None) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


class _FakeModelGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, _context, _profile_id: UUID, _request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content=(
                '{"answer":"The document says alpha. [1]","citation_ids":[1],'
                '"insufficient_evidence":false}'
            ),
            provider="integration-fake",
            model="deterministic-fake",
        )


class _FailIfCalledAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _profile, _credential, _request):
        self.calls += 1
        raise AssertionError("cross-workspace profile reached provider adapter")

    async def health(self, _profile, _credential):
        self.calls += 1
        raise AssertionError("cross-workspace profile reached provider adapter")

    async def stream(self, _profile, _credential, _request):
        self.calls += 1
        raise AssertionError("cross-workspace profile reached provider adapter")
        yield


async def _seed(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID, UUID, DocumentChunk]:
    async with factory() as session:
        user = User(
            email=f"{uuid4()}@m3e.test",
            normalized_email=f"{uuid4()}@m3e.test",
            password_hash="not-used",
        )
        session.add(user)
        await session.flush()
        organization = Organization(name=f"m3e-{uuid4()}", created_by=user.id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="OWNER",
            )
        )
        workspace = Workspace(organization_id=organization.id, name=f"workspace-{uuid4()}")
        session.add(workspace)
        await session.flush()
        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name="Citation QA")
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
            blob_key=f"m3e/{uuid4().hex}",
            media_type="text/plain",
            file_size=20,
            ingestion_status=RevisionIngestionStatus.READY,
        )
        session.add(revision)
        await session.flush()
        chunk = DocumentChunk(
            chunk_id=f"m3e-{uuid4().hex}",
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            document_revision_id=revision.id,
            ordinal=0,
            normalized_content_hash="d" * 64,
            text="alpha 中文 knowledge",
            locator={"type": "text_range", "char_start": 0, "char_end": 19},
        )
        session.add(chunk)
        await session.flush()
        snapshot = KnowledgeSnapshot(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            content_hash="e" * 64,
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
        return user.id, workspace.id, knowledge_base.id, snapshot.id, chunk


async def _seed_model_profile_in_other_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
) -> UUID:
    async with factory() as session:
        organization = Organization(name=f"m3e-other-{uuid4()}", created_by=user_id)
        session.add(organization)
        await session.flush()
        workspace = Workspace(organization_id=organization.id, name=f"other-{uuid4()}")
        session.add(workspace)
        await session.flush()
        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider="workspace-b-private-provider",
            name="workspace-b-private-credential",
            secret="workspace-b-secret-must-not-leak",
        )
        session.add(credential)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_credential_id=credential.id,
            model="workspace-b-private-model",
            max_tokens=256,
            timeout_seconds=5,
            capabilities={"structured_output": True},
        )
        session.add(profile)
        await session.commit()
        return profile.id


def _index(settings: Settings, chunk: DocumentChunk) -> QdrantVectorIndex:
    index = QdrantVectorIndex(
        url=settings.qdrant_url,
        collection_name=settings.knowledge_qdrant_collection,
        dense_vector_size=settings.knowledge_dense_vector_size,
        timeout_seconds=settings.knowledge_qdrant_timeout_seconds,
    )
    dense = DeterministicFakeDenseEmbedder(dimension=settings.knowledge_dense_vector_size)
    sparse = DeterministicFakeSparseEncoder()
    index.ensure_collection()
    index.upsert(
        (
            VectorRecord(
                point_id=deterministic_point_id(chunk.chunk_id),
                chunk_id=chunk.chunk_id,
                dense=dense.embed_query(chunk.text),
                sparse=sparse.encode_query(chunk.text),
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


@pytest.mark.asyncio
async def test_citation_qa_uses_real_tenant_postgres_and_qdrant(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, workspace_id, knowledge_base_id, snapshot_id, chunk = await _seed(db_factory)
    settings = Settings(
        testing=True,
        database_url=async_database_url(TEST_DATABASE_URL),
        qdrant_url=TEST_QDRANT_URL,
        knowledge_qdrant_collection=f"agenthub_m3e_{uuid4().hex}",
        knowledge_dense_vector_size=16,
    )
    index = _index(settings, chunk)
    gateway = _FakeModelGateway()
    components = RetrievalComponents(
        dense=DeterministicFakeDenseEmbedder(dimension=16),
        sparse=DeterministicFakeSparseEncoder(),
        reranker=DeterministicFakeReranker(),
        index=index,
    )
    app = create_app(settings)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with db_factory() as session:
            yield session

    async def override_components() -> RetrievalComponents:
        return components

    async def override_gateway() -> _FakeModelGateway:
        return gateway

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_retrieval_components] = override_components
    app.dependency_overrides[get_model_gateway] = override_gateway
    token = issue_access_token(user_id, settings)
    payload = {
        "workspace_id": str(workspace_id),
        "knowledge_base_id": str(knowledge_base_id),
        "knowledge_snapshot_id": str(snapshot_id),
        "model_profile_id": str(uuid4()),
        "query": "What is in the source?",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/knowledge/query",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["citations"][0]["chunk_id"] == chunk.chunk_id
    assert body["retrieval_trace"]["snapshot_id"] == str(snapshot_id)
    assert gateway.calls == 1
    assert "integration-fake" not in response.text


@pytest.mark.asyncio
async def test_citation_qa_rejects_cross_workspace_model_profile_before_adapter(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, workspace_id, knowledge_base_id, snapshot_id, chunk = await _seed(db_factory)
    foreign_profile_id = await _seed_model_profile_in_other_workspace(
        db_factory,
        user_id=user_id,
    )
    settings = Settings(
        testing=True,
        database_url=async_database_url(TEST_DATABASE_URL),
        qdrant_url=TEST_QDRANT_URL,
        knowledge_qdrant_collection=f"agenthub_m3e_cross_profile_{uuid4().hex}",
        knowledge_dense_vector_size=16,
    )
    index = _index(settings, chunk)
    components = RetrievalComponents(
        dense=DeterministicFakeDenseEmbedder(dimension=16),
        sparse=DeterministicFakeSparseEncoder(),
        reranker=DeterministicFakeReranker(),
        index=index,
    )
    adapter = _FailIfCalledAdapter()
    app = create_app(settings)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with db_factory() as session:
            yield session

    async def override_components() -> RetrievalComponents:
        return components

    async def override_gateway(session: AsyncSession = db_session_dependency):
        return SqlAlchemyModelGateway(session, adapter=adapter)

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_retrieval_components] = override_components
    app.dependency_overrides[get_model_gateway] = override_gateway
    token = issue_access_token(user_id, settings)
    payload = {
        "workspace_id": str(workspace_id),
        "knowledge_base_id": str(knowledge_base_id),
        "knowledge_snapshot_id": str(snapshot_id),
        "model_profile_id": str(foreign_profile_id),
        "query": "What is in the source?",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/knowledge/query",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_PROFILE_DISABLED"
    assert adapter.calls == 0
    assert "workspace-b-private" not in response.text
    assert "workspace-b-secret" not in response.text
