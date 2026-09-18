"""Safe ToolAudit projection backed by the existing control-plane AuditLog."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.control_plane.audit import append_audit
from packages.core.execution_context.models import WorkspaceExecutionContext

logger = logging.getLogger(__name__)

_SAFE_KEYS = frozenset(
    {
        "tool_identity",
        "tool_revision_id",
        "agent_version_id",
        "decision",
        "status",
        "error_code",
        "duration_ms",
        "argument_keys",
    }
)


@dataclass(frozen=True)
class ToolAudit:
    """Provider-neutral, redacted execution audit projection."""

    tool_identity: str
    tool_revision_id: str | None
    agent_version_id: str
    decision: str | None
    status: str
    error_code: str | None
    duration_ms: float
    argument_keys: tuple[str, ...]

    def metadata(self) -> dict[str, Any]:
        return safe_tool_metadata(
            {
                "tool_identity": self.tool_identity,
                "tool_revision_id": self.tool_revision_id,
                "agent_version_id": self.agent_version_id,
                "decision": self.decision,
                "status": self.status,
                "error_code": self.error_code,
                "duration_ms": self.duration_ms,
                "argument_keys": list(self.argument_keys),
            }
        )


class ToolAuditSink(Protocol):
    async def record(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_version_id: UUID,
        tool_call_id: str,
        metadata: Mapping[str, Any],
    ) -> None: ...


def safe_tool_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    safe = {key: value for key, value in metadata.items() if key in _SAFE_KEYS}
    safe["argument_keys"] = sorted(
        key for key in safe.get("argument_keys", []) if isinstance(key, str)
    )
    return safe


class NoopToolAuditSink:
    async def record(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_version_id: UUID,
        tool_call_id: str,
        metadata: Mapping[str, Any],
    ) -> None:
        del context, agent_version_id, tool_call_id, metadata


class SqlAlchemyToolAuditSink:
    """Writes using a fresh session so audit persistence is independent of reads."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def record(
        self,
        context: WorkspaceExecutionContext,
        *,
        agent_version_id: UUID,
        tool_call_id: str,
        metadata: Mapping[str, Any],
    ) -> None:
        del tool_call_id
        safe_metadata = safe_tool_metadata(metadata)
        try:
            organization_id = UUID(context.organization.organization_id)
        except ValueError:
            organization_id = None
        try:
            workspace_id = UUID(context.workspace_id)
        except ValueError:
            workspace_id = None
        try:
            actor_user_id = UUID(context.user_id) if context.user_id else None
        except ValueError:
            actor_user_id = None
        async with self.session_factory() as session:
            append_audit(
                session,
                action="tool.execute",
                resource_type="tool",
                resource_id=str(agent_version_id),
                request_id=context.request_id,
                actor_user_id=actor_user_id,
                organization_id=organization_id,
                workspace_id=workspace_id,
                safe_metadata=safe_metadata,
            )
            await session.commit()


async def record_tool_audit(
    sink: ToolAuditSink,
    context: WorkspaceExecutionContext,
    *,
    agent_version_id: UUID,
    tool_call_id: str,
    metadata: Mapping[str, Any],
) -> None:
    try:
        await sink.record(
            context,
            agent_version_id=agent_version_id,
            tool_call_id=tool_call_id,
            metadata=safe_tool_metadata(metadata),
        )
    except Exception:
        logger.warning("tool_audit_failed", exc_info=True)


__all__ = [
    "NoopToolAuditSink",
    "SqlAlchemyToolAuditSink",
    "ToolAuditSink",
    "ToolAudit",
    "record_tool_audit",
    "safe_tool_metadata",
]
