from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import (
    AgentKnowledgeBinding,
    AgentTool,
    AgentVersion,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.publish import AgentPublishService
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.models import (
    Document,
    DocumentRevision,
    KnowledgeBase,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.knowledge.snapshots import KnowledgeSnapshotService
from packages.model_gateway.models import ModelProfile, ProviderCredential

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M4-A PostgreSQL integration tests.",
    ),
]


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
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(Path("alembic.ini"))), "head")
        yield
    finally:
        if previous is None:
            os.environ.pop("AGENTHUB_DATABASE_URL", None)
        else:
            os.environ["AGENTHUB_DATABASE_URL"] = previous
        get_settings.cache_clear()


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


def context_for(
    user_id: UUID, workspace_id: UUID, organization_id: UUID
) -> WorkspaceExecutionContext:
    principal = PrincipalContext(
        request_id="m4a-integration",
        trace_id="m4a-integration",
        user_id=str(user_id),
    )
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=principal,
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset(
            {
                "agent_create",
                "agent_edit",
                "agent_run",
                "knowledge_run",
                "tool_create",
                "tool_edit",
                "tool_run",
                "workspace_read",
            }
        ),
    )


async def create_base(
    session: AsyncSession,
    label: str,
    *,
    capabilities: dict[str, object] | None = None,
) -> dict[str, object]:
    user = User(
        email=f"m4a-{label}-{uuid4()}@example.test",
        normalized_email=f"m4a-{label}-{uuid4()}@example.test",
        password_hash="not-used-in-m4a",
    )
    session.add(user)
    await session.flush()
    organization = Organization(name=f"M4-A {label}", created_by=user.id)
    session.add(organization)
    await session.flush()
    session.add(
        OrganizationMembership(organization_id=organization.id, user_id=user.id, role="OWNER")
    )
    workspace = Workspace(organization_id=organization.id, name=f"M4-A {label} workspace")
    session.add(workspace)
    await session.flush()
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider="deepseek",
        name=f"m4a-{label}",
        secret=f"secret-{label}",
    )
    session.add(credential)
    await session.flush()
    profile = ModelProfile(
        workspace_id=workspace.id,
        provider_credential_id=credential.id,
        model="deepseek-chat",
        max_tokens=2_000,
        timeout_seconds=30,
        capabilities=capabilities or {"streaming": True, "max_context_tokens": 64_000},
    )
    session.add(profile)
    knowledge_base = KnowledgeBase(workspace_id=workspace.id, name=f"M4-A {label} KB")
    session.add(knowledge_base)
    await session.flush()
    document = Document(
        workspace_id=workspace.id,
        knowledge_base_id=knowledge_base.id,
        name=f"{label}.md",
    )
    session.add(document)
    await session.flush()
    revision = DocumentRevision(
        workspace_id=workspace.id,
        knowledge_base_id=knowledge_base.id,
        document_id=document.id,
        revision_number=1,
        original_filename=f"{label}.md",
        blob_key=f"m4a/{label}/{uuid4()}.md",
        media_type="text/markdown",
        file_size=32,
        lifecycle_status=RevisionLifecycleStatus.ACTIVE,
        ingestion_status=RevisionIngestionStatus.READY,
    )
    session.add(revision)
    await session.commit()
    context = context_for(user.id, workspace.id, organization.id)
    snapshot = await KnowledgeSnapshotService().create_current_snapshot(
        session, context, knowledge_base.id
    )
    return {
        "user": user,
        "user_id": user.id,
        "organization": organization,
        "organization_id": organization.id,
        "workspace": workspace,
        "workspace_id": workspace.id,
        "context": context,
        "credential": credential,
        "profile": profile,
        "profile_id": profile.id,
        "knowledge_base": knowledge_base,
        "knowledge_base_id": knowledge_base.id,
        "snapshot": snapshot,
        "snapshot_id": snapshot.snapshot_id,
    }


async def add_tool(
    session: AsyncSession,
    base: dict[str, object],
    *,
    name: str = "search_knowledge",
    version: int = 1,
    suffix: str = "v1",
) -> tuple[Tool, ToolRevision]:
    workspace_id = base["workspace_id"]
    user_id = base["user_id"]
    spec = {
        "kind": "builtin",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "identity": suffix,
    }
    tool = Tool(workspace_id=workspace_id, name=f"{name}-{suffix}", enabled=True)
    session.add(tool)
    await session.flush()
    revision = ToolRevision(
        workspace_id=workspace_id,
        tool_id=tool.id,
        revision_number=version,
        spec=spec,
        spec_hash=canonical_json_hash(spec),
        created_by=user_id,
    )
    session.add(revision)
    await session.commit()
    return tool, revision


@pytest.mark.asyncio
async def test_m4a_publish_freezes_model_snapshot_tool_and_hash(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        base = await create_base(
            session,
            f"freeze-{uuid4()}",
            capabilities={"tool_calling": True, "streaming": True},
        )
        tool, tool_revision = await add_tool(session, base)
        service = AgentPublishService()
        agent = await service.create_draft(
            session,
            base["context"],
            name="Support Agent",
            system_prompt="Use the approved knowledge snapshot.",
            model_profile_id=base["profile_id"],
            model_retry_policy={"max_attempts": 2},
        )
        session.add(
            AgentKnowledgeBinding(
                workspace_id=base["workspace_id"],
                agent_id=agent.id,
                knowledge_base_id=base["knowledge_base_id"],
                binding_mode="PINNED",
                snapshot_id=base["snapshot_id"],
            )
        )
        session.add(
            AgentTool(
                workspace_id=base["workspace_id"],
                agent_id=agent.id,
                tool_id=tool.id,
                tool_revision_id=tool_revision.id,
            )
        )
        await session.commit()
        published = await service.publish(session, base["context"], agent.id)
        version = await session.get(AgentVersion, published.id)

        assert version is not None
        assert published.version_number == 1
        assert version.resolved_spec_hash == canonical_json_hash(version.resolved_spec)
        assert version.resolved_spec["model"]["provider"] == "deepseek"
        assert version.resolved_spec["model"]["profile_id"] == str(base["profile_id"])
        assert version.resolved_spec["retrieval"]["knowledge_snapshot_ids"] == [
            str(base["snapshot"].snapshot_id)
        ]
        assert version.resolved_spec["retrieval"]["knowledge_snapshots"][0]["snapshot_hash"] == (
            base["snapshot"].content_hash
        )
        assert version.resolved_spec["tools"][0]["tool_revision_id"] == str(tool_revision.id)
        assert "secret-freeze" not in json.dumps(version.resolved_spec)


@pytest.mark.asyncio
async def test_m4a_concurrent_publish_allocates_serial_versions(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        base = await create_base(session, f"concurrent-{uuid4()}")
        agent = await AgentPublishService().create_draft(
            session,
            base["context"],
            name="Concurrent Agent",
            system_prompt="Stable prompt.",
            model_profile_id=base["profile_id"],
        )
        agent_id = agent.id

    async def publish_once() -> int:
        async with db_factory() as session:
            result = await AgentPublishService().publish(session, base["context"], agent_id)
            return result.version_number

    versions = await asyncio.gather(publish_once(), publish_once())
    assert sorted(versions) == [1, 2]

    async with db_factory() as session:
        stored = list(
            (
                await session.scalars(
                    select(AgentVersion).where(AgentVersion.agent_id == agent_id)
                )
            ).all()
        )
        assert sorted(item.version_number for item in stored) == [1, 2]


@pytest.mark.asyncio
async def test_m4a_cross_workspace_references_are_hidden_or_rejected(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        first = await create_base(session, f"cross-a-{uuid4()}")
        second = await create_base(session, f"cross-b-{uuid4()}")
        service = AgentPublishService()

        with pytest.raises(AgentHubError) as raised:
            await service.create_draft(
                session,
                first["context"],
                name="Cross Workspace Agent",
                system_prompt="Must not resolve another workspace.",
                model_profile_id=second["profile_id"],
            )
        assert raised.value.code == "MODEL_PROFILE_DISABLED"
        assert "secret-cross-b" not in str(raised.value)

        agent = await service.create_draft(
            session,
            first["context"],
            name="Scoped Agent",
            system_prompt="Scoped.",
            model_profile_id=first["profile_id"],
        )
        agent_id = agent.id
        session.add(
            AgentKnowledgeBinding(
                workspace_id=first["workspace_id"],
                agent_id=agent_id,
                knowledge_base_id=second["knowledge_base_id"],
                binding_mode="PINNED",
                snapshot_id=second["snapshot_id"],
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        tool, _ = await add_tool(session, second, suffix="cross-b")
        session.add(
            AgentTool(
                workspace_id=first["workspace_id"],
                agent_id=agent_id,
                tool_id=tool.id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_m4a_tool_revision_v2_does_not_mutate_published_v1(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        base = await create_base(
            session,
            f"tool-immutable-{uuid4()}",
            capabilities={"tool_calling": True},
        )
        tool, revision_one = await add_tool(session, base, suffix="v1")
        service = AgentPublishService()
        agent = await service.create_draft(
            session,
            base["context"],
            name="Tool Version Agent",
            system_prompt="Use only the bound revision.",
            model_profile_id=base["profile_id"],
        )
        session.add(
            AgentTool(
                workspace_id=base["workspace_id"],
                agent_id=agent.id,
                tool_id=tool.id,
                tool_revision_id=revision_one.id,
            )
        )
        await session.commit()
        first = await service.publish(session, base["context"], agent.id)
        second_spec = {
            "kind": "builtin",
            "input_schema": {"type": "object"},
            "effect": "READ",
            "risk_level": "LOW",
            "approval_policy": "NEVER",
            "identity": "v2",
        }
        revision_two = ToolRevision(
            workspace_id=base["workspace_id"],
            tool_id=tool.id,
            revision_number=2,
            spec=second_spec,
            spec_hash=canonical_json_hash(second_spec),
            created_by=base["user_id"],
        )
        session.add(revision_two)
        await session.commit()
        second = await service.publish(session, base["context"], agent.id)
        versions = list(
            (
                await session.scalars(
                    select(AgentVersion)
                    .where(AgentVersion.agent_id == agent.id)
                    .order_by(AgentVersion.version_number)
                )
            ).all()
        )

        assert [item.version_number for item in versions] == [1, 2]
        assert first.resolved_spec_hash == versions[0].resolved_spec_hash
        assert second.resolved_spec_hash == versions[1].resolved_spec_hash
        assert all(
            item.resolved_spec["tools"][0]["tool_revision_id"] == str(revision_one.id)
            for item in versions
        )


@pytest.mark.asyncio
async def test_m4a_tool_publish_requires_tool_calling_capability(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_factory() as session:
        base = await create_base(session, f"capability-{uuid4()}", capabilities={})
        tool, revision = await add_tool(session, base, suffix="capability")
        service = AgentPublishService()
        agent = await service.create_draft(
            session,
            base["context"],
            name="Capability Agent",
            system_prompt="Requires tool calling.",
            model_profile_id=base["profile_id"],
        )
        session.add(
            AgentTool(
                workspace_id=base["workspace_id"],
                agent_id=agent.id,
                tool_id=tool.id,
                tool_revision_id=revision.id,
            )
        )
        await session.commit()

        with pytest.raises(AgentHubError) as raised:
            await service.publish(session, base["context"], agent.id)
        assert raised.value.code == "MODEL_CAPABILITY_MISMATCH"
        assert (
            await session.scalar(
                select(AgentVersion.id).where(AgentVersion.agent_id == agent.id)
            )
            is None
        )
