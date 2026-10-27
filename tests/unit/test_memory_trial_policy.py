"""Trial pressure is measured against the frozen declared model limit."""

import pytest

from benchmarks.evaluation.memory_trial_policy import create_trial_policy
from packages.agent_runtime.context_budget import ContextCategory, ContextMessage
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelMessage


def test_trial_uses_32k_route_and_actual_64k_preset_intersection():
    policy = create_trial_policy()
    assert policy.context_limit == 32768
    assert policy.reserved_output_tokens == 2048
    assert policy.context_policy["input_limit_tokens"] == 29184
    assert policy.context_policy["thresholds"] == {"soft": 20429, "hard": 24807, "target": 16051}


def test_oversized_current_task_cannot_be_silently_trimmed():
    policy = create_trial_policy()
    with pytest.raises(AgentHubError):
        policy.admit(
            [ContextMessage(ModelMessage("user", "x" * 130000), ContextCategory.CURRENT_USER_TASK)]
        )
