from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.app import create_app
from apps.api.schemas.agents import AgentCreateRequest
from packages.core.config.settings import Settings


def test_m4a_agent_routes_are_registered() -> None:
    app = create_app(Settings(testing=True))
    paths = set(app.openapi()["paths"])

    assert "/api/v1/workspaces/{workspace_id}/agents" in paths
    assert "/api/v1/workspaces/{workspace_id}/agents/{agent_id}/publish" in paths


def test_agent_create_schema_rejects_internal_fields() -> None:
    with pytest.raises(ValidationError):
        AgentCreateRequest(
            name="Agent",
            system_prompt="Prompt",
            model_profile_id=uuid4(),
            workspace_id=uuid4(),
        )
