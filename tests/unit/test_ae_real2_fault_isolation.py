"""AE-Real-2 fault injection must be isolated per AgentRun/repetition."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from benchmarks.evaluation.fault_injection import PerRunFlakyQuery
from packages.tools.errors import ToolHandlerError


@pytest.mark.asyncio
async def test_flaky_query_state_is_isolated_per_request_id() -> None:
    handler = PerRunFlakyQuery(failures_before_success=1)
    arguments = {"customer_ref": "C-AE-REAL-2"}

    first_run = SimpleNamespace(request_id="run-1")
    with pytest.raises(ToolHandlerError):
        await handler(first_run, None, arguments, None)
    assert (await handler(first_run, None, arguments, None))["name"] == "AE Real Fixture"

    second_run = SimpleNamespace(request_id="run-2")
    with pytest.raises(ToolHandlerError):
        await handler(second_run, None, arguments, None)
    assert (await handler(second_run, None, arguments, None))["name"] == "AE Real Fixture"

    assert handler.calls == {"run-1": 2, "run-2": 2}
