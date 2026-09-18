"""M4-A tool revision hashing and persistence helpers.

This module only freezes tool definitions. It deliberately does not execute tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import Tool, ToolRevision
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.tools.validation import validate_executable_tool_spec

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "password",
        "secret",
        "token",
    }
)


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in _SECRET_KEYS or _contains_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False


def validate_tool_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy a provider-neutral, secret-free tool spec."""

    if not isinstance(spec, Mapping):
        raise AgentHubError("INVALID_TOOL_SPEC", "Tool specification must be an object.", 422)
    if _contains_secret_key(spec):
        raise AgentHubError("TOOL_SPEC_SECRET_FORBIDDEN", "Tool secrets are not allowed.", 422)
    return dict(spec)


def tool_spec_hash(spec: Mapping[str, Any]) -> str:
    return canonical_json_hash(validate_tool_spec(spec))


class ToolRevisionService:
    async def create_revision(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        *,
        tool_id: UUID,
        spec: Mapping[str, Any],
    ) -> ToolRevision:
        if "tool_edit" not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)
        workspace_id = _workspace_id(context)
        tool = await session.scalar(
            select(Tool)
            .where(Tool.id == tool_id, Tool.workspace_id == workspace_id)
            .with_for_update()
        )
        if tool is None:
            raise AgentHubError("TOOL_NOT_FOUND", "The tool was not found.", 404)
        normalized_spec = validate_tool_spec(spec)
        latest = await session.scalar(
            select(func.max(ToolRevision.revision_number)).where(
                ToolRevision.workspace_id == workspace_id,
                ToolRevision.tool_id == tool_id,
            )
        )
        revision = ToolRevision(
            workspace_id=workspace_id,
            tool_id=tool_id,
            revision_number=(latest or 0) + 1,
            spec=normalized_spec,
            spec_hash=canonical_json_hash(normalized_spec),
            created_by=_principal_id(context),
        )
        session.add(revision)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "TOOL_REVISION_CREATE_FAILED", "The tool revision could not be created.", 409
            ) from exc
        return revision


def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
    try:
        return UUID(context.workspace_id)
    except ValueError:
        raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None


def _principal_id(context: WorkspaceExecutionContext) -> UUID:
    try:
        if context.user_id is None:
            raise ValueError
        return UUID(context.user_id)
    except ValueError:
        raise AgentHubError("AUTHENTICATION_REQUIRED", "Authentication is required.", 401) from None


__all__ = [
    "ToolRevisionService",
    "tool_spec_hash",
    "validate_executable_tool_spec",
    "validate_tool_spec",
]
