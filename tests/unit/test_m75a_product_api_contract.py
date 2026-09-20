from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.app import create_app
from apps.api.schemas.product_control_plane import (
    ProviderCredentialCreateRequest,
    ToolCreateRequest,
)
from packages.control_plane.product_control_plane import BUILTIN_TOOL_CATALOG
from packages.core.config.settings import Settings


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
