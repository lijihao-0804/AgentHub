"""Enhancement 3A: the API contract of remote MCP connections.

These tests pin the promises a caller is entitled to rely on — what may be
sent, what comes back, and what can never come back — without a database or a
network. The secret assertions are deliberately written against the declared
models rather than against one sampled response, so a field added later is
caught by construction rather than by whether a test happened to exercise it.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.routes.mcp_connections import PREFIX, router
from apps.api.schemas.mcp_connections import (
    McpConnectionCreateRequest,
    McpConnectionDiscoveryResponse,
    McpConnectionPatchRequest,
    McpConnectionResponse,
    McpConnectionRotateSecretRequest,
    McpConnectionTestResponse,
    McpDiscoveredToolResponse,
)
from packages.control_plane.rbac import (
    DEVELOPER_PERMISSIONS,
    VIEWER_PERMISSIONS,
    WORKSPACE_ADMIN_PERMISSIONS,
)
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.mcp.service import TOOL_EDIT, McpConnectionService

SECRET_FIELD_NAMES = {
    "secret",
    "encrypted_secret",
    "secret_ciphertext",
    "secret_version",
    "authorization",
    "headers",
    "token",
    "bearer_token",
}

RESPONSE_MODELS = [
    McpConnectionResponse,
    McpConnectionTestResponse,
    McpConnectionDiscoveryResponse,
    McpDiscoveredToolResponse,
]


def context(permissions: frozenset[str]) -> WorkspaceExecutionContext:
    principal = PrincipalContext(request_id="r", trace_id="t", user_id=str(uuid4()))
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=principal, organization_id=str(uuid4()), org_role="OWNER"
        ),
        workspace_id=str(uuid4()),
        permissions=permissions,
    )


def service() -> McpConnectionService:
    return McpConnectionService(
        settings=Settings(
            testing=True, environment="test", credential_master_key="unit-test-master-key"
        )
    )


class ExplodingSession:
    """Stands in for a session that must never be reached.

    Every validation these tests cover happens before persistence. If any of
    them stops running first, the failure is loud rather than a silent write.
    """

    def add(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError("the request reached the database despite being invalid")

    async def execute(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("the request reached the database despite being invalid")

    async def scalar(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("the request reached the database despite being invalid")


async def create(ctx: WorkspaceExecutionContext, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "name": "primary",
        "endpoint_url": "https://mcp.example.com/mcp",
        "auth_type": "NONE",
        "secret": None,
        "enabled": True,
    }
    payload.update(overrides)
    await service().create_connection(ExplodingSession(), ctx, **payload)


# --- request contract -------------------------------------------------------


def test_create_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        McpConnectionCreateRequest(
            name="primary",
            endpoint_url="https://mcp.example.com/mcp",
            auth_type="NONE",
            headers={"Authorization": "Bearer smuggled"},
        )


@pytest.mark.parametrize(
    "model",
    [McpConnectionCreateRequest, McpConnectionPatchRequest, McpConnectionRotateSecretRequest],
)
def test_every_request_model_forbids_extras(model: type) -> None:
    assert model.model_config.get("extra") == "forbid"


def test_create_request_bounds_its_strings() -> None:
    with pytest.raises(ValidationError):
        McpConnectionCreateRequest(name="x" * 129, endpoint_url="https://mcp.example.com/mcp")
    with pytest.raises(ValidationError):
        McpConnectionCreateRequest(name="primary", endpoint_url="https://e.com/" + "a" * 4000)


def test_create_request_rejects_an_unsupported_auth_type() -> None:
    with pytest.raises(ValidationError):
        McpConnectionCreateRequest(
            name="primary", endpoint_url="https://mcp.example.com/mcp", auth_type="OAUTH"
        )


# --- auth-type validation ---------------------------------------------------


@pytest.mark.asyncio
async def test_none_auth_may_not_be_given_a_secret() -> None:
    with pytest.raises(AgentHubError) as excinfo:
        await create(context(WORKSPACE_ADMIN_PERMISSIONS), auth_type="NONE", secret="tok-abc")

    assert excinfo.value.status_code == 422
    assert "tok-abc" not in excinfo.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [None, "", "   "])
async def test_bearer_auth_requires_a_non_empty_secret(secret: str | None) -> None:
    with pytest.raises(AgentHubError) as excinfo:
        await create(context(WORKSPACE_ADMIN_PERMISSIONS), auth_type="BEARER", secret=secret)

    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint", ["file:///etc/passwd", "ftp://mcp.example.com/mcp", "https://u:p@mcp.example.com/m"]
)
async def test_an_unusable_endpoint_is_refused_before_a_row_exists(endpoint: str) -> None:
    with pytest.raises(AgentHubError) as excinfo:
        await create(
            context(WORKSPACE_ADMIN_PERMISSIONS),
            auth_type="BEARER",
            secret="tok-abc",
            endpoint_url=endpoint,
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "MCP_ENDPOINT_INVALID"


# --- response safety --------------------------------------------------------


@pytest.mark.parametrize("model", RESPONSE_MODELS)
def test_no_response_model_can_carry_a_secret(model: type) -> None:
    assert set(model.model_fields) & SECRET_FIELD_NAMES == set()
    assert model.model_config.get("extra") == "forbid"


def test_the_response_reports_only_whether_a_secret_exists() -> None:
    field = McpConnectionResponse.model_fields["secret_configured"]

    assert field.annotation is bool

    with pytest.raises(ValidationError):
        # Not "v1:..." , not a hint, not a prefix — a boolean or nothing.
        McpConnectionResponse.model_validate(
            {
                "id": uuid4(),
                "workspace_id": uuid4(),
                "name": "primary",
                "endpoint_url": "https://mcp.example.com/mcp",
                "auth_type": "BEARER",
                "secret_configured": "v1:gAAAAA",
                "enabled": True,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )


def test_a_discovered_tool_carries_no_governance_verdict() -> None:
    """3A reports the remote's catalog; it does not grade it."""

    for governance_field in ("effect", "risk_level", "approval_policy", "execution_kind"):
        assert governance_field not in McpDiscoveredToolResponse.model_fields


# --- immutability -----------------------------------------------------------


@pytest.mark.parametrize("field", ["endpoint_url", "auth_type"])
def test_patch_cannot_repoint_a_connection(field: str) -> None:
    assert field not in McpConnectionPatchRequest.model_fields

    with pytest.raises(ValidationError):
        McpConnectionPatchRequest(**{field: "https://evil.example.com/mcp"})


def test_patch_accepts_exactly_the_two_mutable_fields() -> None:
    assert set(McpConnectionPatchRequest.model_fields) == {"name", "enabled"}


@pytest.mark.asyncio
async def test_the_service_refuses_an_immutable_field_even_if_a_caller_gets_past_the_schema() -> (
    None
):
    with pytest.raises(AgentHubError) as excinfo:
        await service().patch_connection(
            ExplodingSession(),
            context(WORKSPACE_ADMIN_PERMISSIONS),
            uuid4(),
            {"endpoint_url": "https://evil.example.com/mcp"},
        )

    assert excinfo.value.status_code == 422


# --- routing and permissions ------------------------------------------------


def test_connections_are_not_exposed_as_tools() -> None:
    paths = {route.path for route in router.routes}

    assert PREFIX == "/api/v1/workspaces/{workspace_id}/mcp-connections"
    assert paths == {
        PREFIX,
        PREFIX + "/{connection_id}",
        PREFIX + "/{connection_id}/rotate-secret",
        PREFIX + "/{connection_id}/test",
        PREFIX + "/{connection_id}/discover-tools",
        PREFIX + "/{connection_id}/import-tool",
    }
    assert not any("/tools" in path.removeprefix(PREFIX) for path in paths)


def test_the_permissions_used_are_the_ones_that_already_exist() -> None:
    """3A introduces no new RBAC permission, only new uses of existing ones."""

    assert TOOL_EDIT == "tool_edit"
    assert TOOL_EDIT in DEVELOPER_PERMISSIONS
    assert TOOL_EDIT in WORKSPACE_ADMIN_PERMISSIONS
    assert TOOL_EDIT not in VIEWER_PERMISSIONS


@pytest.mark.asyncio
async def test_a_viewer_cannot_create_a_connection() -> None:
    with pytest.raises(AgentHubError) as excinfo:
        await create(context(VIEWER_PERMISSIONS), auth_type="NONE", secret=None)

    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_a_developer_cannot_touch_the_credential() -> None:
    """Testing a connection is development; holding its token is administration."""

    with pytest.raises(AgentHubError) as excinfo:
        await service().rotate_secret(
            ExplodingSession(), context(DEVELOPER_PERMISSIONS), uuid4(), "tok-new"
        )

    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_a_viewer_cannot_reach_the_remote() -> None:
    for operation in (service().test_connection, service().discover_tools):
        with pytest.raises(AgentHubError) as excinfo:
            await operation(ExplodingSession(), context(VIEWER_PERMISSIONS), uuid4())
        assert excinfo.value.status_code == 403
