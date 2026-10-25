"""Exercise the production Wiki API with the current raw RAG implementation."""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from qdrant_client import QdrantClient
from sqlalchemy import select

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge.adapters.fakes import (
    DeterministicFakeDenseEmbedder,
    DeterministicFakeReranker,
    DeterministicFakeSparseEncoder,
)
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.contracts import VectorRecord
from packages.knowledge.models import DocumentChunk
from packages.knowledge.point_ids import deterministic_point_id
from packages.wiki.service import WikiService, claim_job, execute_job
from tests.support.wiki import FakeCompiler, add_source, seed_wiki

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Set AGENTHUB_TEST_DATABASE_URL."),
]


@pytest_asyncio.fixture
async def db_factory():
    url = TEST_DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine, factory = create_database(url)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_api_modes_compose_current_rag_with_frozen_wiki_and_scope(
    db_factory, monkeypatch
) -> None:
    context, kb_id, profile_id, revisions = await seed_wiki(db_factory)
    outsider, _, _, outsider_revisions = await seed_wiki(db_factory)
    service = WikiService()
    async with db_factory() as session:
        compilation = await service.request(session, context, kb_id, profile_id)
    async with db_factory() as session:
        claim = await claim_job(session, compilation.id)
    assert claim is not None
    assert (
        await execute_job(db_factory, context, claim.id, claim.lease_token, FakeCompiler())
        == "ready"
    )
    async with db_factory() as session:
        published = await service.publish(session, context, kb_id, compilation.id)

    # This source is indexed but must never enter the already frozen publication.
    later = await add_source(db_factory, context, kb_id, "Retention period is 99 years.")
    async with db_factory() as session:
        chunks = list(
            await session.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.document_revision_id.in_(
                        [*revisions, *outsider_revisions, later.id]
                    )
                )
            )
        )
    originals = {chunk.chunk_id: chunk for chunk in chunks}
    dense = DeterministicFakeDenseEmbedder(dimension=4)
    sparse = DeterministicFakeSparseEncoder()
    index = QdrantVectorIndex(
        url=":memory:",
        collection_name="wiki-rag-integration",
        dense_vector_size=4,
        client=QdrantClient(":memory:"),
    )
    index.ensure_collection()
    index.upsert(
        [
            VectorRecord(
                point_id=deterministic_point_id(chunk.chunk_id),
                chunk_id=chunk.chunk_id,
                dense=dense.embed_query(chunk.text),
                sparse=sparse.encode_query(chunk.text),
                payload={
                    "chunk_id": chunk.chunk_id,
                    "workspace_id": str(chunk.workspace_id),
                    "knowledge_base_id": str(chunk.knowledge_base_id),
                    "document_revision_id": str(chunk.document_revision_id),
                },
            )
            for chunk in chunks
        ]
    )
    component_requests = []

    async def components(request):
        component_requests.append(request.url.path)
        return SimpleNamespace(
            dense=dense, sparse=sparse, reranker=DeterministicFakeReranker(), index=index
        )

    # Replace only provider initialization; API -> HybridKnowledgeRetriever ->
    # scoped SQL/Qdrant -> rerank -> Wiki evidence validation runs unchanged.
    monkeypatch.setattr("apps.api.routes.wiki.get_retrieval_components", components)
    settings = Settings(testing=True, database_url=TEST_DATABASE_URL)
    app = create_app(settings)
    active_context = context

    async def context_dependency():
        return active_context

    async def database_dependency():
        async with db_factory() as session:
            yield session

    app.dependency_overrides[get_workspace_context] = context_dependency
    app.dependency_overrides[get_db_session] = database_dependency
    path = (
        f"/api/v1/workspaces/{context.workspace_id}/knowledge-bases/"
        f"{kb_id}/wiki/compilations/{published.id}/query"
    )
    body = {
        "source_snapshot_id": str(published.source_snapshot_id),
        "query": "retention period",
        "intent": "synthesis",
        "require_current": False,
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for mode in ("WIKI", "RAG", "HYBRID"):
                response = await client.post(path, json={**body, "mode": mode})
                assert response.status_code == 200, response.text
                result = response.json()
                assert result["route"] == mode
                assert result["binding"]["compilation_hash"] == published.content_hash
                assert result["binding"]["source_snapshot_id"] == str(published.source_snapshot_id)
                assert result["binding"]["source_snapshot_hash"] == published.source_snapshot_hash
                assert result["binding"]["validated"] is True
                raw = [e for e in result["evidence"] if e["source"] == "raw_retrieval"]
                if mode == "WIKI":
                    assert not component_requests and not raw
                    assert result["wiki_pages"]
                else:
                    assert len(raw) == 2
                    assert not result["supplemental_required"]
                if mode == "HYBRID":
                    assert result["wiki_pages"] and result["conflicts"]
                    assert result["answer"] == (
                        "Conflicting evidence prevents a safe extractive answer."
                    )
                for evidence in result["evidence"]:
                    chunk = originals[evidence["chunk_id"]]
                    assert chunk.workspace_id == UUID(context.workspace_id)
                    assert chunk.knowledge_base_id == kb_id
                    assert chunk.document_revision_id in revisions
                    assert evidence["document_revision_id"] == str(chunk.document_revision_id)
                    assert evidence["locator"] == chunk.locator
                    assert evidence["quote"] in chunk.text
            assert len(component_requests) == 2

            # Cross-tenant binding validation fails before raw providers are loaded.
            active_context = outsider
            foreign = await client.post(path, json={**body, "mode": "HYBRID"})
            assert foreign.status_code == 404
            assert len(component_requests) == 2
            active_context = context

            async with db_factory() as session:
                await service.revoke_source(session, context, kb_id, revisions[0])
            revoked = await client.post(path, json={**body, "mode": "HYBRID"})
            assert revoked.status_code == 409
            assert revoked.json()["error"]["code"] == "WIKI_SOURCE_REVOKED"
            assert len(component_requests) == 2
    finally:
        index.close()
