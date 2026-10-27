"""Reproducible offline budget for the declared 32K Aliyun trial route."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from benchmarks.evaluation.memory_trial_client import MODEL
from packages.agent_runtime.context_budget import ContextBudgetConfig, ContextBudgetPolicy
from packages.agent_runtime.context_policy import resolve_context_policy
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
)


def create_trial_policy() -> ContextBudgetPolicy:
    """Freeze the existing 32K declaration; never infer a larger provider window."""
    profile = ResolvedModelExecutionProfile(
        id=UUID(int=1),
        workspace_id=UUID(int=2),
        provider_credential_id=UUID(int=3),
        provider="openai-compatible",
        model=MODEL,
        temperature=Decimal(0),
        max_tokens=2048,
        timeout_seconds=Decimal(60),
        capabilities=ModelCapabilities(
            tool_calling=True,
            streaming=True,
            structured_output=False,
            vision=False,
            max_context_tokens=32768,
        ),
    )
    frozen = resolve_context_policy(
        {"policy_version": "adaptive_context_v1", "preset": "GENERAL_64K"},
        [
            {
                "profile_id": str(profile.id),
                "credential_ref": str(profile.provider_credential_id),
                "provider": profile.provider,
                "model": profile.model,
                "max_tokens": profile.max_tokens,
                "capabilities": {
                    "tool_calling": True,
                    "streaming": True,
                    "structured_output": False,
                    "vision": False,
                    "max_context_tokens": 32768,
                },
            }
        ],
        reserved_output_tokens=2048,
    )
    spec = SimpleNamespace(
        runtime={"context_policy": frozen},
        model_plan=ResolvedModelExecutionPlan(primary=profile),
    )
    return ContextBudgetPolicy(spec, ContextBudgetConfig(reserved_output_tokens=2048))
