from __future__ import annotations

from uuid import UUID

from packages.tools.actions import ActionExecutionResult, ActionExecutionStatus


def test_action_result_exposes_unknown_outcome_without_retry_semantics() -> None:
    result = ActionExecutionResult.unknown_outcome()
    assert result.status is ActionExecutionStatus.UNKNOWN_OUTCOME
    assert result.failure_code == "ACTION_OUTCOME_UNKNOWN"
    assert result.data is None


def test_action_result_does_not_require_provider_tool_call_id() -> None:
    result = ActionExecutionResult.succeeded(
        {"ticket_id": str(UUID("11111111-1111-1111-1111-111111111111"))}
    )
    assert result.status is ActionExecutionStatus.SUCCEEDED
