"""M4-D AgentRun execution facade.

The implementation lives in runtime_service (orchestration), runtime_graph
(state machine) and runtime_projection (pure helpers); this module keeps the
historical import surface stable for the 42 files that import from here.
"""

from __future__ import annotations

from packages.agent_runtime.frozen import FrozenAgentSpec
from packages.agent_runtime.runtime_graph import _AgentRunGraph
from packages.agent_runtime.runtime_projection import (
    _RUNTIME_POLICY,
    _SYSTEM_PREFIX_LENGTH,
    _TERMINAL_TOOL_ERRORS,
    DEFAULT_STREAM_GRACE_SECONDS,
    DEFAULT_THREAD_CONTEXT_MAX_TURNS,
    STREAM_FOLLOW_STOP_STATUSES,
    AgentRunExecutionOverrides,
    AgentRunResult,
    AgentRunState,
    _aggregate_usage,
    _append_tool_call_delta,
    _approval_interrupt_payload,
    _approval_tool_result,
    _bounded_tool_result,
    _categorize_messages,
    _close_event_queue,
    _cost_guard_failure,
    _is_uncertain_action_failure,
    _is_uuid,
    _memory_message,
    _queue_event,
    _run_result,
    _safe_end_span,
    _safe_start_span,
    _safe_step_metadata,
    _usage_record,
    _usage_step_metadata,
)
from packages.agent_runtime.runtime_service import AgentRunService

__all__ = [
    "DEFAULT_STREAM_GRACE_SECONDS",
    "DEFAULT_THREAD_CONTEXT_MAX_TURNS",
    "STREAM_FOLLOW_STOP_STATUSES",
    "AgentRunExecutionOverrides",
    "AgentRunResult",
    "AgentRunService",
    "AgentRunState",
    "FrozenAgentSpec",
    "_AgentRunGraph",
    "_RUNTIME_POLICY",
    "_SYSTEM_PREFIX_LENGTH",
    "_TERMINAL_TOOL_ERRORS",
    "_aggregate_usage",
    "_append_tool_call_delta",
    "_approval_interrupt_payload",
    "_approval_tool_result",
    "_bounded_tool_result",
    "_categorize_messages",
    "_close_event_queue",
    "_cost_guard_failure",
    "_is_uncertain_action_failure",
    "_is_uuid",
    "_memory_message",
    "_queue_event",
    "_run_result",
    "_safe_end_span",
    "_safe_start_span",
    "_safe_step_metadata",
    "_usage_record",
    "_usage_step_metadata",
]
