"""M4-B policy boundary: only low-risk approval-free reads auto-execute."""

from __future__ import annotations

from enum import StrEnum

from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
    ToolSourceKind,
)


class ToolPolicyDecision(StrEnum):
    ALLOW_AUTO = "ALLOW_AUTO"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ToolPolicy:
    @staticmethod
    def requires_human(definition: ToolDefinition) -> bool:
        return definition.risk_level is ToolRisk.HIGH or (
            definition.source_kind is ToolSourceKind.MCP and definition.effect is ToolEffect.WRITE
        )

    @staticmethod
    def decide(definition: ToolDefinition) -> ToolPolicyDecision:
        if (
            definition.effect is ToolEffect.READ
            and definition.approval_policy is ToolApprovalPolicy.NEVER
            and not ToolPolicy.requires_human(definition)
        ):
            return ToolPolicyDecision.ALLOW_AUTO
        return ToolPolicyDecision.REQUIRE_APPROVAL


__all__ = ["ToolPolicy", "ToolPolicyDecision"]
