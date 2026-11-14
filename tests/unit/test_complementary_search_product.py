"""Published complementary search: scope, receipts, fallback and hard-cost guards."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from benchmarks.public_rag.consumer_replay import CONTEXT, SNAPSHOT, _Session, _spec, definition
from packages.agent_runtime.publish import _validate_retrieval_config
from packages.core.execution_context.call_scope import (
    capture_auxiliary_model_calls,
    execution_deadline,
)
from packages.knowledge.contracts import RetrievalResult, RetrievalTrace, RetrievalTraceStage
from packages.model_gateway.contracts import CostEstimate, ModelResponse, ModelUsage, RetryPolicy
from packages.tools.builtins.search_knowledge import search_knowledge
from packages.tools.contracts import ToolExecutionContext
from packages.tools.errors import ToolHandlerError
from tests.unit.test_complementary_evidence_set import sources


def fixture(output='{"selected_ids":[2]}', opt_in=True):
    plan = _spec({}).model_plan
    primary = replace(plan.primary, max_tokens=2048)
    plan = replace(plan, primary=primary, fallbacks=(primary,), retry_policy=RetryPolicy(3))
    gateway = SimpleNamespace(
        generate_resolved=AsyncMock(
            return_value=ModelResponse(
                output,
                "openai-compatible",
                primary.model,
                usage=ModelUsage(33, 5, 38),
                cost_estimate=CostEstimate(Decimal(".0003"), "USD"),
            )
        )
    )
    ctx = ToolExecutionContext(CONTEXT, UUID(int=9), "id", (gateway, plan))
    tool = definition()
    if opt_in:
        profile = _validate_retrieval_config(
            {
                "evidence_set_selector": "complementary_v1",
                "final_top_k": 2,
                "context_budget_bytes": 8000,
            }
        )
        tool = replace(tool, retrieval_config={**tool.retrieval_config, **profile})
    empty = RetrievalTraceStage(0, ())
    trace = RetrievalTrace(str(SNAPSHOT), empty, empty, empty, empty, 1)
    base = SimpleNamespace(
        retrieve_with_trace=AsyncMock(return_value=RetrievalResult(sources(), trace))
    )
    return ctx, tool, base, gateway


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output,expected,reason",
    [
        ('{"selected_ids":[2]}', [2], None),
        ('{"selected_ids":[42]}', [0, 1], "ValueError"),
        ('{"selected_ids":[]}', [], None),
    ],
)
async def test_originals_only_in_admitted_order_single_attempt_and_usage(output, expected, reason):
    ctx, tool, base, gateway = fixture(output)
    with capture_auxiliary_model_calls(allow_unpriced=True) as ledger:
        result = await search_knowledge(
            ctx, tool, {"query": "q", "limit": 2}, _Session, retriever=base
        )
    assert [r["chunk_id"] for r in result["results"]] == [sources()[i].chunk_id for i in expected]
    (receipt,) = result["evidence_selection"]
    assert receipt["status"] == ("fallback" if reason else "selected")
    assert receipt.get("reason") == reason
    assert receipt["snapshot_id"] == str(SNAPSHOT)
    assert all(r["snapshot_id"] == str(SNAPSHOT) for r in result["evidence_refs"])
    base.retrieve_with_trace.assert_awaited_once()
    assert base.retrieve_with_trace.call_args.args[1].final_top_k == 20
    gateway.generate_resolved.assert_awaited_once()
    actual_ctx, plan, request = gateway.generate_resolved.call_args.args
    assert actual_ctx.workspace_id == ctx.workspace_id
    assert plan.primary == replace(ctx.selector_binding[1].primary, max_tokens=256)
    assert not plan.fallbacks and plan.retry_policy.max_attempts == 1
    assert "A is a member of B." in request.messages[1].content
    (record,) = ledger.records
    assert record["source"] == "complementary_selector"
    assert record["usage"]["total_tokens"] == 38 and record["cost"]["amount"] == Decimal(".0003")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,reason",
    [
        ("default", None),
        ("cost", "cost_budget_unpriced_selector"),
        ("deadline", "TimeoutError"),
        ("missing", "unconfigured"),
        ("context", "AGENT_CONTEXT_BUDGET_EXCEEDED"),
    ],
)
async def test_no_selector_dispatch_before_admission(mode, reason):
    ctx, tool, base, gateway = fixture(opt_in=mode != "default")
    if mode == "missing":
        ctx = replace(ctx, selector_binding=None)
    if mode == "context":
        plan = ctx.selector_binding[1]
        ctx = replace(
            ctx,
            selector_binding=(
                gateway,
                replace(
                    plan,
                    primary=replace(
                        plan.primary,
                        capabilities=replace(plan.primary.capabilities, max_context_tokens=128),
                    ),
                ),
            ),
        )
    with (
        capture_auxiliary_model_calls(allow_unpriced=mode != "cost") as ledger,
        execution_deadline(0.01 if mode == "deadline" else 30),
    ):
        result = await search_knowledge(
            ctx, tool, {"query": "q", "limit": 2}, _Session, retriever=base
        )
    assert not ledger.records
    gateway.generate_resolved.assert_not_awaited()
    if mode == "default":
        assert "evidence_selection" not in result
    else:
        assert result["evidence_selection"][0]["reason"] == reason
        assert [r["chunk_id"] for r in result["results"]] == [s.chunk_id for s in sources()[:2]]


@pytest.mark.asyncio
async def test_invalid_binding_or_snapshot_cannot_dispatch_or_recover_another_tenant():
    ctx, tool, base, gateway = fixture()
    plan = ctx.selector_binding[1]
    alien = replace(plan, primary=replace(plan.primary, workspace_id=uuid4()))
    with pytest.raises(ValueError, match="workspace"):
        await search_knowledge(
            replace(ctx, selector_binding=(gateway, alien)),
            tool,
            {"query": "q"},
            _Session,
            retriever=base,
        )
    broken = replace(tool, snapshot_refs=({**tool.snapshot_refs[0], "snapshot_hash": "wrong"},))
    with pytest.raises(ToolHandlerError):
        await search_knowledge(ctx, broken, {"query": "q"}, _Session, retriever=base)
    gateway.generate_resolved.assert_not_awaited()
    base.retrieve_with_trace.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_read_uses_frozen_gateway_and_persists_selector_receipt_and_usage():
    from benchmarks.public_rag.consumer_replay import _Session
    from packages.tools.contracts import ToolResult
    from tests.unit.test_runtime_rag_evidence_projection import _graph

    ctx, tool, base, provider = fixture()
    prepared = SimpleNamespace(prepare_resolved=AsyncMock(return_value=provider))
    graph = _graph()
    graph.context = CONTEXT
    graph.run = SimpleNamespace(id=uuid4(), agent_version_id=uuid4(), thread_id=None)
    graph.service.session_factory = _Session
    graph.service.model_gateway_factory = lambda _: prepared
    graph.service.artifact_recorder = None

    async def execute(**kwargs):
        actual_ctx = replace(ctx, selector_binding=kwargs["selector_binding"])
        data = await search_knowledge(
            actual_ctx, tool, kwargs["arguments"], _Session, retriever=base
        )
        return ToolResult.success(data)

    graph.service.tool_runtime = SimpleNamespace(execute=execute)
    graph._stop_if_requested = AsyncMock(return_value=None)
    graph._emit = AsyncMock()
    graph.step = AsyncMock()
    update = await graph.read_execute(
        {
            "spec": SimpleNamespace(model_plan=ctx.selector_binding[1]),
            "tool_definitions": {"search_knowledge": tool},
            "pending_tool_calls": [
                {"name": "search_knowledge", "arguments": {"query": "q"}, "tool_call_id": "c"}
            ],
            "runtime": {"max_parallel_reads": 1},
            "model_round_count": 1,
            "usage_records": [],
        }
    )
    prepared.prepare_resolved.assert_awaited_once()
    provider.generate_resolved.assert_awaited_once()
    meta = next(
        call.args[2] for call in graph.step.await_args_list if call.args[0] == "EVIDENCE_SELECTION"
    )
    assert meta["status"] == "selected" and meta["snapshot_id"] == str(SNAPSHOT)
    assert update["usage_records"][0]["usage"]["total_tokens"] == 38
