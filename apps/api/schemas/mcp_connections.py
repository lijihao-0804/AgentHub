"""Request and response contracts for remote MCP connections.

No response model here declares a secret field. That is the point of keeping
them in one small module: whether a token can escape is answerable by reading
this file, not by auditing every handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_ENDPOINT_URL_LENGTH = 2048


class McpConnectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    endpoint_url: str = Field(min_length=1, max_length=MAX_ENDPOINT_URL_LENGTH)
    auth_type: Literal["NONE", "BEARER"] = "NONE"
    # Write-only: accepted on create and rotate, never echoed back.
    secret: str | None = Field(default=None, min_length=1, max_length=8192)
    enabled: bool = True


class McpConnectionPatchRequest(BaseModel):
    """Only the two mutable fields exist here.

    ``endpoint_url`` and ``auth_type`` are absent by design rather than
    ignored: with ``extra="forbid"`` an attempt to repoint a connection is a
    422, not a silently dropped field.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None


class McpConnectionRotateSecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str = Field(min_length=1, max_length=8192)


class McpConnectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    workspace_id: UUID
    name: str
    endpoint_url: str
    auth_type: Literal["NONE", "BEARER"]
    # Whether a token exists, never anything about what it is.
    secret_configured: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime


class McpConnectionTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    status: Literal["healthy", "unavailable"]
    failure_code: str | None
    latency_ms: int
    protocol_version: str | None
    server_name: str | None


class McpDiscoveredToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str | None
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    # Carried verbatim from the server as information. Deliberately not mapped
    # to effect, risk level, approval policy or execution kind: those are
    # governance decisions, and 3A does not make them.
    remote_annotations: dict[str, bool] | None


class McpConnectionDiscoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    protocol_version: str | None
    server_name: str | None
    tools: list[McpDiscoveredToolResponse]


__all__ = [
    "McpConnectionCreateRequest",
    "McpConnectionDiscoveryResponse",
    "McpConnectionPatchRequest",
    "McpConnectionResponse",
    "McpConnectionRotateSecretRequest",
    "McpConnectionTestResponse",
    "McpDiscoveredToolResponse",
]
