from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import (
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelToolCallDelta,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.tools.runtime import ToolRuntime

TEST_DATABASE_URL = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="Set AGENTHUB_TEST_DATABASE_URL to run M4-D PostgreSQL integration tests.",
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
async def db_factory(migrated_database: None) -> async_sessionmaker[AsyncSession]:
    del migrated_database
    engine, factory = create_database(async_database_url(TEST_DATABASE_URL))
    try:
        yield factory
    finally:
        await engine.dispose()


def _context(user_id: UUID, workspace_id: UUID, organization_id: UUID) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id=f"m4d-{uuid4().hex}",
                trace_id=f"m4d-{uuid4().hex}",
                user_id=str(user_id),
            ),
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent_run", "tool_run"}),
    )


async def _seed(db_factory: async_sessionmaker[AsyncSession], *, tool: bool = False) -> dict:
    async with db_factory() as session:
        email = f"m4d-{uuid4().hex}@example.test"
        user = User(email=email, normalized_email=email, password_hash="not-used")
        session.add(user)
        await session.flush()
        organization = Organization(name=f"M4-D {email}", created_by=user.id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(organization_id=organization.id, user_id=user.id, role="OWNER")
        )
        workspace = Workspace(organization_id=organization.id, name=f"M4-D {email}")
        session.add(workspace)
        await session.flush()

        from packages.model_gateway.models import ModelProfile, ProviderCredential

        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider="fake",
            name=f"credential-{email}",
            secret="test-secret",
        )
        session.add(credential)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_credential_id=credential.id,
            model="frozen-model",
            temperature=0.1,
            max_tokens=256,
            timeout_seconds=5,
            capabilities={"streaming": True, "tool_calling": True},
        )
        session.add(profile)
        await session.flush()
        from packages.agent_runtime.models import Agent, AgentVersion, Tool, ToolRevision

        agent = Agent(
            workspace_id=workspace.id,
            name=f"agent-{email}",
            system_prompt="Use only published tools.",
            model_profile_id=profile.id,
        )
        session.add(agent)
        await session.flush()
        tool_entries: list[dict[str, object]] = []
        if tool:
            tool_record = Tool(workspace_id=workspace.id, name=f"tool-{email}")
            session.add(tool_record)
            await session.flush()
            tool_spec = {
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
            revision = ToolRevision(
                workspace_id=workspace.id,
                tool_id=tool_record.id,
                revision_number=1,
                spec=tool_spec,
                spec_hash=canonical_json_hash(tool_spec),
                created_by=user.id,
            )
            session.add(revision)
            await session.flush()
            tool_entries.append(
                {
                    "tool_revision_id": str(revision.id),
                    "tool_spec_hash": revision.spec_hash,
                    "effect": "READ",
                    "risk_level": "LOW",
                    "approval_policy": "NEVER",
                }
            )
        resolved = {
            "spec_schema_version": 1,
            "model": {
                "profile_id": str(profile.id),
                "credential_ref": str(credential.id),
                "provider": "fake",
                "model": "frozen-model",
                "temperature": 0.1,
                "max_tokens": 256,
                "timeout_seconds": 5,
                "capabilities": {
                    "streaming": True,
                    "tool_calling": True,
                    "max_context_tokens": 8192,
                },
                "retry_policy": {"max_attempts": 1},
                "fallback_chain": [],
                "fallback_profiles": [],
            },
            "prompt": {"system_prompt": "Use only published tools.", "prompt_version": 1},
            "retrieval": {"knowledge_binding_mode": "PINNED", "knowledge_snapshots": []},
            "tools": tool_entries,
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
            "context": _context(user.id, workspace.id, organization.id),
            "workspace_id": workspace.id,
            "version": version,
        }


class ScriptedStreamGateway:
    def __init__(
        self,
        scripts: list[list[object]],
        *,
        sync_responses: list[ModelResponse] | None = None,
    ) -> None:
        self.scripts = scripts
        self.sync_responses = list(sync_responses or [])
        self.requests = []
        self.stream_calls = 0

    async def generate_resolved(self, context, plan, request):
        del context, plan
        self.requests.append(request)
        if self.sync_responses:
            return self.sync_responses.pop(0)
        return ModelResponse(content="sync answer", provider="fake", model="frozen-model")

    def stream_resolved(self, context, plan, request):
        del context, plan
        self.requests.append(request)
        index = self.stream_calls
        self.stream_calls += 1
        return self._run_script(self.scripts[index])

    async def _run_script(self, script: list[object]):
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item


def _message_script(content: str) -> list[object]:
    return [
        ModelStreamEvent(
            event_type=ModelStreamEventType.MESSAGE_DELTA,
            message_delta=content,
        ),
        ModelStreamEvent(
            event_type=ModelStreamEventType.COMPLETED,
            response=ModelResponse(content=content, provider="fake", model="frozen-model"),
        ),
    ]


@pytest.mark.asyncio
async def test_sync_model_admission_is_mandatory_and_records_no_text(db_factory) -> None:
    base = await _seed(db_factory)
    gateway = ScriptedStreamGateway([])
    result = await AgentRunService(
        db_factory, model_gateway_factory=lambda session: gateway
    ).run(base["context"], agent_version_id=base["version"].id, input_text="sync")
    assert result.status == "SUCCEEDED"
    assert len(gateway.requests) == 1
    assert gateway.requests[0].messages[-1].content == "sync"


@pytest.mark.asyncio
async def test_mandatory_context_overflow_fails_before_model_call(db_factory) -> None:
    base = await _seed(db_factory)
    async with db_factory() as session:
        from packages.agent_runtime.models import AgentVersion

        version = await session.get(AgentVersion, base["version"].id)
        assert version is not None
        spec = dict(version.resolved_spec)
        spec["model"] = {
            **spec["model"],
            "max_tokens": 1,
            "capabilities": {"max_context_tokens": 10},
        }
        version.resolved_spec = spec
        version.resolved_spec_hash = canonical_json_hash(spec)
        await session.commit()
    gateway = ScriptedStreamGateway([])
    result = await AgentRunService(
        db_factory, model_gateway_factory=lambda session: gateway
    ).run(base["context"], agent_version_id=base["version"].id, input_text="overflow")
    assert result.status == "FAILED"
    assert result.failure_code == "AGENT_CONTEXT_BUDGET_EXCEEDED"
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_sync_calculator_reuses_the_existing_tool_runtime_loop(db_factory) -> None:
    base = await _seed(db_factory, tool=True)
    gateway = ScriptedStreamGateway(
        [],
        sync_responses=[
            ModelResponse(
                content="",
                provider="fake",
                model="frozen-model",
                tool_calls=(ModelToolCall("calculator", {"expression": "2+3"}),),
            ),
            ModelResponse(content="5", provider="fake", model="frozen-model"),
        ],
    )
    result = await AgentRunService(
        db_factory,
        model_gateway_factory=lambda session: gateway,
        tool_runtime=ToolRuntime(session_factory=db_factory),
    ).run(base["context"], agent_version_id=base["version"].id, input_text="calculate")
    assert result.status == "SUCCEEDED"
    assert result.final_output == "5"
    assert len(gateway.requests) == 2


@pytest.mark.asyncio
async def test_stream_no_tool_emits_protocol_events_and_completed_output(db_factory) -> None:
    base = await _seed(db_factory)
    gateway = ScriptedStreamGateway([_message_script("hello")])
    events = [
        event
        async for event in AgentRunService(
            db_factory, model_gateway_factory=lambda session: gateway
        ).stream(base["context"], agent_version_id=base["version"].id, input_text="question")
    ]
    assert [event.type.value for event in events] == [
        "run.started",
        "context.budget",
        "message.delta",
        "run.completed",
    ]
    assert events[-1].data["output"] == "hello"
    assert events[-1].data["output"] == "hello"


@pytest.mark.asyncio
async def test_stream_calculator_preserves_tool_event_and_budget_order(db_factory) -> None:
    base = await _seed(db_factory, tool=True)
    gateway = ScriptedStreamGateway(
        [
            [
                ModelStreamEvent(
                    event_type=ModelStreamEventType.TOOL_CALL_DELTA,
                    tool_call_delta=ModelToolCallDelta(
                        index=0, name="calculator", arguments_delta='{"expression":"2+3"}'
                    ),
                ),
                ModelStreamEvent(
                    event_type=ModelStreamEventType.COMPLETED,
                    response=ModelResponse(
                        content="",
                        provider="fake",
                        model="frozen-model",
                        tool_calls=(ModelToolCall("calculator", {"expression": "2+3"}),),
                    ),
                ),
            ],
            _message_script("5"),
        ]
    )
    events = [
        event
        async for event in AgentRunService(
            db_factory,
            model_gateway_factory=lambda session: gateway,
            tool_runtime=ToolRuntime(session_factory=db_factory),
        ).stream(base["context"], agent_version_id=base["version"].id, input_text="calculate")
    ]
    types = [event.type.value for event in events]
    assert types[0:2] == ["run.started", "context.budget"]
    assert "tool.started" in types and "tool.completed" in types
    assert types.count("context.budget") == 2
    assert types[-2:] == ["message.delta", "run.completed"]
    assert all("arguments" not in event.data and "data" not in event.data for event in events)


@pytest.mark.asyncio
async def test_post_visible_provider_failure_is_terminal_and_never_falls_back(db_factory) -> None:
    base = await _seed(db_factory)
    gateway = ScriptedStreamGateway(
        [
            [
                ModelStreamEvent(
                    event_type=ModelStreamEventType.MESSAGE_DELTA,
                    message_delta="partial",
                ),
                ModelGatewayError(
                    ModelGatewayErrorCode.MODEL_STREAM_INTERRUPTED,
                ),
            ]
        ]
    )
    events = [
        event
        async for event in AgentRunService(
            db_factory, model_gateway_factory=lambda session: gateway
        ).stream(base["context"], agent_version_id=base["version"].id, input_text="interrupt")
    ]
    assert [event.type.value for event in events][-2:] == ["message.delta", "run.failed"]
    assert events[-1].data["failure_code"] == "MODEL_STREAM_INTERRUPTED"
    assert gateway.stream_calls == 1


@pytest.mark.asyncio
async def test_stream_cancellation_closes_provider_and_persists_terminal_failure(
    db_factory,
) -> None:
    base = await _seed(db_factory)
    entered = asyncio.Event()
    closed = asyncio.Event()

    class BlockingGateway(ScriptedStreamGateway):
        def stream_resolved(self, context, plan, request):
            del context, plan, request
            return self._blocking()

        async def _blocking(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.COMPLETED,
                response=ModelResponse(content="never", provider="fake", model="frozen-model"),
            )

    stream = AgentRunService(
        db_factory, model_gateway_factory=lambda session: BlockingGateway([])
    ).stream(base["context"], agent_version_id=base["version"].id, input_text="cancel")
    first = await anext(stream)
    assert first.type.value == "run.started"
    await anext(stream)
    await entered.wait()
    await stream.aclose()
    assert closed.is_set()
    async with db_factory() as session:
        run = await session.scalar(
            select(AgentRun).where(AgentRun.id == first.run_id)
        )
        assert run is not None
        assert run.status == "FAILED"
        assert run.failure_code == "AGENT_STREAM_CANCELLED"


@pytest.mark.asyncio
async def test_stream_preflight_rejects_cross_workspace_before_events(db_factory) -> None:
    first = await _seed(db_factory)
    second = await _seed(db_factory)
    service = AgentRunService(db_factory)
    with pytest.raises(AgentHubError) as raised:
        await service.preflight_stream(first["context"], agent_version_id=second["version"].id)
    assert raised.value.code == "AGENT_VERSION_NOT_FOUND"
