"""Real ThreadService capture and restoration of compact RAG evidence."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from packages.agent_runtime.models import AgentRun, AgentVersion, ToolRevision
from packages.agent_runtime.runtime_service import AgentRunService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.knowledge.models import DocumentChunk
from packages.knowledge.snapshots import KnowledgeSnapshotService
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.service import ThreadService
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime
from tests.integration.test_context_policy_m2 import _create_knowledge_base
from tests.integration.test_history_transcript_capture_m3 import TEST_DATABASE_URL, _seed
from tests.integration.test_thread_history_m3 import _async_database_url

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="isolated DB required"),
]


@pytest_asyncio.fixture
async def factory():
    engine, session_factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_compact_rag_evidence_is_replayed_with_current_snapshot_and_permission(factory):
    base = await _seed(factory)
    context = base["context"].model_copy(
        update={"permissions": base["context"].permissions | frozenset({"knowledge_run"})}
    )
    async with factory() as session:
        knowledge = await _create_knowledge_base(
            session, workspace_id=base["workspace_id"], label=uuid4().hex
        )
        kb, revision, document = (
            knowledge["knowledge_base"],
            knowledge["revision"],
            knowledge["document"],
        )
        chunk_id = f"history-rag-{uuid4().hex}"
        session.add(
            DocumentChunk(
                chunk_id=chunk_id,
                workspace_id=base["workspace_id"],
                knowledge_base_id=kb.id,
                document_id=document.id,
                document_revision_id=revision.id,
                ordinal=0,
                normalized_content_hash="0" * 64,
                text="source evidence",
                locator={},
            )
        )
        snapshot = await KnowledgeSnapshotService().create_current_snapshot(session, context, kb.id)
        version = await session.scalar(
            select(AgentVersion).where(AgentVersion.id == base["version_id"])
        )
        resolved = deepcopy(version.resolved_spec)
        entry = resolved["tools"][0]
        tool = await session.scalar(
            select(ToolRevision).where(ToolRevision.id == UUID(entry["tool_revision_id"]))
        )
        tool_spec = deepcopy(tool.spec)
        tool_spec["identity"] = "search_knowledge"
        tool_spec["input_schema"] = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        }
        tool.spec = tool_spec
        tool.spec_hash = canonical_json_hash(tool_spec)
        entry["tool_spec_hash"] = tool.spec_hash
        resolved["retrieval"]["evidence_projection"] = "compact_v1"
        resolved["retrieval"]["knowledge_bindings"] = [
            {
                "knowledge_base_id": str(kb.id),
                "binding_mode": "PINNED",
                "snapshot_id": str(snapshot.snapshot_id),
                "snapshot_hash": snapshot.content_hash,
            }
        ]
        version.resolved_spec = resolved
        version.resolved_spec_hash = canonical_json_hash(resolved)
        await session.commit()

    ref = {
        "knowledge_base_id": str(kb.id),
        "snapshot_id": str(snapshot.snapshot_id),
        "document_revision_id": str(revision.id),
        "chunk_id": chunk_id,
    }
    record = {
        "chunk_id": chunk_id,
        "snapshot_id": str(snapshot.snapshot_id),
        "document_id": str(document.id),
        "document_revision_id": str(revision.id),
        "snippet": "source evidence",
    }

    async def search_handler(context, definition, arguments, session_factory):
        return {"results": [record], "evidence_refs": [ref]}

    class Gateway:
        def __init__(self):
            self.requests = []
            self.citations = []

        async def generate_resolved(self, context, plan, request):
            self.requests.append(request)
            if len(self.requests) in {1, 3}:
                return ModelResponse(
                    content="",
                    provider="fake",
                    model="fake",
                    tool_calls=(ModelToolCall("search_knowledge", {"query": "q"}, "call-1"),),
                )
            if len(self.requests) in {2, 4}:
                evidence = [message for message in request.messages if message.role == "tool"][-1]
                citation = json.loads(evidence.content)["results"][0]["id"]
                self.citations.append(citation)
                return ModelResponse(content=f"Answer [{citation}]", provider="fake", model="fake")
            return ModelResponse(content="continued", provider="fake", model="fake")

    gateway = Gateway()
    service = AgentRunService(
        factory,
        model_gateway_factory=lambda session: gateway,
        tool_runtime=ToolRuntime(
            session_factory=factory,
            registry=ToolRegistry(handler_overrides={"search_knowledge": search_handler}),
        ),
        thread_context_provider=SqlAlchemyThreadContextProvider(factory),
    )
    threads = ThreadService(factory, run_service=service)
    thread = await threads.create_thread(
        context, agent_id=base["agent_id"], title="historical evidence"
    )
    first = await threads.submit_turn(context, thread.id, user_input="What is in the source?")
    async with factory() as session:
        first_run = await session.scalar(select(AgentRun).where(AgentRun.id == first.run_id))
        assert first_run.status == "SUCCEEDED"
        assert gateway.citations[0] in first_run.thread_history_evidence_registry
    await threads.submit_turn(context, thread.id, user_input="What did it say?")
    assert len(gateway.requests) == 4
    assert gateway.citations[0] != gateway.citations[1]
    replayed = gateway.requests[-1].messages
    historical_tool = next(message for message in replayed if message.role == "tool")
    assert json.loads(historical_tool.content)["results"][0]["id"] == gateway.citations[0]
    assert any(
        message.role == "assistant" and f"[{gateway.citations[0]}]" in (message.content or "")
        for message in replayed
    )

    restricted = context.model_copy(update={"permissions": context.permissions - {"knowledge_run"}})
    denied = await threads.submit_turn(restricted, thread.id, user_input="repeat")
    async with factory() as session:
        denied_run = await session.scalar(select(AgentRun).where(AgentRun.id == denied.run_id))
        assert denied_run.status == "FAILED" and denied_run.failure_code == "FORBIDDEN"
    assert len(gateway.requests) == 4
