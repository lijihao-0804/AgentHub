from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.app import create_app
from apps.api.schemas.product_control_plane import (
    ProviderCredentialCreateRequest,
    ToolCreateRequest,
    ToolResponse,
)
from packages.agent_runtime.models import Tool, ToolRevision
from packages.control_plane.product_control_plane import BUILTIN_TOOL_CATALOG, BuiltinToolCatalog
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError


def test_m75a_routes_are_registered_without_frontend_dependencies() -> None:
    paths = create_app(Settings(testing=True)).openapi()["paths"]

    assert "/api/v1/workspaces/{workspace_id}/provider-credentials" in paths
    assert "/api/v1/workspaces/{workspace_id}/model-profiles" in paths
    assert "/api/v1/workspaces/{workspace_id}/tool-catalog" in paths
    assert "/api/v1/workspaces/{workspace_id}/tools/{tool_id}/revisions" in paths
    assert "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/knowledge-bindings" in paths
    assert "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/tool-bindings" in paths
    assert (
        "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents"
        in paths
    )
    assert (
        "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/snapshots/{snapshot_id}"
        in paths
    )


def test_product_requests_reject_server_owned_fields() -> None:
    with pytest.raises(ValidationError):
        ProviderCredentialCreateRequest(
            provider="deepseek",
            name="primary",
            secret="secret",
            workspace_id=uuid4(),
            secret_ciphertext="must-not-be-client-controlled",
        )
    with pytest.raises(ValidationError):
        ToolCreateRequest(builtin_identity="calculator", handler_path="arbitrary.module:call")


def test_builtin_catalog_is_server_owned_and_contains_only_runtime_identities() -> None:
    assert set(BUILTIN_TOOL_CATALOG) == {
        "calculator",
        "query_customer",
        "search_knowledge",
        "create_ticket",
    }
    assert BUILTIN_TOOL_CATALOG["create_ticket"]["effect"] == "WRITE"
    assert BUILTIN_TOOL_CATALOG["create_ticket"]["approval_policy"] == "ALWAYS"
    assert BUILTIN_TOOL_CATALOG["create_ticket"]["execution_kind"] == "action"
    for entry in BUILTIN_TOOL_CATALOG.values():
        assert "handler" not in entry
        assert "url" not in entry
        assert "command" not in entry


def test_tool_projection_exposes_validated_governance_and_fails_closed() -> None:
    from packages.control_plane.product_control_plane import ProductControlPlaneService

    tool = Tool(
        id=uuid4(),
        workspace_id=uuid4(),
        name="renamed-calculator",
        description="Managed calculator",
        enabled=True,
        created_at=datetime.now(UTC),
    )
    spec = BuiltinToolCatalog.spec("calculator")
    revision = ToolRevision(
        id=uuid4(),
        workspace_id=tool.workspace_id,
        tool_id=tool.id,
        revision_number=1,
        spec=spec,
        spec_hash=canonical_json_hash(spec),
        created_at=datetime.now(UTC),
        created_by=uuid4(),
    )

    response = ToolResponse.model_validate(
        ProductControlPlaneService._tool_projection(tool, revision)
    )
    assert response.identity == "calculator"
    assert response.effect == "READ"
    assert response.risk_level == "LOW"
    assert response.approval_policy == "NEVER"
    assert response.execution_kind == "builtin"
    assert response.current_revision_number == 1
    assert response.current_spec_hash == revision.spec_hash

    bad_revision = ToolRevision(
        id=revision.id,
        workspace_id=revision.workspace_id,
        tool_id=revision.tool_id,
        revision_number=revision.revision_number,
        spec={**spec, "effect": "WRITE"},
        spec_hash=revision.spec_hash,
        created_at=revision.created_at,
        created_by=revision.created_by,
    )
    with pytest.raises(AgentHubError) as raised:
        ProductControlPlaneService._tool_projection(tool, bad_revision)
    assert raised.value.code == "TOOL_REVISION_INVALID"

    no_revision = ToolResponse.model_validate(
        ProductControlPlaneService._tool_projection(tool, None)
    )
    assert no_revision.identity is None
    assert no_revision.effect is None
    assert no_revision.execution_kind is None
