from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.app import create_app
from apps.api.schemas.agents import AgentPreflightResponse
from apps.api.schemas.product_control_plane import ModelProfileTestResponse
from packages.control_plane.product_control_plane import ProductControlPlaneService
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import ModelHealthResult, ModelHealthStatus
from packages.model_gateway.models import ModelProfile


class _ScalarSession:
    def __init__(self, profile: ModelProfile | None) -> None:
        self.profile = profile

    async def scalar(self, _statement):
        return self.profile


class _HealthGateway:
    def __init__(self, result: ModelHealthResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def health(self, _context, profile_id):
        self.calls.append(str(profile_id))
        return self.result


def _context(workspace_id, permissions: frozenset[str]) -> WorkspaceExecutionContext:
    principal = PrincipalContext(
        request_id="m75b-unit",
        trace_id="m75b-unit",
        user_id=str(uuid4()),
    )
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=principal,
            organization_id=str(uuid4()),
            org_role="MEMBER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=permissions,
    )


def _profile(workspace_id) -> ModelProfile:
    return ModelProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=uuid4(),
        model="controlled-test-model",
        temperature=Decimal("0"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        capabilities={"max_context_tokens": 8192},
        enabled=True,
    )


def test_m75b_routes_and_response_contracts_are_registered() -> None:
    paths = create_app(Settings(testing=True)).openapi()["paths"]

    assert (
        "/api/v1/workspaces/{workspace_id}/model-profiles/{profile_id}/test" in paths
    )
    assert "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/preflight" in paths
    assert "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/versions/{version_id}" in paths

    response = ModelProfileTestResponse(
        model_profile_id=uuid4(),
        status="degraded",
        failure_code="MODEL_RATE_LIMITED",
        latency_ms=0,
    )
    assert response.status == "degraded"
    with pytest.raises(ValidationError):
        ModelProfileTestResponse(
            model_profile_id=response.model_profile_id,
            status="healthy",
            failure_code=None,
            latency_ms=1,
            secret="must-not-cross-boundary",
        )

    preflight = AgentPreflightResponse(
        status="READY",
        agent_id=uuid4(),
        workspace_id=uuid4(),
        draft_updated_at=datetime.now(UTC),
        spec_schema_version=2,
        resolved_spec_hash="a" * 64,
        resolved_spec={"spec_schema_version": 2},
    )
    assert preflight.status == "READY"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "failure_code"),
    [
        (ModelHealthStatus.HEALTHY, None),
        (ModelHealthStatus.DEGRADED, "MODEL_RATE_LIMITED"),
        (ModelHealthStatus.UNAVAILABLE, "MODEL_PROVIDER_UNAVAILABLE"),
    ],
)
async def test_model_profile_test_preserves_gateway_health_semantics(status, failure_code) -> None:
    workspace_id = uuid4()
    profile = _profile(workspace_id)
    gateway = _HealthGateway(ModelHealthResult(status=status, failure_code=failure_code))
    result = await ProductControlPlaneService().test_model_profile(
        _ScalarSession(profile),
        _context(workspace_id, frozenset({"agent_edit"})),
        profile.id,
        gateway,
    )

    assert result["model_profile_id"] == profile.id
    assert result["status"] == status.value
    assert result["failure_code"] == failure_code
    assert result["latency_ms"] >= 0
    assert gateway.calls == [str(profile.id)]
    serialized = ModelProfileTestResponse.model_validate(result).model_dump_json()
    assert "secret" not in serialized.lower()
    assert "authorization" not in serialized.lower()


@pytest.mark.asyncio
async def test_model_profile_test_enforces_agent_edit_and_workspace_scope() -> None:
    workspace_a = uuid4()
    workspace_b = uuid4()
    profile_b = _profile(workspace_b)
    gateway = _HealthGateway(ModelHealthResult(ModelHealthStatus.HEALTHY))
    service = ProductControlPlaneService()

    with pytest.raises(AgentHubError) as viewer_error:
        await service.test_model_profile(
            _ScalarSession(profile_b),
            _context(workspace_a, frozenset({"workspace_read"})),
            profile_b.id,
            gateway,
        )
    assert viewer_error.value.code == "FORBIDDEN"
    assert gateway.calls == []

    with pytest.raises(AgentHubError) as cross_workspace_error:
        await service.test_model_profile(
            _ScalarSession(None),
            _context(workspace_a, frozenset({"agent_edit"})),
            profile_b.id,
            gateway,
        )
    assert cross_workspace_error.value.code == "MODEL_PROFILE_NOT_FOUND"
    assert gateway.calls == []
