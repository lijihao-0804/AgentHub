"""Shared validation for executable, provider-neutral tool revisions."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

from packages.core.errors.exceptions import AgentHubError
from packages.tools.contracts import ToolApprovalPolicy, ToolEffect, ToolRisk

_DEFAULT_TIMEOUT_SECONDS = 30
_MAX_TIMEOUT_SECONDS = 120


def validate_executable_tool_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the exact fields required by executable tools."""

    if not isinstance(spec, Mapping):
        raise _invalid()
    required = ("kind", "identity", "input_schema", "effect", "risk_level", "approval_policy")
    if any(key not in spec for key in required):
        raise _invalid()
    if spec["kind"] != "builtin" or not isinstance(spec["identity"], str) or not spec["identity"]:
        raise _invalid()
    if not isinstance(spec["input_schema"], Mapping):
        raise _invalid()
    if spec["effect"] not in {item.value for item in ToolEffect}:
        raise _invalid()
    if spec["risk_level"] not in {item.value for item in ToolRisk}:
        raise _invalid()
    if spec["approval_policy"] not in {item.value for item in ToolApprovalPolicy}:
        raise _invalid()
    timeout = spec.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise _invalid()
    description = spec.get("description", "")
    if not isinstance(description, str):
        raise _invalid()
    try:
        Draft202012Validator.check_schema(dict(spec["input_schema"]))
    except SchemaError:
        raise _invalid() from None
    normalized = deepcopy(dict(spec))
    normalized["description"] = description
    normalized["timeout_seconds"] = timeout
    return normalized


def _invalid() -> AgentHubError:
    return AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)


__all__ = ["validate_executable_tool_spec"]
