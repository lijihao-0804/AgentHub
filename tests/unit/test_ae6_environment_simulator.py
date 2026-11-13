"""AE-6a environment and user-simulator contracts."""

from __future__ import annotations

import pytest

from packages.evaluation.environment import FixtureEnvironment, StateAssertion
from packages.evaluation.user_simulator import (
    FrozenUserSimulatorProfile,
    ScriptedUserSimulator,
    Turn,
    contains_canary,
)


@pytest.mark.asyncio
async def test_fixture_environment_reset_and_assert() -> None:
    async def assert_state(handle, assertions):
        return StateAssertion(
            passed=handle.seed == 7 and assertions.get("refund_status") == "APPROVED",
            observed={"seed": handle.seed},
        )

    environment = FixtureEnvironment(workspace_id="ws", assert_state=assert_state)
    handle = await environment.reset(seed=7, fixture_ref="case-1")

    result = await environment.assert_state(handle, {"refund_status": "APPROVED"})

    assert result.passed is True
    assert handle.fixture_hash


@pytest.mark.asyncio
async def test_fixture_environment_rejects_unknown_handle() -> None:
    async def assert_state(handle, assertions):
        return StateAssertion(True)

    environment = FixtureEnvironment(workspace_id="ws", assert_state=assert_state)
    handle = await environment.reset(seed=1, fixture_ref="case-1")
    await environment.teardown(handle)

    result = await environment.assert_state(handle, {})

    assert result.passed is False
    assert result.reason == "environment_not_active"


@pytest.mark.asyncio
async def test_teardown_is_idempotent() -> None:
    async def assert_state(handle, assertions):
        return StateAssertion(True)

    environment = FixtureEnvironment(workspace_id="ws", assert_state=assert_state)
    handle = await environment.reset(seed=1, fixture_ref="case-1")

    await environment.teardown(handle)
    await environment.teardown(handle)


@pytest.mark.asyncio
async def test_scripted_simulator_reveals_only_when_asked() -> None:
    simulator = ScriptedUserSimulator(
        opening_text="我想退货",
        private_facts={"order_id": "A-1001"},
        triggers={"order_id": ["订单号", "order id"]},
        fallback="请帮我处理",
        max_turns=4,
    )

    assert await simulator.opening() == "我想退货"
    assert await simulator.respond(transcript=[], agent_message="您好") == "请帮我处理"
    assert await simulator.respond(transcript=[], agent_message="请提供订单号") == "A-1001"
    assert await simulator.respond(transcript=[], agent_message="订单号呢") == "请帮我处理"


@pytest.mark.asyncio
async def test_scripted_simulator_exhausts_at_max_turns() -> None:
    simulator = ScriptedUserSimulator(
        opening_text="hi",
        private_facts={"a": "x"},
        triggers={"a": ["a"]},
        max_turns=1,
    )

    assert await simulator.respond(transcript=[], agent_message="b") == simulator.fallback
    assert await simulator.respond(transcript=[], agent_message="a") == "（对话已结束）"
    assert simulator.exhausted() is True


def test_scripted_simulator_requires_trigger_for_every_fact() -> None:
    with pytest.raises(ValueError):
        ScriptedUserSimulator(
            opening_text="hi",
            private_facts={"a": "x"},
            triggers={},
        )


def test_frozen_simulator_profile_is_stable_and_seed_sensitive() -> None:
    first = FrozenUserSimulatorProfile(
        simulator_id="s",
        model="m",
        prompt_hash="h",
        temperature=0.0,
        seed=1,
        max_turns=8,
        disclosure_policy_hash="d",
    )
    same = FrozenUserSimulatorProfile(
        simulator_id="s",
        model="m",
        prompt_hash="h",
        temperature=0.0,
        seed=1,
        max_turns=8,
        disclosure_policy_hash="d",
    )
    other = FrozenUserSimulatorProfile(
        simulator_id="s",
        model="m",
        prompt_hash="h",
        temperature=0.0,
        seed=2,
        max_turns=8,
        disclosure_policy_hash="d",
    )

    assert first.evaluator_version == same.evaluator_version
    assert first.evaluator_version != other.evaluator_version
    assert first.evaluator_version.startswith("usv1-")


def test_canary_detection() -> None:
    assert contains_canary("the answer is CANARY-7f3a", "CANARY-7f3a") is True
    assert contains_canary("the answer is 42", "CANARY-7f3a") is False
    assert contains_canary("anything", "") is False


def test_turn_projection() -> None:
    assert Turn(role="user", content="hi").projection() == {"role": "user", "content": "hi"}
