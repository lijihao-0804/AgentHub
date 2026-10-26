"""PostgreSQL contracts for the hidden RAG and Wiki publication findings."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import (
    Agent,
    AgentTool,
    AgentVersion,
    AgentWikiBinding,
    Tool,
)
from packages.agent_runtime.publish import AgentPublishService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.models import KnowledgeBase, KnowledgeSnapshot
from packages.knowledge.snapshots import _content_hash
from packages.tools.errors import ToolHandlerError
from packages.wiki.models import WikiCompilation
from tests.integration.test_m4c_agent_runtime import _seed

DATABASE_URL = (
    os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
    or "postgresql+asyncpg://agenthub:agenthub@127.0.0.1:5432/"
    "agenthub_hidden_defects_20261026_final"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL required"),
]


@pytest_asyncio.fixture
async def db_factory():
    """Use the externally bootstrapped 0037 database; never migrate or clean it."""

    engine, factory = create_database(DATABASE_URL)
    try:
        yield factory
    finally:
        await engine.dispose()


def _query_wiki_spec() -> dict[str, object]:
    return {
        "kind": "builtin",
        "identity": "query_wiki",
        "description": "Query the bound Wiki.",
        "input_schema": {"type": "object"},
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 30,
    }


async def _snapshot(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    label: str,
    sequence: int,
) -> tuple[KnowledgeBase, KnowledgeSnapshot]:
    knowledge_base = KnowledgeBase(
        workspace_id=workspace_id,
        name=f"hidden-rag-{label}-{uuid4().hex}",
    )
    session.add(knowledge_base)
    await session.flush()
    snapshot = KnowledgeSnapshot(
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base.id,
        content_hash=_content_hash(knowledge_base.id, []),
    )
    assert sequence > 0
    session.add(snapshot)
    await session.flush()
    return knowledge_base, snapshot


@pytest.mark.asyncio
async def test_h12_latest_keyset_paginates_past_ten_revoked_rows(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"h12-{uuid4().hex}")
        knowledge_base, snapshot = await _snapshot(
            session,
            workspace_id=base["workspace_id"],
            label="latest",
            sequence=1,
        )
        session.add(
            AgentWikiBinding(
                workspace_id=base["workspace_id"],
                agent_id=base["version"].agent_id,
                knowledge_base_id=knowledge_base.id,
                binding_mode="LATEST",
            )
        )
        start = datetime.now(UTC) - timedelta(minutes=20)
        compilations = [
            WikiCompilation(
                workspace_id=base["workspace_id"],
                knowledge_base_id=knowledge_base.id,
                source_snapshot_id=snapshot.id,
                source_snapshot_hash=snapshot.content_hash,
                identity_hash=f"{index + 2:064x}",
                compiler_spec={},
                requested_by=str(base["user"].id),
                status="PUBLISHED",
                content_hash=f"{index + 100:064x}",
                telemetry={},
                published_at=(
                    start + timedelta(seconds=index) if index < 11 else start - timedelta(seconds=1)
                ),
                published_by=str(base["user"].id),
            )
            for index in range(12)
        ]
        session.add_all(compilations)
        await session.commit()
        revoked_ids = {item.id for item in compilations[:11]}
        live = compilations[11]

    import packages.agent_runtime.publish as publish_module

    async def sources(_session, _context, row, **_kwargs):
        if row.id in revoked_ids:
            raise AgentHubError("WIKI_SOURCE_REVOKED", "Revoked fixture.")
        return []

    monkeypatch.setattr(
        publish_module,
        "WikiService",
        lambda: SimpleNamespace(sources=sources),
    )
    async with db_factory() as session:
        agent = await session.get(Agent, base["version"].agent_id)
        assert agent is not None
        resolved = await AgentPublishService()._resolve_wiki_binding(
            session,
            base["context"],
            agent,
        )

    assert resolved is not None
    assert resolved["wiki_publication_id"] == str(live.id)
    assert resolved["publication_hash"] == live.content_hash


@pytest.mark.asyncio
async def test_c05_publication_rejects_query_wiki_without_wiki_binding(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        base = await _seed(
            session,
            label=f"c05-{uuid4().hex}",
            tool_spec=_query_wiki_spec(),
        )
        agent = await session.get(Agent, base["version"].agent_id)
        tool = await session.get(Tool, base["revision"].tool_id)
        assert agent is not None and tool is not None
        assert (
            await session.scalar(
                select(AgentWikiBinding).where(
                    AgentWikiBinding.workspace_id == base["workspace_id"],
                    AgentWikiBinding.agent_id == agent.id,
                )
            )
            is None
        )
        session.add(
            AgentTool(
                workspace_id=base["workspace_id"],
                agent_id=agent.id,
                tool_id=tool.id,
                tool_revision_id=base["revision"].id,
            )
        )
        await session.commit()
        agent_id = agent.id

    context = base["context"].model_copy(
        update={"permissions": frozenset({"agent_edit", "agent_run", "tool_run", "knowledge_run"})}
    )
    async with db_factory() as session:
        with pytest.raises(AgentHubError) as error:
            await AgentPublishService().publish(session, context, agent_id)
    assert error.value.code == "WIKI_BINDING_REQUIRED"


@pytest.mark.asyncio
async def test_c08_historical_frozen_models_fail_closed_after_deployment_drift(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        base = await _seed(session, label=f"c08-{uuid4().hex}")
        version = await session.get(AgentVersion, base["version"].id)
        assert version is not None
        retrieval = dict(version.resolved_spec.get("retrieval", {}))
        retrieval.update(
            {
                "embedding_model": "models/frozen-embedding",
                "reranker_model": "models/frozen-reranker",
            }
        )
        version.resolved_spec = {
            **version.resolved_spec,
            "retrieval": retrieval,
        }
        version.resolved_spec_hash = canonical_json_hash(version.resolved_spec)
        await session.commit()

    from packages.knowledge.composition import retrieval_model_identity_mismatches
    from packages.tools.builtins.search_knowledge import search_knowledge
    from packages.tools.contracts import (
        ToolApprovalPolicy,
        ToolDefinition,
        ToolEffect,
        ToolRisk,
    )

    class _CurrentComponents:
        dense = SimpleNamespace(model_name="models/deployed-embedding")
        sparse = SimpleNamespace(model_name="models/deployed-embedding")
        reranker = SimpleNamespace(model_name="models/deployed-reranker")

    retriever = SimpleNamespace(components=_CurrentComponents())
    retrieval = dict(version.resolved_spec["retrieval"])
    assert retrieval_model_identity_mismatches(
        retrieval,
        retriever,
    )

    accesses = 0

    def session_factory():
        nonlocal accesses
        accesses += 1
        raise AssertionError("drift must fail before opening a DB session")

    definition = ToolDefinition(
        identity="search_knowledge",
        revision_id=uuid4(),
        spec_hash="c08",
        description="search",
        input_schema={},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        retrieval_config=retrieval,
    )
    from packages.core.execution_context.models import (
        OrganizationContext,
        PrincipalContext,
        WorkspaceExecutionContext,
    )
    from packages.tools.contracts import ToolExecutionContext

    context = ToolExecutionContext(
        WorkspaceExecutionContext(
            organization=OrganizationContext(
                principal=PrincipalContext(request_id="c08", trace_id="c08"),
                organization_id=str(base["organization_id"]),
                org_role="OWNER",
            ),
            workspace_id=str(base["workspace_id"]),
            workspace_role="DEVELOPER",
        ),
        agent_version_id=version.id,
        tool_call_id="c08-call",
    )
    with pytest.raises(ToolHandlerError) as error:
        await search_knowledge(
            context,
            definition,
            {"query": "retention"},
            session_factory,
            retriever=retriever,
        )
    assert error.value.code == "KNOWLEDGE_MODEL_IDENTITY_DRIFT"
    assert accesses == 0
