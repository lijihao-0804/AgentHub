from __future__ import annotations

from collections.abc import Iterable

from packages.control_plane.enums import OrganizationRole, WorkspaceRole

ORGANIZATION_MANAGE = "organization_management"
WORKSPACE_ADMIN = "workspace_administration"
WORKSPACE_READ = "workspace_read"
APPROVE_ACTION = "approve_action"
RUN_ACTION = "run_action"


def resolve_permissions(
    organization_role: str,
    workspace_role: str | None,
) -> frozenset[str]:
    """Resolve permissions from server-side membership roles only."""
    if organization_role in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
        return frozenset({ORGANIZATION_MANAGE, WORKSPACE_ADMIN, WORKSPACE_READ, APPROVE_ACTION})
    if organization_role != OrganizationRole.MEMBER or workspace_role is None:
        return frozenset()
    if workspace_role == WorkspaceRole.DEVELOPER:
        return frozenset(
            {
                WORKSPACE_READ,
                "agent_create",
                "agent_edit",
                "agent_run",
                "knowledge_create",
                "knowledge_edit",
                "knowledge_run",
                "tool_create",
                "tool_edit",
                "tool_run",
                RUN_ACTION,
            }
        )
    if workspace_role == WorkspaceRole.VIEWER:
        return frozenset({WORKSPACE_READ})
    return frozenset()


def has_permission(permissions: Iterable[str], permission: str) -> bool:
    return permission in permissions


def ensure_not_last_owner(owner_count: int, current_role: str, new_role: str | None) -> None:
    if (
        current_role == OrganizationRole.OWNER
        and new_role != OrganizationRole.OWNER
        and owner_count <= 1
    ):
        from packages.core.errors.exceptions import AgentHubError

        raise AgentHubError(
            "LAST_OWNER_PROTECTION",
            "The last organization owner cannot be removed or downgraded.",
            409,
        )
