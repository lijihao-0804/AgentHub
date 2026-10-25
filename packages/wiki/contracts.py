"""Additive Wiki binding; raw snapshots alone never identify a publication."""

from dataclasses import asdict, dataclass
from decimal import Decimal
from uuid import UUID

from packages.core.canonical.json_hash import canonical_json_hash
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)

COMPILER_VERSION = "wiki-extractive-v1"


def freeze_plan(plan: ResolvedModelExecutionPlan) -> dict:
    def profile(p):
        data = asdict(p)
        for key in (
            "id",
            "workspace_id",
            "provider_credential_id",
            "temperature",
            "timeout_seconds",
        ):
            data[key] = str(data[key])
        return data

    return {
        "primary": profile(plan.primary),
        "fallbacks": [profile(p) for p in plan.fallbacks],
        "retry_policy": asdict(plan.retry_policy),
    }


def thaw_plan(data: dict) -> ResolvedModelExecutionPlan:
    def profile(raw):
        p = dict(raw)
        for key in ("id", "workspace_id", "provider_credential_id"):
            p[key] = UUID(p[key])
        for key in ("temperature", "timeout_seconds"):
            p[key] = Decimal(p[key])
        p["capabilities"] = ModelCapabilities(**p["capabilities"])
        return ResolvedModelExecutionProfile(**p)

    return ResolvedModelExecutionPlan(
        profile(data["primary"]),
        tuple(profile(p) for p in data["fallbacks"]),
        RetryPolicy(**data["retry_policy"]),
    )


@dataclass(frozen=True)
class WikiBinding:
    compilation_id: str
    compilation_hash: str
    source_snapshot_id: str
    source_snapshot_hash: str
    schema_version: int = 1


def page_key(title: str) -> str:
    return canonical_json_hash({"page_key_version": 1, "title": " ".join(title.split()).casefold()})
