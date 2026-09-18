from __future__ import annotations

import pytest

from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
)
from packages.tools.policy import ToolPolicy, ToolPolicyDecision
from packages.tools.runtime import validate_executable_tool_spec


def _definition(
    *, effect: ToolEffect = ToolEffect.READ, approval: ToolApprovalPolicy = ToolApprovalPolicy.NEVER
) -> ToolDefinition:
    from uuid import uuid4

    return ToolDefinition(
        identity="calculator",
        revision_id=uuid4(),
        spec_hash="a" * 64,
        description="",
        input_schema={"type": "object"},
        effect=effect,
        risk_level=ToolRisk.LOW,
        approval_policy=approval,
        timeout_seconds=30,
    )


def test_only_approval_free_reads_auto_execute() -> None:
    assert ToolPolicy.decide(_definition()) is ToolPolicyDecision.ALLOW_AUTO
    assert (
        ToolPolicy.decide(_definition(approval=ToolApprovalPolicy.ALWAYS))
        is ToolPolicyDecision.REQUIRE_APPROVAL
    )
    assert (
        ToolPolicy.decide(_definition(effect=ToolEffect.WRITE))
        is ToolPolicyDecision.REQUIRE_APPROVAL
    )


def test_old_revision_defaults_description_and_timeout() -> None:
    normalized = validate_executable_tool_spec(
        {
            "kind": "builtin",
            "identity": "calculator",
            "input_schema": {"type": "object"},
            "effect": "READ",
            "risk_level": "LOW",
            "approval_policy": "NEVER",
        }
    )
    assert normalized["description"] == ""
    assert normalized["timeout_seconds"] == 30


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "remote", "identity": "calculator"},
        {"kind": "builtin", "identity": "calculator"},
        {
            "kind": "builtin",
            "identity": "calculator",
            "input_schema": {"type": "object"},
            "effect": "WRITE",
            "risk_level": "LOW",
        },
        {
            "kind": "builtin",
            "identity": "calculator",
            "input_schema": {"type": "object"},
            "effect": "READ",
            "risk_level": "LOW",
            "approval_policy": "NEVER",
            "timeout_seconds": 121,
        },
    ],
)
def test_invalid_executable_revision_is_rejected(spec: dict[str, object]) -> None:
    with pytest.raises(Exception) as error:
        validate_executable_tool_spec(spec)
    assert error.value.code == "TOOL_REVISION_INVALID"
