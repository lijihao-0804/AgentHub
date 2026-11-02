"""Explicit, per-run fault injection for AE-Real-2 evidence tests.

The handler is shared by the runtime, but its failure state must be scoped to one
agent run.  Keying by ``ToolExecutionContext.request_id`` gives each case/repetition
an independent "first call fails, retry succeeds" episode instead of leaking the
failure count across trials.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.tools.errors import ToolHandlerError


class PerRunFlakyQuery:
    def __init__(self, *, failures_before_success: int = 1) -> None:
        self.failures_before_success = max(1, failures_before_success)
        self.calls: dict[str, int] = {}
        self._failed: dict[str, bool] = {}

    async def __call__(
        self,
        context: Any,
        definition: Any,
        arguments: Mapping[str, Any],
        session_factory: Any,
    ) -> dict[str, Any]:
        del definition, session_factory
        key = str(
            getattr(context, "request_id", None)
            or getattr(context, "tool_call_id", "unknown")
        )
        self.calls[key] = self.calls.get(key, 0) + 1
        if not self._failed.get(key, False):
            self._failed[key] = True
            raise ToolHandlerError("SIMULATED_TOOL_FAILURE", "Injected first-call failure.")
        # A successful retry closes this failure episode.  If a runtime reuses the
        # same request id across repetitions, the next call starts a new episode.
        self._failed[key] = False
        return {
            "customer_ref": arguments.get("customer_ref"),
            "name": "AE Real Fixture",
            "email": "ae-real@example.test",
        }
