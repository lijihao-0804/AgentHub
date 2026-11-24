"""Unit tests for background summary prewarm decision and fail-safe dispatch."""

from __future__ import annotations

from uuid import uuid4

import pytest

from packages.agent_runtime.summary_prewarm import (
    evaluate_summary_prewarm,
    maybe_enqueue_summary_prewarm,
)


class _RecordingDispatcher:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.should_fail = should_fail

    async def enqueue_summary_prewarm(
        self, *, workspace_id, thread_id, run_id, utilization_percent
    ) -> None:
        if self.should_fail:
            raise RuntimeError("broker down")
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "thread_id": thread_id,
                "run_id": run_id,
                "utilization_percent": utilization_percent,
            }
        )


def test_evaluate_summary_prewarm_respects_soft_trigger_and_guards() -> None:
    ws, tid, rid = uuid4(), uuid4(), uuid4()
    policy = {"enabled": True, "soft_trigger_percent": 70}

    below = evaluate_summary_prewarm(
        workspace_id=ws,
        thread_id=tid,
        run_id=rid,
        run_status="SUCCEEDED",
        summary_policy=policy,
        observed_input_tokens=6000,
        available_input_tokens=10000,
    )
    assert below.should_prewarm is False
    assert below.reason == "BELOW_SOFT_TRIGGER"
    assert below.utilization_percent == 60.0

    above = evaluate_summary_prewarm(
        workspace_id=ws,
        thread_id=tid,
        run_id=rid,
        run_status="SUCCEEDED",
        summary_policy=policy,
        observed_input_tokens=7500,
        available_input_tokens=10000,
    )
    assert above.should_prewarm is True
    assert above.reason == "SOFT_TRIGGER_REACHED"
    assert above.utilization_percent == 75.0

    disabled = evaluate_summary_prewarm(
        workspace_id=ws,
        thread_id=tid,
        run_id=rid,
        run_status="SUCCEEDED",
        summary_policy={"enabled": False},
        observed_input_tokens=9000,
        available_input_tokens=10000,
    )
    assert disabled.should_prewarm is False
    assert disabled.reason == "SUMMARY_DISABLED"

    active = evaluate_summary_prewarm(
        workspace_id=ws,
        thread_id=tid,
        run_id=rid,
        run_status="SUCCEEDED",
        summary_policy=policy,
        observed_input_tokens=9000,
        available_input_tokens=10000,
        has_active_summary_job=True,
    )
    assert active.should_prewarm is False
    assert active.reason == "ACTIVE_JOB_EXISTS"


@pytest.mark.asyncio
async def test_maybe_enqueue_summary_prewarm_dispatches_and_fails_safe() -> None:
    ws, tid, rid = uuid4(), uuid4(), uuid4()
    decision = evaluate_summary_prewarm(
        workspace_id=ws,
        thread_id=tid,
        run_id=rid,
        run_status="SUCCEEDED",
        summary_policy={"enabled": True},
        observed_input_tokens=8000,
        available_input_tokens=10000,
    )
    ok_dispatcher = _RecordingDispatcher()
    assert await maybe_enqueue_summary_prewarm(ok_dispatcher, decision) is True
    assert len(ok_dispatcher.calls) == 1
    assert ok_dispatcher.calls[0]["utilization_percent"] == 80.0

    failing_dispatcher = _RecordingDispatcher(should_fail=True)
    assert await maybe_enqueue_summary_prewarm(failing_dispatcher, decision) is False
