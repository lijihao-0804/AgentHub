"""JSON-safe retained execution evidence, separate from cleanup state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from typing import Any
from uuid import UUID


def _json_value(value: Any) -> Any:
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("INTERACTIVE_OBSERVATION_NOT_JSON_SAFE")


def observation_envelope(result: Any) -> dict[str, Any]:
    return _json_value(asdict(result))


def with_cleanup(envelope: dict[str, Any], status: str, error: str | None = None) -> dict[str, Any]:
    result = deepcopy(envelope)
    observation = result["observation"]
    observation["environment_lifecycle_status"] = status
    observation["execution_complete"] = status == "TEARDOWN_COMPLETE"
    environment = observation.setdefault("environment", {})
    environment["teardown_status"] = status
    environment["teardown_error"] = error
    if status != "TEARDOWN_COMPLETE":
        observation["cleanup_failure_reason"] = (
            "environment_cleanup_blocked"
            if status == "CLEANUP_BLOCKED"
            else "environment_teardown_failed"
        )
    return result


def observation_from_envelope(envelope: dict[str, Any]):
    # Import at the application boundary to keep lifecycle persistence independent
    # of runner composition and avoid a circular model/driver dependency.
    from packages.evaluation.runner import CaseExecutionObservation

    values = deepcopy(envelope)
    if values.get("agent_run_id") is not None:
        values["agent_run_id"] = UUID(values["agent_run_id"])
    if values.get("cost_amount") is not None:
        values["cost_amount"] = Decimal(str(values["cost_amount"]))
    values["model_attempts"] = tuple(values.get("model_attempts", ()))
    return CaseExecutionObservation(**values)
