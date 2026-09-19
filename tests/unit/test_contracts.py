from decimal import Decimal
from math import inf, nan

import pytest
from pydantic import ValidationError

from packages.core.canonical.json_hash import canonical_json, canonical_json_hash
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)


def test_canonical_json_is_order_independent_and_hashable() -> None:
    left = {"b": 2, "a": {"enabled": True, "value": None}}
    right = {"a": {"value": None, "enabled": True}, "b": 2}

    assert canonical_json(left) == canonical_json(right)
    assert canonical_json_hash(left) == canonical_json_hash(right)


def test_canonical_json_rejects_non_json_numeric_edges() -> None:
    with pytest.raises(TypeError):
        canonical_json({"value": Decimal("0.1")})
    with pytest.raises(ValueError):
        canonical_json({"value": nan})
    with pytest.raises(ValueError):
        canonical_json({"value": inf})


def test_workspace_context_requires_explicit_scopes() -> None:
    principal = PrincipalContext(request_id="req-1", trace_id="trace-1", user_id="user-1")
    organization = OrganizationContext(
        principal=principal,
        organization_id="org-1",
        org_role="MEMBER",
    )
    context = WorkspaceExecutionContext(
        organization=organization,
        workspace_id="workspace-1",
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent:run"}),
    )

    assert context.request_id == "req-1"
    assert context.user_id == "user-1"
    with pytest.raises(ValidationError):
        WorkspaceExecutionContext.model_validate(
            {
                "organization": organization,
                "workspace_id": "workspace-1",
                "workspace_role": "DEVELOPER",
                "organization_id": "client-controlled",
            }
        )
