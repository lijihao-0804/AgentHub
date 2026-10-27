"""M3 end-to-end capture and replay of a real parallel tool exchange."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import (
    Agent,
    AgentRun,
    AgentVersion,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.publish import _resolved_spec
from packages.agent_runtime.runtime_service import AgentRunService
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import ModelRequest, ModelResponse, ModelToolCall
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.models import AgentThread
from packages.threads.service import ThreadService
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime
from tests.unit.test_agent_publish import _agent
from tests.unit.test_context_policy_m2_runtime import _profile

TEST_DATABASE_URL = (
    os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
    or "postgresql://agenthub:m2_local_test@127.0.0.1:15432/agenthub_context_policy_m2"
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Requires isolated PostgreSQL"),
]


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


@pytest_asyncio.fixture
async def factory() -> async_sessionmaker[AsyncSession]:
    # The caller owns schema setup for this isolated database.
    engine, session_factory = create_database(_async_database_url(TEST_DATABASE_URL))
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _context(user_id: UUID, workspace_id: UUID, organization_id: UUID) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id=f"m3-capture-{uuid4().hex}",
                trace_id=f"m3-capture-{uuid4().hex}",
                user_id=str(user_id),
            ),
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent_run", "tool_run"}),
    )


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, UUID]:
    label = uuid4().hex
    async with factory() as session:
        user = User(
            email=f"m3-capture-{label}@example.test",
            normalized_email=f"m3-capture-{label}@example.test",
            password_hash="not-used",
        )
        session.add(user)
        await session.flush()
        organization = Organization(name=f"M3 Capture {label}", created_by=user.id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(
                organization_id=organization.id,
                user_id=user.id,
                role="OWNER",
            )
        )
        workspace = Workspace(organization_id=organization.id, name=f"M3 Capture {label}")
        session.add(workspace)
        await session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=user.id,
                role="DEVELOPER",
            )
        )
        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider="fake",
            name=f"credential-{label}",
            secret="offline-test-secret",
        )
        session.add(credential)
        await session.flush()
        profile_record = ModelProfile(
            workspace_id=workspace.id,
            provider_credential_id=credential.id,
            model="offline-history-model",
            max_tokens=512,
            timeout_seconds=5,
            capabilities={"tool_calling": True, "streaming": False},
        )
        session.add(profile_record)
        await session.flush()
        agent = Agent(
            workspace_id=workspace.id,
            name=f"agent-{label}",
            system_prompt="Return exact deterministic test answers.",
            model_profile_id=profile_record.id,
        )
        session.add(agent)
        await session.flush()

        tool_spec = {
            "kind": "builtin",
            "identity": "query_customer",
            "description": "Read a deterministic test customer.",
            "input_schema": {
                "type": "object",
                "properties": {"customer_ref": {"type": "string"}},
                "required": ["customer_ref"],
                "additionalProperties": False,
            },
            "effect": "READ",
            "risk_level": "LOW",
            "approval_policy": "NEVER",
        }
        tool = Tool(workspace_id=workspace.id, name=f"customer-{label}")
        session.add(tool)
        await session.flush()
        revision = ToolRevision(
            workspace_id=workspace.id,
            tool_id=tool.id,
            revision_number=1,
            spec=tool_spec,
            spec_hash=canonical_json_hash(tool_spec),
            created_by=user.id,
        )
        session.add(revision)
        await session.flush()

        profile = replace(
            _profile(total=32768, output=512, workspace=workspace.id),
            id=profile_record.id,
            provider_credential_id=credential.id,
        )
        draft = _agent(
            workspace_id=workspace.id,
            model_profile_id=profile.id,
            system_prompt=agent.system_prompt,
            runtime_config={
                "context_policy": {"policy_version": "adaptive_context_v1"},
                "history_policy": "deterministic_history_v1",
            },
        )
        spec = _resolved_spec(
            agent=draft,
            model=((profile,), {profile.id: "fake"}),
            bindings=(),
            tools=(
                {
                    "tool_revision_id": str(revision.id),
                    "tool_spec_hash": revision.spec_hash,
                    "effect": "READ",
                    "risk_level": "LOW",
                    "approval_policy": "NEVER",
                },
            ),
        )
        version = AgentVersion(
            workspace_id=workspace.id,
            agent_id=agent.id,
            version_number=1,
            spec_schema_version=spec["spec_schema_version"],
            resolved_spec=spec,
            resolved_spec_hash=canonical_json_hash(spec),
            created_by=user.id,
        )
        session.add(version)
        await session.commit()
        return {
            "user_id": user.id,
            "organization_id": organization.id,
            "workspace_id": workspace.id,
            "agent_id": agent.id,
            "version_id": version.id,
            "context": _context(user.id, workspace.id, organization.id),
        }


class ScriptedGateway:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def generate_resolved(self, context, plan, request):
        del context, plan
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[index]


def _tool_result(request: ModelRequest, call_id: str) -> dict[str, str]:
    tool = next(message for message in request.messages if message.tool_call_id == call_id)
    return {"content": tool.content or "", "name": tool.name or ""}


@pytest.mark.asyncio
async def test_actual_parallel_tool_exchange_is_captured_and_replayed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed(factory)
    started = asyncio.Event()
    release = asyncio.Event()

    async def parallel_handler(context, definition, arguments, session_factory):
        del context, definition, session_factory
        customer_ref = arguments["customer_ref"]
        started.set()
        await release.wait()
        return {"customer_ref": customer_ref, "status": f"resolved-{customer_ref}"}

    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="",
                provider="fake",
                model="offline-history-model",
                tool_calls=(
                    ModelToolCall(
                        "query_customer",
                        {"customer_ref": "alpha"},
                        "provider-alpha",
                    ),
                    ModelToolCall(
                        "query_customer",
                        {"customer_ref": "beta"},
                        "provider-beta",
                    ),
                ),
            ),
            ModelResponse(
                content="Both customers were resolved.",
                provider="fake",
                model="offline-history-model",
            ),
            ModelResponse(
                content="I have the complete prior exchange.",
                provider="fake",
                model="offline-history-model",
            ),
        ]
    )
    service = AgentRunService(
        factory,
        model_gateway_factory=lambda session: gateway,
        tool_runtime=ToolRuntime(
            session_factory=factory,
            registry=ToolRegistry(handler_overrides={"query_customer": parallel_handler}),
        ),
        thread_context_provider=SqlAlchemyThreadContextProvider(factory),
    )
    threads = ThreadService(factory, run_service=service)
    thread = await threads.create_thread(
        base["context"],
        agent_id=base["agent_id"],
        title="M3 actual capture",
    )

    task = asyncio.create_task(
        threads.submit_turn(
            base["context"],
            thread.id,
            user_input="Resolve alpha and beta together.",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    assert not task.done()
    release.set()
    first = await asyncio.wait_for(task, timeout=10)

    async with factory() as session:
        captured_run = await session.scalar(select(AgentRun).where(AgentRun.id == first.run_id))
        turn = await session.scalar(select(AgentThread).where(AgentThread.id == thread.id))
    assert captured_run is not None
    assert turn is not None
    assert captured_run.status == "SUCCEEDED"
    assert captured_run.final_output == "Both customers were resolved."
    assert captured_run.thread_turn_id == first.turn.id
    assert captured_run.thread_history_transcript is not None

    first_assistant_calls = next(
        message for message in gateway.requests[0].messages if message.role == "user"
    )
    assert first_assistant_calls.content == "Resolve alpha and beta together."
    assert len(gateway.requests) == 2

    second = await threads.submit_turn(
        base["context"],
        thread.id,
        user_input="What did you just do?",
    )
    assert second.run_id != first.run_id
    assert len(gateway.requests) == 3

    next_request = gateway.requests[-1]
    replay = [
        message
        for message in next_request.messages
        if message.role in {"user", "assistant", "tool"}
        and (
            message.content == "Resolve alpha and beta together."
            or message.content == "Both customers were resolved."
            or message.content == "What did you just do?"
            or message.tool_calls
            or message.role == "tool"
        )
    ]
    prior_user = next(
        message
        for message in next_request.messages
        if message.role == "user" and message.content == "Resolve alpha and beta together."
    )
    assert prior_user is not None
    prior_call_message = next(
        message
        for message in next_request.messages
        if message.role == "assistant" and message.tool_calls
    )
    alpha_id, beta_id = (call.provider_tool_call_id for call in prior_call_message.tool_calls)
    assert alpha_id != "provider-alpha" and beta_id != "provider-beta"
    assert [(call.name, dict(call.arguments)) for call in prior_call_message.tool_calls] == [
        ("query_customer", {"customer_ref": "alpha"}),
        ("query_customer", {"customer_ref": "beta"}),
    ]
    prior_results = [
        message
        for message in next_request.messages
        if message.role == "tool" and message.tool_call_id in {alpha_id, beta_id}
    ]
    assert len(prior_results) == 2
    result_by_id = {
        message.tool_call_id: _tool_result(next_request, message.tool_call_id or "")
        for message in prior_results
    }
    assert set(result_by_id) == {alpha_id, beta_id}
    assert '"customer_ref":"alpha"' in result_by_id[alpha_id]["content"]
    assert '"status":"resolved-alpha"' in result_by_id[alpha_id]["content"]
    assert '"customer_ref":"beta"' in result_by_id[beta_id]["content"]
    assert '"status":"resolved-beta"' in result_by_id[beta_id]["content"]
    assert all(result_by_id[call_id]["name"] == "query_customer" for call_id in result_by_id)
    assert replay[-1].content == "What did you just do?"


@pytest.mark.asyncio
async def test_revoked_membership_stops_the_next_model_dispatch(factory):
    base = await _seed(factory)
    started, release = asyncio.Event(), asyncio.Event()

    async def gated_handler(context, definition, arguments, session_factory):
        started.set()
        await release.wait()
        return {"customer_ref": arguments["customer_ref"]}

    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="",
                provider="fake",
                model="offline-history-model",
                tool_calls=(ModelToolCall("query_customer", {"customer_ref": "alpha"}, "call-1"),),
            ),
            ModelResponse(
                content="should not be called", provider="fake", model="offline-history-model"
            ),
        ]
    )
    service = AgentRunService(
        factory,
        model_gateway_factory=lambda session: gateway,
        tool_runtime=ToolRuntime(
            session_factory=factory,
            registry=ToolRegistry(handler_overrides={"query_customer": gated_handler}),
        ),
        thread_context_provider=SqlAlchemyThreadContextProvider(factory),
    )
    threads = ThreadService(factory, run_service=service)
    thread = await threads.create_thread(
        base["context"], agent_id=base["agent_id"], title="revocation"
    )
    task = asyncio.create_task(
        threads.submit_turn(base["context"], thread.id, user_input="look up")
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    async with factory() as session:
        await session.execute(
            update(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == base["organization_id"],
                OrganizationMembership.user_id == base["user_id"],
            )
            .values(role="MEMBER")
        )
        await session.execute(
            update(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == base["workspace_id"],
                WorkspaceMembership.user_id == base["user_id"],
            )
            .values(role="VIEWER")
        )
        await session.commit()
    release.set()
    result = await asyncio.wait_for(task, timeout=10)
    async with factory() as session:
        stored = await session.scalar(select(AgentRun).where(AgentRun.id == result.run_id))
        assert stored.status == "FAILED" and stored.failure_code == "FORBIDDEN"
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_missing_legacy_tool_sidecar_fails_closed_before_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await _seed(factory)
    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="The incomplete history was ignored.",
                provider="fake",
                model="offline-history-model",
            )
        ]
    )
    service = AgentRunService(
        factory,
        model_gateway_factory=lambda session: gateway,
        tool_runtime=ToolRuntime(session_factory=factory),
        thread_context_provider=SqlAlchemyThreadContextProvider(factory),
    )
    threads = ThreadService(factory, run_service=service)
    thread = await threads.create_thread(
        base["context"],
        agent_id=base["agent_id"],
        title="M3 missing sidecar",
    )

    first = await threads.submit_turn(
        base["context"],
        thread.id,
        user_input="Legacy answer.",
    )
    async with factory() as session:
        legacy_run = await session.scalar(select(AgentRun).where(AgentRun.id == first.run_id))
        assert legacy_run is not None
        legacy_run.thread_history_transcript = None
        legacy_run.tool_call_count = 1
        await session.commit()

    calls_before = len(gateway.requests)
    failed = await threads.submit_turn(
        base["context"],
        thread.id,
        user_input="Try to use the incomplete history.",
    )
    async with factory() as session:
        failed_run = await session.scalar(select(AgentRun).where(AgentRun.id == failed.run_id))
    assert failed_run is not None
    assert failed_run.status == "FAILED"
    assert failed_run.failure_code == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
    assert len(gateway.requests) == calls_before
