from decimal import Decimal
from uuid import uuid4

import pytest

from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.capabilities import validate_capabilities
from packages.model_gateway.contracts import CapabilityRequirements
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.model_gateway.profile_resolution import _to_resolved


def make_context(workspace_id) -> WorkspaceExecutionContext:
    principal = PrincipalContext(request_id="m2", trace_id="m2", user_id=str(uuid4()))
    organization = OrganizationContext(
        principal=principal,
        organization_id=str(uuid4()),
        org_role="OWNER",
    )
    return WorkspaceExecutionContext(
        organization=organization,
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
    )


def test_model_gateway_tables_contain_required_fields_without_invocation_table() -> None:
    assert {
        "workspace_id",
        "provider",
        "name",
        "secret",
        "base_url",
        "enabled",
        "created_at",
    }.issubset(ProviderCredential.__table__.columns.keys())
    assert {
        "workspace_id",
        "provider_credential_id",
        "model",
        "temperature",
        "max_tokens",
        "timeout_seconds",
        "fallback_profile_id",
        "capabilities",
        "enabled",
        "created_at",
    }.issubset(ModelProfile.__table__.columns.keys())
    assert "model_invocations" not in ModelProfile.metadata.tables


def test_capability_validation_keeps_numeric_context_check_strict() -> None:
    validate_capabilities(
        {
            "streaming": True,
            "max_context_tokens": 8192,
        },
        CapabilityRequirements(required={"streaming"}, max_context_tokens=4096),
    )
    with pytest.raises(ModelGatewayError) as mismatch:
        validate_capabilities(
            {"streaming": True, "max_context_tokens": 1024},
            CapabilityRequirements(max_context_tokens=4096),
        )
    assert mismatch.value.code == ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH


def test_profile_resolution_does_not_copy_credential_secret() -> None:
    credential = ProviderCredential(
        id=uuid4(),
        workspace_id=uuid4(),
        provider="deepseek",
        name="primary",
        secret="must-not-cross-profile-boundary",
    )
    profile = ModelProfile(
        id=uuid4(),
        workspace_id=credential.workspace_id,
        provider_credential_id=credential.id,
        model="deepseek-chat",
        temperature=Decimal("0"),
        max_tokens=1024,
        timeout_seconds=Decimal("30"),
        capabilities={"streaming": True, "max_context_tokens": 8192},
    )

    resolved = _to_resolved(profile)

    assert not hasattr(resolved, "secret")
    assert "must-not-cross-profile-boundary" not in repr(resolved)


def test_context_is_workspace_specific() -> None:
    first = make_context(uuid4())
    second = make_context(uuid4())

    assert first.workspace_id != second.workspace_id
