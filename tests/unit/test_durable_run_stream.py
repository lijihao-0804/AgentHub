"""A run must outlive the connection that started it, and cost what it may.

These cover the three pieces that decouple a run from its HTTP request --
the stream hub's ownership rules, the durable event log's persistence rules,
and the per-run spend ceiling -- without needing a database or a model.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.event_store import (
    NON_PERSISTED_EVENT_TYPES,
    _event_from_row,
    should_persist,
)
from packages.agent_runtime.events import AgentEvent, AgentEventType
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.publish import _validate_runtime_config
from packages.agent_runtime.runtime import _cost_guard_failure
from packages.agent_runtime.stream_hub import RunStreamHub, RunStreamRegistry
from packages.core.errors.exceptions import AgentHubError

RUN_ID = uuid4()
AGENT_VERSION_ID = uuid4()


def make_event(sequence: int, event_type: AgentEventType = AgentEventType.MESSAGE_STARTED):
    payload = (
        {"delta": "x"}
        if event_type is AgentEventType.MESSAGE_DELTA
        else {"model_round": 1, "status": "SUCCEEDED"}
        if event_type is AgentEventType.MESSAGE_COMPLETED
        else {"model_round": 1}
    )
    return AgentEvent(
        sequence=sequence,
        type=event_type,
        request_id="req-1",
        run_id=str(RUN_ID),
        agent_version_id=str(AGENT_VERSION_ID),
        timestamp=datetime.now(UTC),
        payload=payload,
    )


def build_hub(*, grace_seconds: float | None = 0.05):
    source: asyncio.Queue = asyncio.Queue()
    aborted: list[bool] = []

    async def abort() -> None:
        aborted.append(True)

    hub = RunStreamHub(
        run_id=RUN_ID,
        source=source,
        abort=abort,
        grace_seconds=grace_seconds,
    )
    return hub, source, aborted


async def drain(hub: RunStreamHub, *, stop_after: int) -> list[AgentEvent]:
    received: list[AgentEvent] = []
    async for event in hub.subscribe():
        received.append(event)
        if len(received) >= stop_after:
            break
    return received


# --- hub ownership ---------------------------------------------------------


@pytest.mark.asyncio
async def test_one_subscriber_leaving_does_not_end_the_run():
    """The defect this whole change exists to fix: a closed tab killed a run."""

    hub, source, aborted = build_hub(grace_seconds=5)
    hub.start()
    stayer = asyncio.create_task(drain(hub, stop_after=2))
    await asyncio.sleep(0)
    leaver = asyncio.create_task(drain(hub, stop_after=1))
    await asyncio.sleep(0)

    source.put_nowait(make_event(1))
    await leaver  # one consumer disconnects after a single event
    source.put_nowait(make_event(2))

    assert len(await stayer) == 2
    assert aborted == []
    await hub.aclose()


@pytest.mark.asyncio
async def test_last_subscriber_leaving_aborts_only_after_the_grace_window():
    """"Nobody is watching any more" must still end the run -- just not instantly."""

    hub, source, aborted = build_hub(grace_seconds=0.05)
    hub.start()
    consumer = asyncio.create_task(drain(hub, stop_after=1))
    await asyncio.sleep(0)
    source.put_nowait(make_event(1))
    await consumer

    assert aborted == [], "abort must not be immediate"
    await asyncio.sleep(0.15)
    assert aborted == [True]
    await hub.aclose()


@pytest.mark.asyncio
async def test_reattaching_inside_the_grace_window_cancels_the_abort():
    hub, source, aborted = build_hub(grace_seconds=0.2)
    hub.start()
    first = asyncio.create_task(drain(hub, stop_after=1))
    await asyncio.sleep(0)
    source.put_nowait(make_event(1))
    await first

    second = asyncio.create_task(drain(hub, stop_after=1))
    await asyncio.sleep(0.05)  # re-attach well inside the window
    source.put_nowait(make_event(2))
    await second

    await asyncio.sleep(0.1)
    assert aborted == [], "a client that came back must not lose its run"
    await hub.aclose()


@pytest.mark.asyncio
async def test_a_detached_run_is_never_aborted_for_being_unwatched():
    """In the worker, nobody watching is the normal state, not abandonment."""

    hub, source, aborted = build_hub(grace_seconds=None)
    hub.start()
    await asyncio.sleep(0.05)
    source.put_nowait(make_event(1))
    source.put_nowait(None)
    await hub.wait_closed()

    assert aborted == []
    assert hub.closed is True
    assert hub.last_sequence == 1


@pytest.mark.asyncio
async def test_registry_forgets_a_closed_run():
    hub, source, _ = build_hub(grace_seconds=None)
    registry = RunStreamRegistry()
    registry.register(hub)
    assert registry.get(RUN_ID) is hub

    hub.start()
    source.put_nowait(None)
    await hub.wait_closed()

    assert registry.get(RUN_ID) is None
    assert len(registry) == 0


# --- durable log ------------------------------------------------------------


def test_deltas_are_deliberately_not_persisted():
    """Persisting deltas would make the log O(tokens) to buy a typing animation."""

    assert NON_PERSISTED_EVENT_TYPES == frozenset({AgentEventType.MESSAGE_DELTA})
    assert should_persist(make_event(1, AgentEventType.MESSAGE_DELTA)) is False
    assert should_persist(make_event(2, AgentEventType.MESSAGE_COMPLETED)) is True


def test_a_naive_stored_timestamp_is_read_back_as_utc():
    """asyncpg can hand back a naive datetime; AgentEvent refuses one."""

    row = SimpleNamespace(
        sequence=7,
        event_type=AgentEventType.MESSAGE_STARTED.value,
        request_id="req-1",
        run_id=RUN_ID,
        agent_run_id=RUN_ID,
        step_id=None,
        event_id="evt-7",
        occurred_at=datetime(2026, 9, 21, 12, 0, 0),  # noqa: DTZ001 - the point of the test
        payload={"model_round": 1},
    )
    event = _event_from_row(row, agent_version_id=AGENT_VERSION_ID)
    assert event.timestamp.tzinfo is UTC
    assert event.sequence == 7


# --- cost ceiling -----------------------------------------------------------


def usage(amount: str, currency: str = "USD") -> list[dict]:
    return [
        {
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "cached_tokens": 0,
            },
            "cost": {"amount": Decimal(amount), "currency": currency, "is_estimate": True},
        }
    ]


def test_no_ceiling_means_no_guard():
    assert _cost_guard_failure(usage("999.00"), None) is None


def test_the_first_round_is_never_blocked():
    assert _cost_guard_failure([], 1) is None


def test_spend_below_the_ceiling_continues():
    assert _cost_guard_failure(usage("0.004"), 5_000) is None


def test_spend_at_or_above_the_ceiling_stops_the_run():
    assert _cost_guard_failure(usage("0.005"), 5_000) == "AGENT_COST_LIMIT_EXCEEDED"
    assert _cost_guard_failure(usage("0.020"), 5_000) == "AGENT_COST_LIMIT_EXCEEDED"


def test_an_unpriced_or_foreign_currency_run_stops_rather_than_pretending():
    """A ceiling that silently does nothing reads as a guarantee it is not."""

    unpriced = [{"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}]
    assert _cost_guard_failure(unpriced, 5_000) == "AGENT_COST_UNMEASURABLE"
    assert _cost_guard_failure(usage("0.001", "EUR"), 5_000) == "AGENT_COST_UNMEASURABLE"


def test_a_provider_that_reports_no_usage_at_all_stops_the_run():
    """The silent hole: a ceiling with nothing to measure must not read as a pass.

    A provider that returns no usage leaves the record list empty however much the
    run has spent, which is why emptiness alone cannot mean "nothing spent yet".
    """

    assert _cost_guard_failure([], 5_000, rounds_completed=0) is None
    assert _cost_guard_failure([], 5_000, rounds_completed=1) == "AGENT_COST_UNMEASURABLE"
    assert _cost_guard_failure([], None, rounds_completed=3) is None


# --- frozen config ----------------------------------------------------------


def test_an_agent_published_without_a_ceiling_is_uncapped():
    assert _validate_runtime_config({})["max_cost_micro_usd"] is None


def test_a_ceiling_is_frozen_as_given():
    assert _validate_runtime_config({"max_cost_micro_usd": 250_000})["max_cost_micro_usd"] == (
        250_000
    )


def _resolved_spec(runtime: dict) -> dict:
    return {
        "spec_schema_version": 2,
        "model": {
            "profile_id": str(uuid4()),
            "credential_ref": str(uuid4()),
            "provider": "deepseek",
            "model": "deepseek-chat",
            "temperature": "0.2",
            "max_tokens": 2048,
            "timeout_seconds": "60",
            "capabilities": {
                "vision": False,
                "streaming": True,
                "tool_calling": True,
                "structured_output": True,
                "max_context_tokens": 65536,
            },
        },
        "prompt": {"system_prompt": "You are helpful.", "prompt_version": 1},
        "retrieval": {"knowledge_bindings": []},
        "runtime": runtime,
    }


@pytest.mark.parametrize("ceiling", [None, 1, 250_000])
def test_the_ceiling_survives_the_freeze_boundary(ceiling):
    """A ceiling the executing spec does not carry is a ceiling that does nothing.

    Publication validates ``max_cost_micro_usd`` and writes it into the resolved
    spec; the guard reads it off the frozen runtime. If the parse in between drops
    the key, both halves pass their own tests and the run is silently uncapped.
    """

    spec = parse_frozen_agent_spec(
        _resolved_spec({"max_steps": 8, "max_cost_micro_usd": ceiling}),
        workspace_id=uuid4(),
    )

    assert spec.runtime["max_cost_micro_usd"] == ceiling


@pytest.mark.parametrize("ceiling", [0, -1, True, "5000", 100_000_001])
def test_a_spec_carrying_an_impossible_ceiling_is_refused(ceiling):
    with pytest.raises(AgentHubError):
        parse_frozen_agent_spec(
            _resolved_spec({"max_cost_micro_usd": ceiling}), workspace_id=uuid4()
        )


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1000", 100_000_001])
def test_an_unusable_ceiling_is_refused_at_publish_time(value):
    with pytest.raises(AgentHubError):
        _validate_runtime_config({"max_cost_micro_usd": value})
