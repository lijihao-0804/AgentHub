"""Unit tests for loop completion verifier and governed read-only subagent contract."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from packages.agent_runtime.subagent_contract import (
    LoopChecklistItem,
    SubagentManager,
    SubagentTemplate,
    verify_loop_completion,
)
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
)


def _tool_def(name: str, effect: ToolEffect) -> ToolDefinition:
    return ToolDefinition(
        identity=name,
        revision_id=uuid4(),
        spec_hash=f"hash:{name}",
        description=name,
        input_schema={"type": "object", "properties": {}},
        effect=effect,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=10,
    )


def test_loop_verifier_blocks_premature_completion_and_missing_evidence() -> None:
    incomplete = verify_loop_completion(
        [
            LoopChecklistItem("step-1", "Check invoice", "completed", True, "R00001E1"),
            LoopChecklistItem("step-2", "Verify refund window", "in_progress", True, None),
        ]
    )
    assert incomplete.complete is False
    assert incomplete.incomplete_ids == ("step-2",)
    assert incomplete.continuation_hint is not None

    no_evidence = verify_loop_completion(
        [
            LoopChecklistItem("step-1", "Check invoice", "completed", True, None),
        ]
    )
    assert no_evidence.complete is False
    assert no_evidence.missing_evidence_ids == ("step-1",)

    done = verify_loop_completion(
        [
            LoopChecklistItem("step-1", "Check invoice", "completed", True, "R00001E1"),
            LoopChecklistItem("step-2", "Summarize", "completed", False, None),
        ]
    )
    assert done.complete is True
    assert done.continuation_hint is None


def test_subagent_template_rejects_write_tools_and_nested_recursion() -> None:
    catalog = {
        "search_knowledge": _tool_def("search_knowledge", ToolEffect.READ),
        "create_ticket": _tool_def("create_ticket", ToolEffect.WRITE),
    }
    bad = SubagentTemplate(
        template_id="bad_writer",
        model_profile="flash",
        system_prompt="write",
        allowed_tools=("search_knowledge", "create_ticket"),
    )
    with pytest.raises(ValueError, match="non-READ"):
        bad.validate(catalog)

    good = SubagentTemplate(
        template_id="reader",
        model_profile="flash",
        system_prompt="read only",
        allowed_tools=("search_knowledge",),
        max_cost_micro_usd=200_000,
    )

    async def noop_runner(tmpl, task, cancel_ev):  # noqa: ANN001
        return "ok", 10_000

    nested_mgr = SubagentManager(
        templates=[good],
        tool_catalog=catalog,
        runner=noop_runner,
        parent_budget_micro_usd=1_000_000,
        current_depth=1,
    )
    with pytest.raises(ValueError, match="recursion"):
        nested_mgr.spawn(template_id="reader", task="subtask")


@pytest.mark.asyncio
async def test_subagent_manager_shares_budget_offloads_large_output_and_cascades_cancel() -> None:
    catalog = {"search_knowledge": _tool_def("search_knowledge", ToolEffect.READ)}
    tmpl = SubagentTemplate(
        template_id="lit_reader",
        model_profile="flash",
        system_prompt="read papers",
        allowed_tools=("search_knowledge",),
        max_cost_micro_usd=300_000,
    )

    async def big_output_runner(template, task, cancel_ev):  # noqa: ANN001
        if task == "slow":
            await asyncio.sleep(5)
        return "evidence line\n" * 200, 120_000

    mgr = SubagentManager(
        templates=[tmpl],
        tool_catalog=catalog,
        runner=big_output_runner,
        parent_budget_micro_usd=1_000_000,
        offload_threshold_bytes=500,
    )
    handle = mgr.spawn(template_id="lit_reader", task="fast")
    result = await handle
    assert result.status == "SUCCEEDED"
    assert result.offloaded_artifact_ref is not None
    assert result.stored_offload is not None
    # Reserved 300k, spent 120k -> 180k refunded -> 880k remaining
    assert mgr.remaining_budget_micro_usd == 880_000

    # Cascading parent cancel propagates to slow child
    slow_handle = mgr.spawn(template_id="lit_reader", task="slow")
    await asyncio.sleep(0.02)
    await mgr.cancel_all()
    assert mgr.is_cancelled is True
    assert slow_handle.done()


@pytest.mark.asyncio
async def test_subagent_manager_enforces_max_total_spawns_and_concurrency() -> None:
    catalog = {"search_knowledge": _tool_def("search_knowledge", ToolEffect.READ)}
    tmpl = SubagentTemplate(
        template_id="reader",
        model_profile="flash",
        system_prompt="read",
        allowed_tools=("search_knowledge",),
        max_cost_micro_usd=10_000,
    )

    async def slow_runner(template, task, cancel_ev):  # noqa: ANN001
        await asyncio.sleep(0.05)
        return "ok", 5_000

    mgr = SubagentManager(
        templates=[tmpl],
        tool_catalog=catalog,
        runner=slow_runner,
        parent_budget_micro_usd=1_000_000,
        max_total_spawns=2,
        max_concurrent=1,
    )
    h1 = mgr.spawn(template_id="reader", task="t1")
    with pytest.raises(ValueError, match="concurrent"):
        mgr.spawn(template_id="reader", task="t2")
    await h1

    h2 = mgr.spawn(template_id="reader", task="t2")
    await h2
    assert mgr.total_spawns == 2

    with pytest.raises(ValueError, match="total subagent spawns"):
        mgr.spawn(template_id="reader", task="t3")
