from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.evaluation.runner import DeterministicEvaluationDriver, RunProgress


@pytest.mark.asyncio
async def test_deterministic_driver_uses_safe_observation_projection() -> None:
    item = SimpleNamespace(
        category="TOOL",
        input={"request": "create a ticket"},
        expected={"tool_identity": "create_ticket", "arguments": {"title": "secret"}},
    )
    variant = SimpleNamespace(variant_hash="v" * 64)
    result = await DeterministicEvaluationDriver().execute(
        None, run=SimpleNamespace(), variant=variant, item=item
    )

    assert result.observation["category"] == "TOOL"
    assert result.observation["tool_identity"] == "create_ticket"
    assert "secret" not in str(result.observation)
    assert "request" not in result.observation


def test_run_progress_is_bounded_and_deterministic() -> None:
    progress = RunProgress(total=8, pending=2, running=1, completed=4, failed=1, cancelled=0)

    assert progress.progress == 0.625
