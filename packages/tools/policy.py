"""M4-B policy boundary: only low-risk approval-free reads auto-execute."""

from __future__ import annotations

from enum import StrEnum

from packages.tools.contracts import ToolApprovalPolicy, ToolDefinition, ToolEffect


class ToolPolicyDecision(StrEnum):
    ALLOW_AUTO = "ALLOW_AUTO"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ToolPolicy:
    @staticmethod
    def decide(definition: ToolDefinition) -> ToolPolicyDecision:
        if (
            definition.effect is ToolEffect.READ
            and definition.approval_policy is ToolApprovalPolicy.NEVER
        ):
            return ToolPolicyDecision.ALLOW_AUTO
        return ToolPolicyDecision.REQUIRE_APPROVAL


__all__ = ["ToolPolicy", "ToolPolicyDecision"]
