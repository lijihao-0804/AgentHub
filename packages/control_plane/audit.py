from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.models import AuditLog

_FORBIDDEN_METADATA_KEY = re.compile(r"(?:password|token|secret|credential)", re.IGNORECASE)


class UnsafeAuditMetadata(ValueError):
    """Raised when an audit payload attempts to include a secret-like field."""


def assert_safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if _FORBIDDEN_METADATA_KEY.search(str(key)):
                    raise UnsafeAuditMetadata(f"forbidden audit metadata key: {key}")
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return value


def append_audit(
    session: AsyncSession,
    *,
    action: str,
    resource_type: str,
    request_id: str | None,
    actor_user_id: UUID | None = None,
    organization_id: UUID | None = None,
    workspace_id: UUID | None = None,
    resource_id: str | None = None,
    safe_metadata: dict[str, Any] | None = None,
) -> AuditLog:
    metadata = assert_safe_metadata(safe_metadata or {})
    entry = AuditLog(
        actor_user_id=actor_user_id,
        organization_id=organization_id,
        workspace_id=workspace_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        safe_metadata=metadata,
    )
    session.add(entry)
    return entry
