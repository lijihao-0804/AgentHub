"""Historical RAG references must remain authorized snapshot members."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.evidence_plan import assemble_evidence_plan
from packages.knowledge.models import DocumentChunk
from packages.knowledge.snapshots import KnowledgeSnapshotService
from packages.model_gateway.contracts import ModelMessage, ModelToolCall
from tests.integration.test_context_policy_m2 import _create_knowledge_base
from tests.integration.test_thread_history_m3 import (
    TEST_DATABASE_URL,
    _async_database_url,
    _seed_workspace,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Requires isolated PostgreSQL"),
]


@pytest_asyncio.fixture
async def factory():
    engine, session_factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_evidence_requires_current_permission_and_actual_snapshot_member(factory):
    base = await _seed_workspace(factory)
    context = SimpleNamespace(
        workspace_id=str(base["workspace_id"]), permissions=frozenset({"knowledge_run", "tool_run"})
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
        chunk_id = f"history-evidence-{uuid4().hex}"
        session.add(
            DocumentChunk(
                chunk_id=chunk_id,
                workspace_id=base["workspace_id"],
                knowledge_base_id=kb.id,
                document_id=document.id,
                document_revision_id=revision.id,
                ordinal=0,
                normalized_content_hash="0" * 64,
                text="verified source evidence",
                locator={},
            )
        )
        snapshot = await KnowledgeSnapshotService().create_current_snapshot(session, context, kb.id)
        await session.commit()
    binding = {
        "knowledge_base_id": str(kb.id),
        "snapshot_id": str(snapshot.snapshot_id),
        "snapshot_hash": snapshot.content_hash,
    }
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
        "snippet": "verified source evidence",
    }
    plan = assemble_evidence_plan([record], [ref], budget=4000, estimator=lambda value: len(value))
    assert plan is not None and plan.registry
    turn = SimpleNamespace(
        messages=(
            ModelMessage("assistant", tool_calls=(ModelToolCall("search_knowledge", {}, "c"),)),
            ModelMessage(
                "tool", json.dumps(plan.payload), name="search_knowledge", tool_call_id="c"
            ),
        ),
        evidence_registry=plan.registry,
        source_knowledge_snapshots=(binding,),
        source_wiki_publications=(),
    )
    graph = _AgentRunGraph.__new__(_AgentRunGraph)
    graph.context = context
    graph.run = SimpleNamespace(effective_knowledge_snapshots=[binding])
    graph.service = SimpleNamespace(session_factory=factory)
    conversation = SimpleNamespace(turns=(turn,))
    assert (
        await graph._history_evidence_registry(conversation, workspace_id=base["workspace_id"])
        == plan.registry
    )

    graph.context.permissions = frozenset({"tool_run"})
    with pytest.raises(AgentHubError) as denied:
        await graph._history_evidence_registry(conversation, workspace_id=base["workspace_id"])
    assert denied.value.code == "FORBIDDEN"
    graph.context.permissions = frozenset({"knowledge_run", "tool_run"})
    graph.run.effective_knowledge_snapshots = []
    with pytest.raises(AgentHubError) as mismatch:
        await graph._history_evidence_registry(conversation, workspace_id=base["workspace_id"])
    assert mismatch.value.code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
    graph.run.effective_knowledge_snapshots = [binding]
    invalid_ref = dict(ref, chunk_id="not-a-snapshot-member")
    bad_record = dict(record, chunk_id="not-a-snapshot-member")
    bad = assemble_evidence_plan(
        [bad_record], [invalid_ref], budget=4000, estimator=lambda value: len(value)
    )
    turn.evidence_registry = bad.registry
    turn.messages = (
        turn.messages[0],
        ModelMessage("tool", json.dumps(bad.payload), name="search_knowledge", tool_call_id="c"),
    )
    with pytest.raises(AgentHubError) as missing:
        await graph._history_evidence_registry(conversation, workspace_id=base["workspace_id"])
    assert missing.value.code == "AGENT_HISTORY_SNAPSHOT_INVALID"
