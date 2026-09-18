"""Provider-neutral contracts for the M4-B read tool runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from packages.core.execution_context.models import WorkspaceExecutionContext


class ToolEffect(StrEnum):
    READ = "READ"
    WRITE = "WRITE"


class ToolRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ToolApprovalPolicy(StrEnum):
    NEVER = "NEVER"
    ALWAYS = "ALWAYS"


class ToolResultStatus(StrEnum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ToolDefinition:
    """The executable projection of one hash-verified published ToolRevision."""

    identity: str
    revision_id: UUID
    spec_hash: str
    description: str
    input_schema: dict[str, Any]
    effect: ToolEffect
    risk_level: ToolRisk
    approval_policy: ToolApprovalPolicy
    timeout_seconds: int
    snapshot_refs: tuple[dict[str, str], ...] = ()
    retrieval_config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolExecutionContext:
    """Request and tenant context passed to a builtin handler.

    The handler receives the already-authorized workspace context. It never receives a
    caller-provided workspace identifier and must use this immutable scope for reads.
    """

    workspace_context: WorkspaceExecutionContext
    agent_version_id: UUID
    tool_call_id: str

    @property
    def workspace_id(self) -> str:
        return self.workspace_context.workspace_id

    @property
    def organization_id(self) -> str:
        return self.workspace_context.organization.organization_id

    @property
    def user_id(self) -> str | None:
        return self.workspace_context.user_id

    @property
    def request_id(self) -> str:
        return self.workspace_context.request_id


@dataclass(frozen=True)
class ToolResult:
    status: ToolResultStatus
    data: Any = None
    error_code: str | None = None
    safe_message: str | None = None
    data_trust: str = "UNTRUSTED"
    duration_ms: float = 0.0

    @classmethod
    def success(cls, data: Any, *, duration_ms: float = 0.0) -> ToolResult:
        return cls(
            status=ToolResultStatus.SUCCESS,
            data=data,
            duration_ms=duration_ms,
        )

    @classmethod
    def failure(
        cls,
        error_code: str,
        safe_message: str,
        *,
        duration_ms: float = 0.0,
    ) -> ToolResult:
        return cls(
            status=ToolResultStatus.ERROR,
            error_code=error_code,
            safe_message=safe_message,
            duration_ms=duration_ms,
        )


__all__ = [
    "ToolApprovalPolicy",
    "ToolDefinition",
    "ToolEffect",
    "ToolExecutionContext",
    "ToolResult",
    "ToolResultStatus",
    "ToolRisk",
]
