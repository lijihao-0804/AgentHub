"""Non-blocking background summary prewarm decision and dispatch hook.

Eliminates online TTFT spikes caused by synchronous thread-history compaction:
when a completed thread Run crosses the soft context pressure threshold
(default 70% from ``context_policy.py``) and ``summary_policy.enabled`` is True,
this module decides whether to enqueue a background summary prewarm job and
dispatches it via a fail-safe port that never blocks or fails the online Run.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from packages.agent_runtime.context_policy import THRESHOLD_PERCENTS

logger = logging.getLogger(__name__)

DEFAULT_SOFT_TRIGGER_PERCENT = THRESHOLD_PERCENTS["soft_trigger_percent"]


@dataclass(frozen=True, slots=True)
class SummaryPrewarmDecision:
    should_prewarm: bool
    reason: str
    utilization_percent: float | None = None
    workspace_id: UUID | None = None
    thread_id: UUID | None = None
    run_id: UUID | None = None


class SummaryPrewarmDispatcher(Protocol):
    """Async background dispatcher port (e.g. Celery task enqueue)."""

    def enqueue_summary_prewarm(
        self,
        *,
        workspace_id: UUID,
        thread_id: UUID,
        run_id: UUID,
        utilization_percent: float,
    ) -> Awaitable[None]: ...


def evaluate_summary_prewarm(
    *,
    workspace_id: UUID,
    thread_id: UUID | None,
    run_id: UUID,
    run_status: str,
    summary_policy: Mapping[str, Any] | None,
    observed_input_tokens: int | None,
    available_input_tokens: int | None,
    has_active_summary_job: bool = False,
) -> SummaryPrewarmDecision:
    """Pure decision rule for background thread summary prewarming."""
    if run_status != "SUCCEEDED":
        return SummaryPrewarmDecision(should_prewarm=False, reason="RUN_NOT_SUCCEEDED")
    if thread_id is None:
        return SummaryPrewarmDecision(should_prewarm=False, reason="NO_THREAD")
    if not isinstance(summary_policy, Mapping) or summary_policy.get("enabled") is not True:
        return SummaryPrewarmDecision(should_prewarm=False, reason="SUMMARY_DISABLED")
    if has_active_summary_job:
        return SummaryPrewarmDecision(should_prewarm=False, reason="ACTIVE_JOB_EXISTS")
    if (
        observed_input_tokens is None
        or available_input_tokens is None
        or observed_input_tokens <= 0
        or available_input_tokens <= 0
    ):
        return SummaryPrewarmDecision(should_prewarm=False, reason="TOKEN_USAGE_UNAVAILABLE")

    soft_trigger = int(summary_policy.get("soft_trigger_percent", DEFAULT_SOFT_TRIGGER_PERCENT))
    utilization = round((observed_input_tokens / available_input_tokens) * 100.0, 2)
    if utilization < soft_trigger:
        return SummaryPrewarmDecision(
            should_prewarm=False,
            reason="BELOW_SOFT_TRIGGER",
            utilization_percent=utilization,
        )
    return SummaryPrewarmDecision(
        should_prewarm=True,
        reason="SOFT_TRIGGER_REACHED",
        utilization_percent=utilization,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
    )


async def maybe_enqueue_summary_prewarm(
    dispatcher: SummaryPrewarmDispatcher | None,
    decision: SummaryPrewarmDecision,
) -> bool:
    """Fail-safe dispatch: enqueue prewarm when decided, never raising into online runs."""
    if dispatcher is None or not decision.should_prewarm:
        return False
    if (
        decision.workspace_id is None
        or decision.thread_id is None
        or decision.run_id is None
        or decision.utilization_percent is None
    ):
        return False
    try:
        await dispatcher.enqueue_summary_prewarm(
            workspace_id=decision.workspace_id,
            thread_id=decision.thread_id,
            run_id=decision.run_id,
            utilization_percent=decision.utilization_percent,
        )
        return True
    except Exception:
        logger.warning("summary_prewarm_enqueue_failed", exc_info=True)
        return False


__all__ = [
    "DEFAULT_SOFT_TRIGGER_PERCENT",
    "SummaryPrewarmDecision",
    "SummaryPrewarmDispatcher",
    "evaluate_summary_prewarm",
    "maybe_enqueue_summary_prewarm",
]
