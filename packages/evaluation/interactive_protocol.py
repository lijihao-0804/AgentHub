"""Frozen execution protocol for interactive evaluation experiments."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.interactive_lifecycle import (
    INTERACTIVE_DRIVER_VERSION,
    INTERACTIVE_ENVIRONMENT_VERSION,
    INTERACTIVE_LIFECYCLE_VERSION,
    INTERACTIVE_SIMULATOR_VERSION,
)

INTERACTIVE_PROTOCOL_KEY = "interactive"


def interactive_protocol_manifest() -> dict[str, str]:
    """Return a fresh, stable protocol projection for a new experiment."""

    return {
        "driver_version": INTERACTIVE_DRIVER_VERSION,
        "lifecycle_version": INTERACTIVE_LIFECYCLE_VERSION,
        "environment_version": INTERACTIVE_ENVIRONMENT_VERSION,
        "simulator_version": INTERACTIVE_SIMULATOR_VERSION,
        "environment_kind": "support_fixture",
        "recovery_policy": "fail_closed_no_episode_replay",
        "disclosure_policy_binding": "published_dataset_item_hash",
    }


def require_interactive_protocol(manifest: Mapping[str, Any] | None) -> None:
    """Require the exact registered protocol before interactive dispatch."""

    if (
        not isinstance(manifest, Mapping)
        or manifest.get(INTERACTIVE_PROTOCOL_KEY) != interactive_protocol_manifest()
    ):
        raise AgentHubError(
            "INTERACTIVE_PROTOCOL_NOT_FROZEN",
            "The interactive execution protocol is not frozen for this experiment.",
            409,
        )


__all__ = [
    "INTERACTIVE_PROTOCOL_KEY",
    "interactive_protocol_manifest",
    "require_interactive_protocol",
]
