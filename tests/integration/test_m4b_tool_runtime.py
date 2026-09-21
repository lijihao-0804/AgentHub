from __future__ import annotations

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

from packages.agent_runtime.models import Agent, AgentVersion, Tool, ToolRevision
from packages.control_plane.models import AuditLog, Organization, User, Workspace
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.contracts import (
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceStage,
    RetrievedEvidence,
)
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.tools.audit import SqlAlchemyToolAuditSink
from packages.tools.models import Customer, Ticket
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M4-B PostgreSQL integration tests.",
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


def _context(user_id: UUID, workspace_id: UUID, organization_id: UUID, *, viewer: bool = False):
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id=f"m4b-{uuid4().hex}",
                trace_id=f"m4b-{uuid4().hex}",
                user_id=str(user_id),
            ),
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="VIEWER" if viewer else "DEVELOPER",
        permissions=frozenset() if viewer else frozenset({"tool_run"}),
    )


async def _seed_base(
    session: AsyncSession,
    *,
    label: str,
    tool_spec: dict[str, object] | None = None,
    snapshot: bool = False,
) -> dict[str, object]:
    user = User(
        email=f"m4b-{label}-{uuid4().hex}@example.test",
        normalized_email=f"m4b-{label}-{uuid4().hex}@example.test",
        password_hash="not-used",
    )
    session.add(user)
    await session.flush()
    organization = Organization(name=f"M4-B {label}", created_by=user.id)
    session.add(organization)
    await session.flush()
    workspace = Workspace(organization_id=organization.id, name=f"M4-B {label}")
    session.add(workspace)
    await session.flush()
    credential = ProviderCredential(
        workspace_id=workspace.id,
        provider="fake",
        name=f"m4b-{label}",
        secret="not-used",
    )
    session.add(credential)
    await session.flush()
    profile = ModelProfile(
        workspace_id=workspace.id,
        provider_credential_id=credential.id,
        model="fake-model",
        max_tokens=100,
        timeout_seconds=5,
    )
    session.add(profile)
    await session.flush()
    agent = Agent(
        workspace_id=workspace.id,
        name=f"agent-{label}",
        system_prompt="read safely",
        model_profile_id=profile.id,
    )
    session.add(agent)
    await session.flush()
    if snapshot:
        from packages.knowledge.models import KnowledgeBase, KnowledgeSnapshot

        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name=f"kb-{label}")
        session.add(knowledge_base)
        await session.flush()
        knowledge_snapshot = KnowledgeSnapshot(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            content_hash="b" * 64,
        )
        session.add(knowledge_snapshot)
        await session.flush()
    else:
        knowledge_base = knowledge_snapshot = None
    spec = tool_spec or {
        "kind": "builtin",
        "identity": "calculator",
        "description": "Safe arithmetic",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
    }
    tool = Tool(workspace_id=workspace.id, name=f"tool-{label}")
    session.add(tool)
    await session.flush()
    revision = ToolRevision(
        workspace_id=workspace.id,
        tool_id=tool.id,
        revision_number=1,
        spec=spec,
        spec_hash=canonical_json_hash(spec),
        created_by=user.id,
    )
    session.add(revision)
    await session.flush()
    retrieval: dict[str, object] = {
        "knowledge_binding_mode": "PINNED",
        "knowledge_snapshot_ids": [],
        "knowledge_snapshots": [],
    }
    if knowledge_snapshot is not None and knowledge_base is not None:
        retrieval["knowledge_snapshot_ids"] = [str(knowledge_snapshot.id)]
        retrieval["knowledge_snapshots"] = [
            {"snapshot_id": str(knowledge_snapshot.id), "snapshot_hash": "b" * 64}
        ]
    resolved = {
        "spec_schema_version": 1,
        "model": {"profile_id": str(profile.id)},
        "prompt": {"system": "read safely", "version": 1},
        "retrieval": retrieval,
        "tools": [
            {
                "tool_revision_id": str(revision.id),
                "tool_spec_hash": revision.spec_hash,
                "effect": spec.get("effect"),
                "risk_level": spec.get("risk_level"),
                "approval_policy": spec.get("approval_policy"),
            }
        ],
        "runtime": {},
    }
    version = AgentVersion(
        workspace_id=workspace.id,
        agent_id=agent.id,
        version_number=1,
        spec_schema_version=1,
        resolved_spec=resolved,
        resolved_spec_hash=canonical_json_hash(resolved),
        created_by=user.id,
    )
    session.add(version)
    await session.commit()
    return {
        "user": user,
        "workspace": workspace,
        "workspace_id": workspace.id,
        "organization_id": organization.id,
        "context": _context(user.id, workspace.id, organization.id),
        "viewer_context": _context(user.id, workspace.id, organization.id, viewer=True),
        "agent": agent,
        "version": version,
        "revision": revision,
        "tool": tool,
        "knowledge_base": knowledge_base,
        "snapshot": knowledge_snapshot,
    }


class _RecordingTraceSink:
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
                        {**dict(attributes or {}), "status": status, "failure_code": failure_code},
                    )
                )

        return Span()


class _FakeRetriever:
    def __init__(self, metadata: dict | None = None) -> None:
        self.metadata = metadata or {}

    async def retrieve_with_trace(self, context, query):
        del context
        stage = RetrievalTraceStage(latency_ms=0, results=())
        return RetrievalResult(
            evidence=(
                RetrievedEvidence(
                    document_id="doc-1",
                    document_revision_id="rev-1",
                    chunk_id="chunk-1",
                    source="document:doc-1/revision:rev-1",
                    locator={"type": "text_range", "char_start": 0, "char_end": 5},
                    text=f"result for {query.text}",
                    retrieval_score=1.0,
                    rerank_score=2.0,
                    metadata=dict(self.metadata),
                ),
            ),
            trace=RetrievalTrace(
                snapshot_id=query.knowledge_snapshot_id,
                dense=stage,
                sparse=stage,
                fusion=stage,
                rerank=stage,
                total_latency_ms=0,
            ),
        )


@pytest.mark.asyncio
async def test_published_calculator_executes_and_returns_untrusted_data(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex)
    result = await ToolRuntime(session_factory=db_factory).execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="calculator",
        arguments={"expression": "2 + 3 * 4"},
        tool_call_id="call-1",
    )
    assert result.status == "SUCCESS"
    assert result.data == {"expression": "2 + 3 * 4", "value": 14}
    assert result.data_trust == "UNTRUSTED"


@pytest.mark.asyncio
async def test_unknown_and_unbound_tools_never_dispatch(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex)
    runtime = ToolRuntime(session_factory=db_factory)
    result = await runtime.execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="query_customer",
        arguments={"customer_ref": "C-1", "include_open_tickets": False},
        tool_call_id="call-unknown",
    )
    assert result.error_code == "UNKNOWN_TOOL"


@pytest.mark.asyncio
async def test_extra_and_workspace_arguments_are_rejected(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex)
    runtime = ToolRuntime(session_factory=db_factory)
    for arguments in (
        {"expression": "1", "extra": True},
        {"expression": "1", "workspace_id": str(base["workspace_id"])},
    ):
        result = await runtime.execute(
            context=base["context"],
            agent_version_id=base["version"].id,
            tool_identity="calculator",
            arguments=arguments,
            tool_call_id="call-invalid",
        )
        assert result.error_code == "TOOL_ARGUMENT_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "effect,approval",
    [("READ", "ALWAYS"), ("WRITE", "NEVER")],
)
async def test_approval_semantics_do_not_call_handler(db_factory, effect, approval) -> None:
    spec = {
        "kind": "builtin",
        "identity": "calculator",
        "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}},
        "effect": effect,
        "risk_level": "HIGH",
        "approval_policy": approval,
    }
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex, tool_spec=spec)
    result = await ToolRuntime(session_factory=db_factory).execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="calculator",
        arguments={"expression": "2 + 2"},
        tool_call_id="call-approval",
    )
    assert result.error_code == "TOOL_APPROVAL_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_old_revision_timeout_default_and_invalid_expression_safe(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed_base(
            session,
            label=uuid4().hex,
            tool_spec={
                "kind": "builtin",
                "identity": "calculator",
                "input_schema": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                },
                "effect": "READ",
                "risk_level": "LOW",
                "approval_policy": "NEVER",
            },
        )
    result = await ToolRuntime(session_factory=db_factory).execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="calculator",
        arguments={"expression": "__import__('os').system('whoami')"},
        tool_call_id="call-safe",
    )
    assert result.error_code == "CALCULATOR_INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_revision_hash_mismatch_is_integrity_error(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex)
        base["revision"].spec = {**base["revision"].spec, "description": "tampered"}
        await session.commit()
    result = await ToolRuntime(session_factory=db_factory).execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="calculator",
        arguments={"expression": "1"},
        tool_call_id="call-integrity",
    )
    assert result.error_code == "TOOL_REVISION_INTEGRITY_ERROR"


@pytest.mark.asyncio
async def test_query_customer_is_workspace_scoped(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex)
        spec = {
            "kind": "builtin",
            "identity": "query_customer",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_ref": {"type": "string"},
                    "include_open_tickets": {"type": "boolean"},
                },
                "required": ["customer_ref", "include_open_tickets"],
            },
            "effect": "READ",
            "risk_level": "LOW",
            "approval_policy": "NEVER",
        }
        base = await _seed_base(session, label=uuid4().hex, tool_spec=spec)
        customer = Customer(
            workspace_id=base["workspace_id"],
            customer_ref="C-100",
            name="Ada",
            email="ada@example.test",
        )
        session.add(customer)
        await session.commit()
    result = await ToolRuntime(session_factory=db_factory).execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="query_customer",
        arguments={"customer_ref": "C-100", "include_open_tickets": False},
        tool_call_id="call-customer",
    )
    assert result.status == "SUCCESS"
    assert result.data["name"] == "Ada"


@pytest.mark.asyncio
async def test_query_customer_does_not_disclose_other_workspace(db_factory) -> None:
    spec = {
        "kind": "builtin",
        "identity": "query_customer",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_ref": {"type": "string"},
                "include_open_tickets": {"type": "boolean"},
            },
            "required": ["customer_ref", "include_open_tickets"],
        },
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
    }
    async with db_factory() as session:
        first = await _seed_base(session, label=uuid4().hex, tool_spec=spec)
        second = await _seed_base(session, label=uuid4().hex, tool_spec=spec)
        session.add(
            Customer(
                workspace_id=second["workspace_id"],
                customer_ref="C-OTHER",
                name="Other",
                email="other@example.test",
            )
        )
        await session.commit()
    result = await ToolRuntime(session_factory=db_factory).execute(
        context=first["context"],
        agent_version_id=first["version"].id,
        tool_identity="query_customer",
        arguments={"customer_ref": "C-OTHER", "include_open_tickets": False},
        tool_call_id="call-tenant",
    )
    assert result.error_code == "CUSTOMER_NOT_FOUND"
    assert "Other" not in str(result)


@pytest.mark.asyncio
async def test_cross_workspace_ticket_composite_fk_is_enforced(db_factory) -> None:
    async with db_factory() as session:
        first = await _seed_base(session, label=uuid4().hex)
        second = await _seed_base(session, label=uuid4().hex)
        customer = Customer(
            workspace_id=first["workspace_id"], customer_ref="C-FK", name="FK", email=None
        )
        session.add(customer)
        await session.flush()
        session.add(
            Ticket(
                workspace_id=second["workspace_id"],
                customer_id=customer.id,
                ticket_ref=f"T-{uuid4().hex}",
                subject="must fail",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_viewer_cannot_execute_tool(db_factory) -> None:
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex)
    with pytest.raises(AgentHubError) as error:
        await ToolRuntime(session_factory=db_factory).execute(
            context=base["viewer_context"],
            agent_version_id=base["version"].id,
            tool_identity="calculator",
            arguments={"expression": "1"},
            tool_call_id="call-viewer",
        )
    assert error.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_timeout_and_safe_trace_and_audit(db_factory) -> None:
    spec = {
        "kind": "builtin",
        "identity": "calculator",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 1,
    }
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex, tool_spec=spec)

    async def slow_handler(context, definition, arguments, session):
        del context, definition, arguments, session
        import asyncio

        await asyncio.sleep(1.2)
        return {}

    trace = _RecordingTraceSink()
    runtime = ToolRuntime(
        session_factory=db_factory,
        registry=ToolRegistry(handler_overrides={"calculator": slow_handler}),
        trace_sink=trace,
        audit_sink=SqlAlchemyToolAuditSink(db_factory),
    )
    result = await runtime.execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="calculator",
        arguments={"expression": "1"},
        tool_call_id="call-timeout",
    )
    assert result.error_code == "TOOL_TIMEOUT"
    assert trace.ended[0][1]["status"] == "error"
    async with db_factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.workspace_id == base["workspace_id"])
        )
    assert audit is not None
    assert "argument_keys" in audit.safe_metadata
    assert "expression" in audit.safe_metadata["argument_keys"]
    assert audit.safe_metadata["argument_keys"] == ["expression"]


@pytest.mark.asyncio
async def test_search_knowledge_uses_published_concrete_snapshot_and_retriever(db_factory) -> None:
    spec = {
        "kind": "builtin",
        "identity": "search_knowledge",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query", "limit"],
        },
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
    }
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex, tool_spec=spec, snapshot=True)
    runtime = ToolRuntime(
        session_factory=db_factory,
        registry=ToolRegistry(retriever=_FakeRetriever()),
    )
    result = await runtime.execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="search_knowledge",
        arguments={"query": "alpha", "limit": 1},
        tool_call_id="call-search",
    )
    assert result.status == "SUCCESS"
    assert result.data["results"][0]["chunk_id"] == "chunk-1"
    # A retriever that populates no lifecycle metadata is still valid; the tool
    # reports "unknown / not superseded" rather than failing.
    assert result.data["results"][0]["document_name"] is None
    assert result.data["results"][0]["effective_date"] is None
    assert result.data["results"][0]["superseded"] is False


@pytest.mark.asyncio
async def test_search_knowledge_tells_the_model_a_document_was_superseded(db_factory) -> None:
    """Without this the model sees a UUID and a snippet, and cannot tell a live
    policy from the one that replaced it -- the corpus test's worst failure."""

    spec = {
        "kind": "builtin",
        "identity": "search_knowledge",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query", "limit"],
        },
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
    }
    async with db_factory() as session:
        base = await _seed_base(session, label=uuid4().hex, tool_spec=spec, snapshot=True)
    runtime = ToolRuntime(
        session_factory=db_factory,
        registry=ToolRegistry(
            retriever=_FakeRetriever(
                metadata={
                    "document_name": "password-policy-v1.md",
                    "effective_date": "2024-03-01",
                    "superseded": True,
                    "superseded_by_document_id": "doc-2",
                }
            )
        ),
    )
    result = await runtime.execute(
        context=base["context"],
        agent_version_id=base["version"].id,
        tool_identity="search_knowledge",
        arguments={"query": "alpha", "limit": 1},
        tool_call_id="call-search-superseded",
    )
    assert result.status == "SUCCESS"
    row = result.data["results"][0]
    assert row["document_name"] == "password-policy-v1.md"
    assert row["effective_date"] == "2024-03-01"
    assert row["superseded"] is True
    # The successor's raw id would tell the model nothing it can act on, so it
    # is deliberately not part of the tool result.
    assert "superseded_by_document_id" not in row
