"""M3 source snapshot, planner and actual fake-gateway request together."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.history_checkpoint import verify_history_checkpoint_binding
from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.agent_runtime.publish import _resolved_spec
from packages.agent_runtime.runtime_service import AgentRunService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelMessage, ModelResponse, ModelToolCall
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.models import ThreadTurn
from packages.tools.contracts import ToolResult
from tests.integration.test_thread_history_m3 import (
    TEST_DATABASE_URL,
    _add_exchange,
    _async_database_url,
    _create_thread,
    _seed_workspace,
)
from tests.unit.test_agent_publish import _agent
from tests.unit.test_context_policy_m2_runtime import _profile
from tests.unit.test_runtime_rag_evidence_projection import _model_graph

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
async def test_summary_opt_in_preserves_blocked_plan_without_model_dispatch(factory):
    base = await _seed_workspace(factory)
    thread_id = await _create_thread(factory, base)
    for sequence in range(1, 10):
        await _add_exchange(
            factory,
            base,
            thread_id=thread_id,
            sequence=sequence,
            user_input="q" * 8000,
            final_output="a" * 8000,
        )
    _, run_id = await _add_exchange(
        factory, base, thread_id=thread_id, sequence=10, status="RUNNING"
    )
    async with factory() as session:
        version = await session.scalar(
            select(AgentVersion).where(AgentVersion.id == base["version_id"])
        )
        raw_model = version.resolved_spec["model"]
        profile = replace(
            _profile(total=32768, workspace=base["workspace_id"]),
            id=UUID(raw_model["profile_id"]),
            provider_credential_id=UUID(raw_model["credential_ref"]),
        )
        agent = _agent(
            workspace_id=base["workspace_id"],
            model_profile_id=profile.id,
            runtime_config={
                "context_policy": {"policy_version": "adaptive_context_v1"},
                "history_policy": "deterministic_history_v1",
                "summary_policy": {"enabled": True},
            },
        )
        spec = _resolved_spec(
            agent=agent, model=((profile,), {profile.id: "fake"}), bindings=(), tools=()
        )
        version.spec_schema_version = 5
        version.resolved_spec = spec
        version.resolved_spec_hash = canonical_json_hash(spec)
        await session.commit()
        run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id))

    requests = []
    graph = _model_graph(request_sink=requests)
    graph.run = run
    graph.context = SimpleNamespace(
        workspace_id=str(base["workspace_id"]),
        user_id=str(base["user_id"]),
        permissions=frozenset({"agent_run", "tool_run", "workspace_read"}),
    )
    graph.service.session_factory = factory
    graph.service.thread_context_provider = SqlAlchemyThreadContextProvider(factory)
    graph.service.thread_context_max_turns = 10
    graph.service.memory_selector = None
    graph.sequence = 0

    async def step(*args, **kwargs):
        return None

    graph.step = step
    result = await graph.prepare({})
    assert result["failure_code"] == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
    assert "messages" not in result
    assert result["history_checkpoint_binding"]["run_id"] == str(run_id)
    assert requests == []
    async with factory() as session:
        saved = await session.scalar(select(AgentRun).where(AgentRun.id == run_id))
        assert (
            "NO_ELIGIBLE_SOURCE"
            in saved.effective_thread_history_snapshot["plan"]["insufficiencies"]
        )
    # A missing checkpoint runtime field cannot disguise a summary-enabled
    # version as an older, unbound M3 checkpoint.
    with pytest.raises(AgentHubError) as invalid:
        await verify_history_checkpoint_binding(factory, run=saved, state={})
    assert invalid.value.code == "AGENT_HISTORY_SNAPSHOT_INVALID"

    # The blocked identity must survive an actual graph checkpoint, not only
    # a direct call to the prepare node.
    adapter = LangGraphCheckpointAdapter(TEST_DATABASE_URL)
    async with adapter.checkpointer() as checkpointer:
        config = adapter.config_for_run(base["workspace_id"], run_id)
        final = await graph.invoke({}, checkpointer=checkpointer, config=config)
        checkpoint = await checkpointer.aget_tuple(config)
    assert final["failure_code"] == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
    assert checkpoint is not None
    assert (
        checkpoint.checkpoint["channel_values"]["history_checkpoint_binding"]
        == result["history_checkpoint_binding"]
    )
    assert requests == []


@pytest.mark.asyncio
async def test_frozen_history_is_planned_and_sent_to_fake_gateway(factory):
    base = await _seed_workspace(factory)
    thread_id = await _create_thread(factory, base)
    prior_runs = []
    prior_turns = []
    for sequence in range(1, 13):
        prior_turn_id, prior_run_id = await _add_exchange(
            factory, base, thread_id=thread_id, sequence=sequence
        )
        prior_turns.append(prior_turn_id)
        prior_runs.append(prior_run_id)
    _, pending_id = await _add_exchange(
        factory, base, thread_id=thread_id, sequence=13, status="RUNNING"
    )
    _, run_id = await _add_exchange(
        factory, base, thread_id=thread_id, sequence=14, status="RUNNING"
    )
    async with factory() as session:
        version = await session.scalar(
            select(AgentVersion).where(AgentVersion.id == base["version_id"])
        )
        raw_model = version.resolved_spec["model"]
        profile = replace(
            _profile(total=32768, workspace=base["workspace_id"]),
            id=UUID(raw_model["profile_id"]),
            provider_credential_id=UUID(raw_model["credential_ref"]),
        )
        agent = _agent(
            workspace_id=base["workspace_id"],
            model_profile_id=profile.id,
            runtime_config={
                "context_policy": {"policy_version": "adaptive_context_v1"},
                "history_policy": "deterministic_history_v1",
            },
        )
        spec = _resolved_spec(
            agent=agent, model=((profile,), {profile.id: "fake"}), bindings=(), tools=()
        )
        version.spec_schema_version = 4
        version.resolved_spec = spec
        version.resolved_spec_hash = canonical_json_hash(spec)
        await session.commit()
    async with factory() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == run_id))

    requests = []
    graph = _model_graph(request_sink=requests)
    graph.run = run
    graph.context = SimpleNamespace(
        workspace_id=str(base["workspace_id"]),
        user_id=str(base["user_id"]),
        permissions=frozenset({"agent_run", "tool_run", "workspace_read"}),
    )
    graph.service.session_factory = factory
    graph.service.thread_context_provider = SqlAlchemyThreadContextProvider(factory)
    graph.service.thread_context_max_turns = 10
    graph.service.memory_selector = None
    graph.sequence = 0

    async def step(*args, **kwargs):
        return None

    graph.step = step
    state = await graph.prepare({})
    assert state["history_message_count"] == 24
    assert state["history_checkpoint_binding"]["run_id"] == str(run_id)
    # A genuinely old checkpoint without the new binding remains recoverable.
    assert await verify_history_checkpoint_binding(factory, run=run, state={}) is None
    await graph.model(state)
    assert len(requests) == 1
    assert [message.content for message in requests[0].messages if message.role == "user"] == [
        *(f"question {index}" for index in range(1, 13)),
        "question 14",
    ]
    async with factory() as session:
        saved = await session.scalar(select(AgentRun).where(AgentRun.id == run_id))
        assert saved.effective_thread_history_snapshot["plan"]["pre_trim_tokens"] > 0
        original_snapshot = saved.effective_thread_history_snapshot
        changed_plan = dict(original_snapshot["plan"])
        changed_plan["planned_tokens"] += 1
        saved.effective_thread_history_snapshot = {**original_snapshot, "plan": changed_plan}
        await session.commit()
    blocked = await graph.model(state)
    assert blocked["failure_code"] == "AGENT_HISTORY_SNAPSHOT_INVALID"
    assert len(requests) == 1
    async with factory() as session:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(effective_thread_history_snapshot=original_snapshot)
        )
        await session.execute(
            update(AgentRun).where(AgentRun.id == pending_id).values(status="SUCCEEDED")
        )
        await session.commit()
    replay = await graph.prepare({})
    assert replay["history_message_count"] == 24
    assert all(message.content != "question 13" for message in replay["messages"])

    # A historical tool call without a complete stored exchange must never be
    # silently represented as only its question and final answer.
    _, new_run_id = await _add_exchange(
        factory, base, thread_id=thread_id, sequence=15, status="RUNNING"
    )
    async with factory() as session:
        await session.execute(
            update(AgentRun).where(AgentRun.id == prior_runs[0]).values(tool_call_count=1)
        )
        await session.commit()
        graph.run = await session.scalar(select(AgentRun).where(AgentRun.id == new_run_id))
    refused = await graph.prepare({})
    assert refused["failure_code"] == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
    assert len(requests) == 1

    # A future M3 run persists its complete parallel tool exchange in the
    # same transaction as success and a later run sends it as one whole group.
    async with factory() as session:
        await session.execute(
            update(AgentRun).where(AgentRun.id == prior_runs[0]).values(tool_call_count=0)
        )
        await session.commit()
    transcript = [
        ModelMessage("user", "question 15"),
        ModelMessage(
            "assistant",
            tool_calls=(
                ModelToolCall("lookup", {"item": "a"}, "call-a"),
                ModelToolCall("lookup", {"item": "b"}, "call-b"),
            ),
        ),
        ModelMessage("tool", "result-b", name="lookup", tool_call_id="call-b"),
        ModelMessage("tool", "result-a", name="lookup", tool_call_id="call-a"),
        ModelMessage("assistant", "complete"),
    ]
    result = await AgentRunService(factory)._complete_run(
        graph.context,
        new_run_id,
        status="SUCCEEDED",
        final_output="complete",
        failure_code=None,
        model_step_count=2,
        tool_call_count=2,
        usage_records=[],
        turn_transcript=transcript,
    )
    assert result.status == "SUCCEEDED"
    _, next_run_id = await _add_exchange(
        factory, base, thread_id=thread_id, sequence=16, status="RUNNING"
    )
    async with factory() as session:
        graph.run = await session.scalar(select(AgentRun).where(AgentRun.id == next_run_id))
    prepared = await graph.prepare({})
    assert "failure_code" not in prepared
    await graph.model(prepared)
    replayed = requests[-1].messages
    tool_results = [message for message in replayed if message.role == "tool"]
    calls = next(message.tool_calls for message in replayed if message.tool_calls)
    assert [(item.tool_call_id, item.content) for item in tool_results] == [
        (calls[1].provider_tool_call_id, "result-b"),
        (calls[0].provider_tool_call_id, "result-a"),
    ]

    # Capture the transcript through the real model and observation nodes,
    # rather than constructing the persisted transcript in the test.
    _, captured_run_id = await _add_exchange(
        factory, base, thread_id=thread_id, sequence=17, status="RUNNING"
    )
    async with factory() as session:
        graph.run = await session.scalar(select(AgentRun).where(AgentRun.id == captured_run_id))
    captured_state = await graph.prepare({})
    responses = [
        ModelResponse(
            content="",
            provider="fake",
            model="fake",
            tool_calls=(
                ModelToolCall("lookup", {"q": "x"}, "capture-x"),
                ModelToolCall("lookup", {"q": "y"}, "capture-y"),
            ),
        ),
        ModelResponse(content="captured answer", provider="fake", model="fake"),
    ]

    class ScriptedGateway:
        async def generate_resolved(self, context, plan, request):
            requests.append(request)
            return responses.pop(0)

    graph.service.model_gateway_factory = lambda session: ScriptedGateway()
    captured_state.update(await graph.model(captured_state))
    captured_state["proposed_tool_calls"] = [
        {"name": "lookup", "tool_call_id": "capture-x"},
        {"name": "lookup", "tool_call_id": "capture-y"},
    ]
    captured_state["executed_observations"] = {
        "capture-x": ToolResult.success({"value": "x"}),
        "capture-y": ToolResult.success({"value": "y"}),
    }
    captured_state.update(await graph.observation(captured_state))
    captured_state.update(await graph.model(captured_state))
    captured = captured_state["turn_transcript"]
    assert [message.role for message in captured] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    await AgentRunService(factory)._complete_run(
        graph.context,
        captured_run_id,
        status="SUCCEEDED",
        final_output="captured answer",
        failure_code=None,
        model_step_count=2,
        tool_call_count=2,
        usage_records=[],
        turn_transcript=captured,
    )
    _, after_capture_id = await _add_exchange(
        factory, base, thread_id=thread_id, sequence=18, status="RUNNING"
    )
    async with factory() as session:
        graph.run = await session.scalar(select(AgentRun).where(AgentRun.id == after_capture_id))
    restored = await graph.prepare({})
    assert "failure_code" not in restored
    assert sum(message.role == "tool" for message in restored["messages"]) == 4
    responses.append(ModelResponse(content="restored", provider="fake", model="fake"))
    outcome = await graph.model(restored)
    assert outcome["final_output"] == "restored"
    assert sum(message.role == "tool" for message in requests[-1].messages) == 4

    graph.context.permissions = frozenset({"agent_run"})
    request_count = len(requests)
    revoked = await graph.prepare({})
    assert revoked["failure_code"] == "FORBIDDEN"
    assert len(requests) == request_count
    graph.context.permissions = frozenset({"agent_run", "tool_run", "workspace_read"})

    _, invalid_scope_run = await _add_exchange(
        factory, base, thread_id=thread_id, sequence=19, status="RUNNING"
    )
    async with factory() as session:
        await session.execute(
            update(ThreadTurn)
            .where(ThreadTurn.id == prior_turns[0])
            .values(legacy_recovery_required=True)
        )
        await session.commit()
        graph.run = await session.scalar(select(AgentRun).where(AgentRun.id == invalid_scope_run))
    request_count = len(requests)
    invalid = await graph.prepare({})
    assert invalid["failure_code"] == "AGENT_HISTORY_CONTEXT_INSUFFICIENT"
    assert len(requests) == request_count


@pytest.mark.asyncio
async def test_completion_failure_does_not_publish_a_partial_transcript(factory, monkeypatch):
    base = await _seed_workspace(factory)
    thread_id = await _create_thread(factory, base)
    _, run_id = await _add_exchange(
        factory, base, thread_id=thread_id, sequence=1, status="RUNNING"
    )
    context = SimpleNamespace(workspace_id=str(base["workspace_id"]))
    transcript = [ModelMessage("user", "question 1"), ModelMessage("assistant", "answer")]

    async def fail_before_commit(self):
        raise RuntimeError("injected database commit failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(AsyncSession, "commit", fail_before_commit)
        with pytest.raises(RuntimeError, match="injected"):
            await AgentRunService(factory)._complete_run(
                context,
                run_id,
                status="SUCCEEDED",
                final_output="answer",
                failure_code=None,
                model_step_count=1,
                tool_call_count=0,
                usage_records=[],
                turn_transcript=transcript,
            )
    async with factory() as session:
        stored = await session.scalar(select(AgentRun).where(AgentRun.id == run_id))
        assert stored.status == "RUNNING"
        assert stored.thread_history_transcript is None
