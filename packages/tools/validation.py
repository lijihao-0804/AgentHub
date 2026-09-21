"""Shared validation for executable, provider-neutral tool revisions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, SchemaError

from packages.core.errors.exceptions import AgentHubError
from packages.tools.contracts import ToolApprovalPolicy, ToolEffect, ToolRisk, ToolSourceKind

_DEFAULT_TIMEOUT_SECONDS = 30
_MAX_TIMEOUT_SECONDS = 120

_SPEC_KINDS = {"builtin": ToolSourceKind.BUILTIN, "mcp": ToolSourceKind.MCP}
_MCP_FIELDS = frozenset({"connection_id", "tool_name", "output_schema"})
_MAX_REMOTE_TOOL_NAME_LENGTH = 128

# The narrowest shape every provider we can be asked to talk to accepts for a
# function name. Names are taken from the operator verbatim: a name outside this
# is rejected, never quietly rewritten into one that fits.
_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_valid_tool_identity(identity: object) -> bool:
    """Whether a logical AgentHub tool identity is safe for every model provider."""

    return isinstance(identity, str) and _IDENTITY_PATTERN.fullmatch(identity) is not None


def validate_executable_tool_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the exact fields required by executable tools."""

    if not isinstance(spec, Mapping):
        raise _invalid()
    required = ("kind", "identity", "input_schema", "effect", "risk_level", "approval_policy")
    if any(key not in spec for key in required):
        raise _invalid()
    if spec["kind"] not in _SPEC_KINDS:
        raise _invalid()
    if not isinstance(spec["identity"], str) or not spec["identity"]:
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
    if _SPEC_KINDS[spec["kind"]] is ToolSourceKind.MCP:
        normalized["mcp"] = _validate_mcp_block(spec)
        if not is_valid_tool_identity(spec["identity"]):
            raise _invalid()
    elif "mcp" in normalized:
        # A builtin spec that carries remote wiring is not a builtin spec.
        raise _invalid()
    return normalized


def _validate_mcp_block(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the remote wiring frozen alongside an MCP tool's governance."""

    block = spec.get("mcp")
    if not isinstance(block, Mapping) or set(block) - _MCP_FIELDS:
        raise _invalid()
    if not {"connection_id", "tool_name"} <= set(block):
        raise _invalid()
    try:
        connection_id = str(UUID(str(block["connection_id"])))
    except (TypeError, ValueError):
        raise _invalid() from None
    tool_name = block["tool_name"]
    if (
        not isinstance(tool_name, str)
        or not tool_name.strip()
        or len(tool_name) > _MAX_REMOTE_TOOL_NAME_LENGTH
    ):
        raise _invalid()
    output_schema = block.get("output_schema")
    if output_schema is not None:
        if not isinstance(output_schema, Mapping):
            raise _invalid()
        try:
            Draft202012Validator.check_schema(dict(output_schema))
        except SchemaError:
            raise _invalid() from None
        output_schema = deepcopy(dict(output_schema))
    return {
        "connection_id": connection_id,
        "tool_name": tool_name,
        "output_schema": output_schema,
    }


def _invalid() -> AgentHubError:
    return AgentHubError("TOOL_REVISION_INVALID", "The tool revision is invalid.", 422)


__all__ = ["is_valid_tool_identity", "validate_executable_tool_spec"]
