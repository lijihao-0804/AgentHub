"""W4 native query_wiki tool over a real database, publication and agent run."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from qdrant_client import QdrantClient
from sqlalchemy import select

from packages.agent_runtime.models import (
    Agent,
    AgentRun,
    AgentTool,
    AgentWikiBinding,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_runtime.tool_revisions import tool_spec_hash
from packages.control_plane.models import AuditLog
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
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.model_gateway.contracts import (
    ModelResponse,
    ModelToolCall,
    ResolvedModelExecutionPlan,
)
from packages.model_gateway.models import ModelProfile
from packages.tools.audit import SqlAlchemyToolAuditSink
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime
from packages.wiki.service import WikiService, claim_job, execute_job
from tests.support.wiki import FakeCompiler, seed_wiki

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Set AGENTHUB_TEST_DATABASE_URL."),
]

_TOOL_SPEC = {
    "kind": "builtin",
    "identity": "query_wiki",
    "description": "Query the bound Wiki publication.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "mode": {"type": "string"},
            "intent": {"type": "string"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "effect": "READ",
    "risk_level": "LOW",
    "approval_policy": "NEVER",
    "timeout_seconds": 30,
}


class WikiToolGateway:
    """First turn proposes query_wiki, second turn answers from the observation."""

    def __init__(self) -> None:
        self.requests = []

    async def generate_resolved(self, context, plan: ResolvedModelExecutionPlan, request):
        del context, plan
        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelResponse(
                content="",
                provider="fake",
                model="wiki-fake-model",
                tool_calls=(
                    ModelToolCall(
                        "query_wiki",
                        {"query": "retention", "mode": "HYBRID", "intent": "fact"},
                    ),
                ),
            )
        return ModelResponse(content="answered from wiki", provider="fake", model="wiki-fake-model")


@pytest_asyncio.fixture
async def db_factory():
    url = TEST_DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine, factory = create_database(url)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _publish_wiki(db_factory, context, kb_id, profile_id):
    service = WikiService()
    async with db_factory() as session:
        compilation = await service.request(session, context, kb_id, profile_id)
    async with db_factory() as session:
        claim = await claim_job(session, compilation.id)
    assert (
        await execute_job(db_factory, context, claim.id, claim.lease_token, FakeCompiler())
        == "ready"
    )
    async with db_factory() as session:
        return await service.publish(session, context, kb_id, compilation.id)


async def _seed_agent_with_wiki(db_factory, context, kb_id, profile_id, publication):
    """Draft agent + query_wiki tool + PINNED wiki binding, published through the service."""

    publish_context = context.model_copy(
        update={
            "permissions": context.permissions
            | {"agent_edit", "agent_create", "tool_edit", "knowledge_run", "workspace_read"}
        }
    )
    async with db_factory() as session:
        user_id = UUID(context.user_id)
        workspace_id = UUID(context.workspace_id)
        profile = await session.get(ModelProfile, profile_id)
        assert profile is not None
        profile.capabilities = {"tool_calling": True, "max_context_tokens": 8_192}
        agent = Agent(
            workspace_id=workspace_id,
            name=f"wiki-agent-{uuid4().hex}",
            system_prompt="Answer from the published Wiki.",
            model_profile_id=profile_id,
        )
        session.add(agent)
        await session.flush()
        tool = Tool(workspace_id=workspace_id, name=f"query-wiki-{uuid4().hex}")
        session.add(tool)
        await session.flush()
        session.add(
            ToolRevision(
                workspace_id=workspace_id,
                tool_id=tool.id,
                revision_number=1,
                spec=_TOOL_SPEC,
                spec_hash=tool_spec_hash(_TOOL_SPEC),
                created_by=user_id,
            )
        )
        await session.flush()
        session.add(
            AgentTool(
                workspace_id=workspace_id,
                agent_id=agent.id,
                tool_id=tool.id,
                tool_revision_id=None,
            )
        )
        session.add(
            AgentWikiBinding(
                workspace_id=workspace_id,
                agent_id=agent.id,
                knowledge_base_id=kb_id,
                binding_mode="PINNED",
                wiki_publication_id=publication.id,
                publication_hash=publication.content_hash,
            )
        )
        await session.commit()
        agent_id = agent.id
    async with db_factory() as session:
        published_version = await AgentPublishService().publish(session, publish_context, agent_id)
        from packages.agent_runtime.models import AgentVersion

        version = await session.get(AgentVersion, published_version.id)
        return agent_id, version


def _retriever(db_factory, chunks):
    dense = DeterministicFakeDenseEmbedder(dimension=4)
    sparse = DeterministicFakeSparseEncoder()
    index = QdrantVectorIndex(
        url=":memory:",
        collection_name=f"wiki-native-{uuid4().hex}",
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
    return SessionScopedKnowledgeRetriever(
        session_factory=db_factory,
        components=SimpleNamespace(
            dense=dense, sparse=sparse, reranker=DeterministicFakeReranker(), index=index
        ),
    )


@pytest.mark.asyncio
async def test_agent_run_calls_query_wiki_end_to_end(db_factory) -> None:
    context, kb_id, profile_id, _revisions = await seed_wiki(db_factory)
    publication = await _publish_wiki(db_factory, context, kb_id, profile_id)
    agent_id, version = await _seed_agent_with_wiki(
        db_factory, context, kb_id, profile_id, publication
    )

    # The published spec froze the wiki binding.
    assert version.resolved_spec["wiki_binding"]["wiki_publication_id"] == str(publication.id)
    assert version.resolved_spec["wiki_binding"]["publication_hash"] == publication.content_hash

    async with db_factory() as session:
        chunks = list(
            await session.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.workspace_id == UUID(context.workspace_id)
                )
            )
        )
    assert chunks
    gateway = WikiToolGateway()
    tool_runtime = ToolRuntime(
        session_factory=db_factory,
        registry=ToolRegistry(retriever=_retriever(db_factory, chunks)),
        audit_sink=SqlAlchemyToolAuditSink(db_factory),
    )
    run_service = AgentRunService(
        db_factory,
        model_gateway_factory=lambda _session: gateway,
        tool_runtime=tool_runtime,
    )
    result = await run_service.run(
        context, agent_version_id=version.id, input_text="What is the retention period?"
    )
    assert result.status == "SUCCEEDED", result.failure_code

    async with db_factory() as session:
        run = await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == UUID(context.workspace_id), AgentRun.id == result.run_id
            )
        )
        assert run is not None
        frozen = run.effective_wiki_publications
        assert len(frozen) == 1
        assert frozen[0]["wiki_publication_id"] == str(publication.id)
        assert frozen[0]["publication_hash"] == publication.content_hash
        audits = list(
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.workspace_id == UUID(context.workspace_id),
                    AuditLog.action == "wiki_query",
                )
            )
        )
        assert len(audits) == 1
        assert audits[0].safe_metadata["tool_identity"] == "query_wiki"
        assert audits[0].safe_metadata["status"] == "SUCCESS"

    # Second model request carries the projected envelope view: short ids with
    # origin markers, publication identity left in the private registry.
    assert len(gateway.requests) == 2
    tool_messages = [m for m in gateway.requests[1].messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].name == "query_wiki"
    view = json.loads(tool_messages[0].content)
    assert view["evidence_format"] == "rag_view_v1"
    assert view["results"], "expected wiki evidence in the model view"
    origins = {item.get("origin") for item in view["results"]}
    assert "WIKI_DERIVED" in origins
    assert all("publication_hash" not in json.dumps(item) for item in view["results"])


@pytest.mark.asyncio
async def test_new_run_refused_after_source_revocation(db_factory) -> None:
    context, kb_id, profile_id, revisions = await seed_wiki(db_factory)
    publication = await _publish_wiki(db_factory, context, kb_id, profile_id)
    _agent_id, version = await _seed_agent_with_wiki(
        db_factory, context, kb_id, profile_id, publication
    )
    wiki = WikiService()
    async with db_factory() as session:
        await wiki.revoke_source(session, context, kb_id, revisions[0])
    run_service = AgentRunService(db_factory, model_gateway_factory=lambda _session: None)
    with pytest.raises(Exception) as error:
        await run_service.run(context, agent_version_id=version.id, input_text="post revocation")
    assert getattr(error.value, "code", "") == "WIKI_PUBLICATION_REVOKED"
