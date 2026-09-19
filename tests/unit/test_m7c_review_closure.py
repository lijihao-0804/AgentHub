from types import SimpleNamespace

import pytest

from packages.core.config.settings import Settings
from packages.evaluation.runner import DeterministicEvaluationDriver


def test_evaluation_heartbeat_must_be_shorter_than_lease() -> None:
    with pytest.raises(ValueError, match="evaluation runner heartbeat"):
        Settings(evaluation_runner_lease_seconds=30, evaluation_runner_heartbeat_seconds=30)


@pytest.mark.asyncio
async def test_expected_agent_failure_is_safe_observation_data() -> None:
    result = await DeterministicEvaluationDriver().execute(
        None,
        run=SimpleNamespace(),
        variant=SimpleNamespace(variant_hash="v" * 64),
        item=SimpleNamespace(
            category="FAILURE",
            input={"request": "expected failure"},
            expected={"status": "FAILED", "failure_code": "MODEL_TIMEOUT"},
        ),
    )

    assert result.failure_code is None
    assert result.observation["expected_status"] == "FAILED"
    assert result.observation["expected_failure_code"] == "MODEL_TIMEOUT"
