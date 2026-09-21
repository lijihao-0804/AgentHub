"""Canonical M7-B reproducibility projections and hashes."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.judge import (
    JUDGE_DISABLED_VERSION,
    JUDGE_MANIFEST_KEY,
    FrozenJudgeProfile,
    judge_manifest_entry,
)
from packages.evaluation.models import PricingSnapshot

EVALUATION_SCHEMA_VERSION = 1
DEFAULT_EVALUATOR_VERSIONS: dict[str, str] = {
    JUDGE_MANIFEST_KEY: JUDGE_DISABLED_VERSION,
    "approval-evaluator": "v1",
    "citation-evaluator": "v1",
    "dataset-validator": "v1",
    "failure-evaluator": "v1",
    "retrieval-evaluator": "v1",
    "tool-evaluator": "v1",
}


def default_evaluator_manifest(
    judge_profile: FrozenJudgeProfile | None = None,
) -> dict[str, Any]:
    """Freeze the evaluator identities for one experiment.

    The judge, when one is configured, is frozen here alongside the deterministic
    evaluators: its provider/model/parameter projection is reduced to an evaluator
    version, so the experiment can never be re-scored by a different judge model
    without the manifest -- and every metric row derived from it -- changing.
    """
    versions = dict(DEFAULT_EVALUATOR_VERSIONS)
    versions[JUDGE_MANIFEST_KEY] = judge_manifest_entry(judge_profile)
    manifest: dict[str, Any] = {
        "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluator_versions": dict(sorted(versions.items())),
    }
    if judge_profile is not None:
        # The non-secret projection is kept beside the version so the worker can
        # rebuild exactly the judge the experiment was frozen with, instead of
        # re-resolving a profile that may have been edited since.  The key is
        # absent when there is no judge, so manifests written before the judge
        # existed -- and their hashes -- are untouched.
        manifest["judge_profile"] = judge_profile.projection()
    return manifest


def frozen_judge_version(manifest: Mapping[str, Any] | None) -> str:
    versions = manifest.get("evaluator_versions") if isinstance(manifest, Mapping) else None
    if not isinstance(versions, Mapping):
        return JUDGE_DISABLED_VERSION
    return str(versions.get(JUDGE_MANIFEST_KEY, JUDGE_DISABLED_VERSION))


def normalize_knowledge_snapshots(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise AgentHubError(
            "EVALUATION_KNOWLEDGE_INVALID",
            "Effective knowledge snapshots are invalid.",
            422,
        )
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise AgentHubError(
                "EVALUATION_KNOWLEDGE_INVALID",
                "Effective knowledge snapshots are invalid.",
                422,
            )
        required = {"knowledge_base_id", "binding_mode", "snapshot_id", "snapshot_content_hash"}
        if set(item) != required or item["binding_mode"] not in {"PINNED", "LATEST"}:
            raise AgentHubError(
                "EVALUATION_KNOWLEDGE_INVALID",
                "Effective knowledge snapshots are invalid.",
                422,
            )
        if not all(isinstance(item[key], str) and item[key].strip() for key in required):
            raise AgentHubError(
                "EVALUATION_KNOWLEDGE_INVALID",
                "Effective knowledge snapshots are invalid.",
                422,
            )
        normalized.append(
            {
                "knowledge_base_id": item["knowledge_base_id"],
                "binding_mode": item["binding_mode"],
                "snapshot_id": item["snapshot_id"],
                "snapshot_content_hash": item["snapshot_content_hash"],
            }
        )
    return sorted(
        normalized,
        key=lambda item: (item["knowledge_base_id"], item["snapshot_id"], item["binding_mode"]),
    )


def pricing_snapshot_content_hash(snapshot: PricingSnapshot) -> str:
    return canonical_json_hash(
        {
            "name": snapshot.name,
            "provider": snapshot.provider,
            "model": snapshot.model,
            "currency": snapshot.currency,
            "input_price_per_1m": _decimal_text(snapshot.input_price_per_1m),
            "output_price_per_1m": _decimal_text(snapshot.output_price_per_1m),
            "cached_input_price_per_1m": (
                _decimal_text(snapshot.cached_input_price_per_1m)
                if snapshot.cached_input_price_per_1m is not None
                else None
            ),
            "effective_at": snapshot.effective_at.isoformat(),
            "source_note": snapshot.source_note,
        }
    )


def variant_hash(
    *,
    agent_version_id: str,
    resolved_spec_hash: str,
    effective_knowledge_snapshots: list[dict[str, str]],
    pricing_snapshot_id: str,
    pricing_snapshot_hash: str,
    variant_metadata: Mapping[str, Any],
) -> str:
    return canonical_json_hash(
        {
            "agent_version_id": agent_version_id,
            "resolved_spec_hash": resolved_spec_hash,
            "effective_knowledge_snapshots": normalize_knowledge_snapshots(
                effective_knowledge_snapshots
            ),
            "pricing_snapshot_id": pricing_snapshot_id,
            "pricing_snapshot_hash": pricing_snapshot_hash,
            "variant_metadata": dict(variant_metadata),
        }
    )


def experiment_spec_hash(spec: Mapping[str, Any]) -> str:
    return canonical_json_hash(dict(spec))


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


__all__ = [
    "DEFAULT_EVALUATOR_VERSIONS",
    "EVALUATION_SCHEMA_VERSION",
    "default_evaluator_manifest",
    "experiment_spec_hash",
    "frozen_judge_version",
    "normalize_knowledge_snapshots",
    "pricing_snapshot_content_hash",
    "variant_hash",
]
