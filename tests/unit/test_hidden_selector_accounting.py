"""Selector billing, Run/checkpoint propagation and remaining-deadline fallback."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from packages.agent_runtime.runtime_projection import _aggregate_usage
from packages.core.execution_context.call_scope import (
    auxiliary_model_attempt,
    capture_auxiliary_model_calls,
    execution_deadline,
)
from packages.knowledge.adapters.onyx_selection import OpenAICompatibleSectionSelector
from packages.knowledge.onyx_retriever import OnyxSelectionKnowledgeRetriever
from packages.tools.contracts import ToolResult
from tests.unit.test_onyx_selection_optin import _FakeRetriever, _pool, _query, _Selector
from tests.unit.test_runtime_rag_evidence_projection import _graph


def selector(client):
    return OpenAICompatibleSectionSelector(
        base_url="http://model.test/v1", api_key="test-only", model="selector-model", client=client
    )


def reply(request, content, status=200):
    return httpx.Response(
        status,
        request=request,
        json={
            "model": "selector-model",
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 5,
                "total_tokens": 205,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
            "choices": [{"message": {"content": content}}],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,status,success",
    [
        ('{"section_ids":[2]}', 200, True),
        ("invalid-json", 200, False),
        ("invalid-json", 503, False),
    ],
)
async def test_paid_selector_usage_survives_selection_failure(content, status, success):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: reply(request, content, status))
    ) as client:
        wrapper = OnyxSelectionKnowledgeRetriever(_FakeRetriever(_pool()), selector(client))
        with capture_auxiliary_model_calls(allow_unpriced=True) as calls:
            result = await wrapper.retrieve_with_trace(object(), _query())
    assert len(calls.records) == 1
    attempt = calls.records[0]
    assert attempt["model"] == "selector-model" and attempt["usage"]["total_tokens"] == 205
    assert attempt["attempt_status"] == ("SUCCEEDED" if success else "FAILED")
    assert result.evidence
    totals = _aggregate_usage(calls.records)
    assert totals["total_tokens"] == 205 and totals["total_cached_tokens"] == 20
    assert totals["total_cost_amount"] is None
    assert result.trace.evidence_set.metadata["status"] == ("selected" if success else "fallback")


@pytest.mark.asyncio
async def test_capped_run_never_dispatches_unpriced_selector():
    service = _Selector((2,))
    wrapper = OnyxSelectionKnowledgeRetriever(_FakeRetriever(_pool()), service)
    with capture_auxiliary_model_calls(allow_unpriced=False) as calls:
        result = await wrapper.retrieve_with_trace(object(), _query())
    assert service.calls == [] and calls.records == []
    assert [item.chunk_id for item in result.evidence] == [item.chunk_id for item in _pool()][:5]
    assert result.trace.evidence_set.metadata["reason"] == "cost_budget_unpriced_selector"


@pytest.mark.asyncio
async def test_selector_timeout_returns_existing_pool_before_outer_tool_deadline():
    class SlowBase(_FakeRetriever):
        async def retrieve_with_trace(self, context, query):
            await asyncio.sleep(0.02)
            return await super().retrieve_with_trace(context, query)

    class StuckSelector:
        model = "selector-model"

        async def select_sections(self, *_):
            await asyncio.Event().wait()

    wrapper = OnyxSelectionKnowledgeRetriever(SlowBase(_pool()), StuckSelector())
    with capture_auxiliary_model_calls(allow_unpriced=True) as calls, execution_deadline(0.4):
        async with asyncio.timeout(0.4):
            result = await wrapper.retrieve_with_trace(object(), _query())
    assert result.evidence and result.trace.evidence_set.metadata["reason"] == "selector_deadline"
    assert calls.records[0]["attempt_status"] == "UNKNOWN_TIMEOUT"
    assert _aggregate_usage(calls.records)["total_cost_amount"] is None


@pytest.mark.asyncio
async def test_no_time_left_skips_selector_without_charging_an_attempt():
    service = _Selector((2,))
    with capture_auxiliary_model_calls(allow_unpriced=True) as calls, execution_deadline(0.05):
        result = await OnyxSelectionKnowledgeRetriever(
            _FakeRetriever(_pool()), service
        ).retrieve_with_trace(object(), _query())
    assert result.evidence and service.calls == [] and calls.records == []


@pytest.mark.asyncio
async def test_graph_checkpoints_auxiliary_usage_and_audit_identity():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: reply(request, '{"section_ids":[2]}'))
    ) as client:
        wrapper = OnyxSelectionKnowledgeRetriever(_FakeRetriever(_pool()), selector(client))

        class Tools:
            async def execute(self, **kwargs):
                await wrapper.retrieve_with_trace(object(), _query())
                return ToolResult.success({"results": []})

        graph = _graph()
        graph.context = SimpleNamespace(workspace_id=str(uuid4()))
        graph.run = SimpleNamespace(id=uuid4(), agent_version_id=uuid4(), thread_id=None)
        graph.service.tool_runtime = Tools()
        graph.service.artifact_recorder = None
        audit = []

        async def stop():
            return None

        async def emit(*_):
            pass

        async def step(kind, status, metadata):
            audit.append((kind, status, metadata))

        graph._stop_if_requested, graph._emit, graph.step = stop, emit, step
        state = {
            "pending_tool_calls": [
                {"name": "search_knowledge", "arguments": {}, "tool_call_id": "call"}
            ],
            "runtime": {"max_parallel_reads": 1},
            "model_round_count": 1,
            "usage_records": [
                {
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                        "cached_tokens": 0,
                    },
                    "cost": {"amount": Decimal("0.001"), "currency": "USD", "is_estimate": True},
                }
            ],
        }
        update = await graph.read_execute(state)
    assert len(update["usage_records"]) == 2
    totals = _aggregate_usage(update["usage_records"])
    assert totals["total_tokens"] == 217 and totals["total_cost_amount"] is None
    attempt = next(metadata for kind, _, metadata in audit if kind == "AUXILIARY_MODEL")
    assert attempt["model"] == "selector-model" and attempt["usage_source"] == "onyx_selector"


@pytest.mark.asyncio
async def test_cancelled_read_drains_siblings_before_closing_the_usage_scope():
    stopped = asyncio.Event()

    class Tools:
        async def execute(self, **kwargs):
            if kwargs["tool_identity"] == "first":
                raise asyncio.CancelledError
            with auxiliary_model_attempt(source="onyx_selector", provider="test", model="test"):
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

    graph = _graph()
    graph.context = SimpleNamespace(workspace_id=str(uuid4()))
    graph.run = SimpleNamespace(id=uuid4(), agent_version_id=uuid4())
    graph.service.tool_runtime = Tools()

    async def stop():
        return None

    async def emit(*_):
        pass

    graph._stop_if_requested, graph._emit = stop, emit
    state = {
        "pending_tool_calls": [
            {"name": name, "arguments": {}, "tool_call_id": name} for name in ("first", "second")
        ],
        "runtime": {"max_parallel_reads": 2},
        "usage_records": [],
    }
    with pytest.raises(asyncio.CancelledError):
        await graph.read_execute(state)
    assert stopped.is_set()
    assert len(graph.usage_records) == 1 and graph.usage_records[0]["usage"] is None
