"""Context runtime authorization failures do not depend on summary policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelMessage
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from tests.unit.test_runtime_rag_evidence_projection import _frozen_spec, _model_graph

_ERROR_MAPPINGS = (
    ("FORBIDDEN", "RUN_ACTOR_ACCESS_REVOKED", True),
    ("RUN_ACTOR_ACCESS_REVOKED", "RUN_ACTOR_ACCESS_REVOKED", True),
    ("RUN_EXECUTION_LEASE_LOST", "RUN_EXECUTION_LEASE_LOST", True),
    ("UNKNOWN_OUTCOME", "UNKNOWN_OUTCOME", True),
    ("ACTION_OUTCOME_UNKNOWN", "ACTION_OUTCOME_UNKNOWN", True),
    ("ACTION_RECONCILIATION_REQUIRED", "ACTION_RECONCILIATION_REQUIRED", True),
    ("AGENT_HISTORY_SNAPSHOT_INVALID", "AGENT_HISTORY_SNAPSHOT_INVALID", False),
)


def _state(graph, *, summary_enabled: bool) -> dict:
    return {
        "messages": [
            ModelMessage("system", "runtime"),
            ModelMessage("system", "system"),
            ModelMessage("user", "question"),
        ],
        "spec": _frozen_spec(20_000),
        "runtime": {
            "max_steps": 3,
            "context_budget": graph._runtime_context_budget,
            "summary_policy": {"enabled": summary_enabled},
        },
        "tool_definitions": {},
        "model_round_count": 0,
        "usage_records": [],
    }


def _bind_live_summary_lease(graph) -> None:
    owner = "test-execution-owner"
    run_row = SimpleNamespace(
        status="RUNNING",
        execution_owner=owner,
        execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    class Session:
        async def scalar(self, _statement):
            return run_row

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_exc_info):
            return None

    graph.execution_owner_token = owner
    graph.service.session_factory = lambda: SessionContext()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ("admission", "provider"))
@pytest.mark.parametrize("summary_enabled", (False, True), ids=("summary_off", "summary_on"))
@pytest.mark.parametrize(
    ("error_code", "expected_failure_code", "needs_attention"),
    _ERROR_MAPPINGS,
)
async def test_model_context_failures_map_independently_of_summary_policy(
    path: str,
    summary_enabled: bool,
    error_code: str,
    expected_failure_code: str,
    needs_attention: bool,
) -> None:
    graph = _model_graph(request_sink=[])
    _bind_live_summary_lease(graph)
    if path == "admission":

        def reject_context(_state):
            raise AgentHubError(error_code, "context rejected", 403)

        graph._admit_context = reject_context
    else:

        class RaisingGateway:
            async def generate_resolved(self, _context, _plan, _request):
                raise AgentHubError(error_code, "provider rejected", 403)

        graph.service.model_gateway_factory = lambda _session: RaisingGateway()

    result = await graph.model(_state(graph, summary_enabled=summary_enabled))

    assert result["failure_code"] == expected_failure_code
    if needs_attention:
        assert result["run_status"] == "NEEDS_ATTENTION"
    else:
        assert "run_status" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_enabled", (False, True), ids=("summary_off", "summary_on"))
async def test_provider_failure_remains_an_ordinary_model_failure(
    summary_enabled: bool,
) -> None:
    graph = _model_graph(request_sink=[])

    class FailingGateway:
        async def generate_resolved(self, _context, _plan, _request):
            raise ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT)

    _bind_live_summary_lease(graph)
    graph.service.model_gateway_factory = lambda _session: FailingGateway()

    result = await graph.model(_state(graph, summary_enabled=summary_enabled))

    assert result["failure_code"] == "MODEL_TIMEOUT"
    assert "run_status" not in result
